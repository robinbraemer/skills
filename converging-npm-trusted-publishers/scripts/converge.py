#!/usr/bin/env python3
"""Fail-closed npm Trusted Publisher convergence through the pinned chrome-devtools-axi CLI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote


SUPPORTED_ACTIONS = ("npm publish", "npm stage publish")
PACKAGE_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$")
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9._-]+$")
WORKFLOW_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]+\.ya?ml$")
LEDGER_STATUSES = {
    "pending",
    "exact-match",
    "staged",
    "awaiting-human-auth",
    "migrating",
    "saved-verified",
    "blocked",
}
PINNED_AXI_VERSION = "0.1.29"
# The only chrome-devtools-axi commands the helper may ever issue. Everything
# else (eval/run, fill/fillform/type, screenshot, console/network, heap,
# lighthouse, perf-*, update, ...) is refused before any subprocess is spawned.
AXI_ALLOWED_COMMANDS = frozenset(
    {"newpage", "selectpage", "pages", "snapshot", "click", "press", "open", "closepage"}
)
# Named keys allowed for clearing a prefilled field during an authorized
# migration edit. Every press is followed by an exact value read-back.
CLEARING_KEYS = ("End", "Backspace")
# Semantic contract of the real npm access page (read from live snapshots of
# the deployed UI; any deviation is UI drift and stops the run).
SECTION_HEADING = "Trusted Publisher"
FIELD_LABELS = {
    "owner": "Organization or user*",
    "repository": "Repository*",
    "workflow": "Workflow filename*",
    "environment": "Environment name",
}
ACTION_CHECKBOX_LABELS = {action: f"Allow {action}" for action in SUPPORTED_ACTIONS}
PROVIDER_COMBOBOX_LABEL = "Publisher*"
PROVIDER_NAME = "GitHub Actions"
SAVE_BUTTON_LABEL = "Save changes to trusted publisher connection"
EDIT_BUTTON_LABEL = "Edit"
DELETE_BUTTON_LABEL = "Delete OIDC trusted publisher connection"
PERMISSIONS_LABEL = "Permissions: "
BLOCK_REASONS = {
    "unexpected-publisher",
    "identity-mismatch",
    "ui-drift",
    "authentication-failed",
    "authentication-ambiguous",
    "save-failed",
    "partial-save",
    "readback-mismatch",
    "migration-interrupted",
    "harness-error",
}


DEBUG_CLASSIFY = False


def _debug_classify(message: str) -> None:
    """Redaction-safe classifier tracing (--debug-classify).

    Prints only fixed diagnostic strings, role/name booleans, and the observed
    publisher tuple (public information); never page content, URLs beyond the
    canonical package URL match result, or session material.
    """
    if DEBUG_CLASSIFY:
        print(f"debug-classify: {message}")


class ManifestError(ValueError):
    """The manifest cannot be interpreted without guessing."""


class LedgerError(ValueError):
    """The resume ledger is malformed or belongs to another manifest."""


class HarnessError(RuntimeError):
    """The restricted visible-page interface cannot be used safely."""


class StaleRefError(HarnessError):
    """An element ref was minted for an older snapshot generation."""


@dataclass(frozen=True)
class Publisher:
    owner: str
    repository: str
    workflow: str
    environment: str | None
    allowed_actions: tuple[str, ...]

    def tuple_key(self) -> tuple[str, str, str, str | None]:
        return (self.owner, self.repository, self.workflow, self.environment)


@dataclass(frozen=True)
class Manifest:
    packages: tuple[str, ...]
    publisher: Publisher
    schema_version: int = 1
    previous_publisher: Publisher | None = None


@dataclass(frozen=True)
class Observation:
    state: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"absent", "exact", "previous", "blocked"}:
            raise ValueError("invalid observation state")
        if self.state == "blocked":
            if self.reason not in BLOCK_REASONS:
                raise ValueError("invalid blocked observation reason")
        elif self.reason is not None:
            raise ValueError("only blocked observations may have a reason")


def _checked_keys(
    value: Any, required: set[str], optional: set[str], label: str
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    actual = set(value)
    unknown = sorted(actual - required - optional)
    missing = sorted(required - actual)
    if unknown:
        raise ManifestError(f"unknown {label} keys: {', '.join(unknown)}")
    if missing:
        raise ManifestError(f"missing {label} keys: {', '.join(missing)}")
    return value


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    return _checked_keys(value, expected, set(), label)


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ManifestError(f"{label} must be a non-empty exact string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ManifestError(f"{label} contains a control character")
    return value


def _load_publisher_fields(value: Mapping[str, Any]) -> tuple[str, str, str, str | None]:
    owner = _string(value["owner"], "owner")
    if not OWNER_RE.fullmatch(owner):
        raise ManifestError("owner is not a valid GitHub owner")
    repository = _string(value["repository"], "repository")
    if not REPOSITORY_RE.fullmatch(repository):
        raise ManifestError("repository is not a valid GitHub repository name")
    workflow = _string(value["workflow"], "workflow")
    if not WORKFLOW_RE.fullmatch(workflow):
        raise ManifestError("workflow must be a .yml or .yaml filename, not a path")
    environment_value = value["environment"]
    if environment_value is None:
        environment = None
    else:
        environment = _string(environment_value, "environment")
    return owner, repository, workflow, environment


def _load_allowed_actions(actions_value: Any) -> tuple[str, ...]:
    if not isinstance(actions_value, list) or not actions_value:
        raise ManifestError("allowed_actions must be a non-empty array")
    if any(not isinstance(action, str) for action in actions_value):
        raise ManifestError("allowed_actions must contain strings")
    if len(set(actions_value)) != len(actions_value):
        raise ManifestError("allowed_actions must not contain duplicates")
    unsupported = sorted(set(actions_value) - set(SUPPORTED_ACTIONS))
    if unsupported:
        raise ManifestError("allowed_actions contains an unsupported action")
    return tuple(action for action in SUPPORTED_ACTIONS if action in actions_value)


def load_manifest(path: str) -> Manifest:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read manifest: {error.__class__.__name__}") from error

    if not isinstance(raw, dict):
        raise ManifestError("manifest must be an object")
    schema_version = raw.get("schema_version")
    if isinstance(schema_version, bool) or schema_version not in (1, 2):
        raise ManifestError("schema_version must equal 1 or 2")
    required = {"schema_version", "packages", "publisher"}
    optional = {"previous_publisher"} if schema_version == 2 else set()
    root = _checked_keys(raw, required, optional, "manifest")

    packages_value = root["packages"]
    if not isinstance(packages_value, list) or not packages_value:
        raise ManifestError("packages must be a non-empty array")
    packages: list[str] = []
    for value in packages_value:
        package = _string(value, "package")
        if not PACKAGE_RE.fullmatch(package):
            raise ManifestError(f"invalid package name: {package!r}")
        packages.append(package)
    if len(set(packages)) != len(packages):
        raise ManifestError("packages must not contain duplicates")

    publisher_value = _exact_keys(
        root["publisher"],
        {"owner", "repository", "workflow", "environment", "allowed_actions"},
        "publisher",
    )
    owner, repository, workflow, environment = _load_publisher_fields(publisher_value)
    allowed_actions = _load_allowed_actions(publisher_value["allowed_actions"])
    publisher = Publisher(
        owner=owner,
        repository=repository,
        workflow=workflow,
        environment=environment,
        allowed_actions=allowed_actions,
    )

    previous_publisher: Publisher | None = None
    if "previous_publisher" in root:
        previous_value = _checked_keys(
            root["previous_publisher"],
            {"owner", "repository", "workflow", "environment"},
            {"allowed_actions"},
            "previous_publisher",
        )
        p_owner, p_repository, p_workflow, p_environment = _load_publisher_fields(
            previous_value
        )
        if "allowed_actions" in previous_value:
            previous_actions = _load_allowed_actions(previous_value["allowed_actions"])
            if previous_actions != allowed_actions:
                raise ManifestError(
                    "previous_publisher allowed_actions must be omitted or equal the target's"
                )
        previous_publisher = Publisher(
            owner=p_owner,
            repository=p_repository,
            workflow=p_workflow,
            environment=p_environment,
            allowed_actions=allowed_actions,
        )
        if previous_publisher.tuple_key() == publisher.tuple_key():
            raise ManifestError("previous_publisher must differ from publisher")

    return Manifest(
        packages=tuple(packages),
        publisher=publisher,
        schema_version=schema_version,
        previous_publisher=previous_publisher,
    )


def package_url(package: str) -> str:
    if not PACKAGE_RE.fullmatch(package):
        raise ManifestError("invalid package name")
    return f"https://www.npmjs.com/package/{quote(package, safe='@/')}/access"


def _publisher_data(publisher: Publisher) -> dict[str, Any]:
    return {
        "owner": publisher.owner,
        "repository": publisher.repository,
        "workflow": publisher.workflow,
        "environment": publisher.environment,
        "allowed_actions": list(publisher.allowed_actions),
    }


def _manifest_data(manifest: Manifest) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": manifest.schema_version,
        "packages": list(manifest.packages),
        "publisher": _publisher_data(manifest.publisher),
    }
    if manifest.previous_publisher is not None:
        data["previous_publisher"] = _publisher_data(manifest.previous_publisher)
    return data


def manifest_digest(manifest: Manifest) -> str:
    encoded = json.dumps(
        _manifest_data(manifest), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class LedgerStore:
    def __init__(self, path: str, manifest: Manifest):
        self.path = Path(path)
        self.manifest = manifest
        self.digest = manifest_digest(manifest)
        if self.path.exists():
            self.records = self._load()
        else:
            self.records = {
                package: {"status": "pending"} for package in manifest.packages
            }
            self._write()

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LedgerError(f"cannot read ledger: {error.__class__.__name__}") from error
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "manifest_digest",
            "packages",
        }:
            raise LedgerError("invalid ledger keys")
        if raw["schema_version"] != 1 or isinstance(raw["schema_version"], bool):
            raise LedgerError("invalid ledger schema_version")
        if raw["manifest_digest"] != self.digest:
            raise LedgerError("ledger manifest does not match")
        packages = raw["packages"]
        if not isinstance(packages, dict) or set(packages) != set(self.manifest.packages):
            raise LedgerError("ledger packages do not match manifest")

        records: dict[str, dict[str, str]] = {}
        for package in self.manifest.packages:
            record = packages[package]
            if not isinstance(record, dict) or not set(record) <= {"status", "reason"}:
                raise LedgerError("invalid ledger record keys")
            if "status" not in record or record["status"] not in LEDGER_STATUSES:
                raise LedgerError("invalid ledger status")
            status = record["status"]
            reason = record.get("reason")
            if status == "blocked":
                if reason not in BLOCK_REASONS:
                    raise LedgerError("invalid ledger reason")
            elif reason is not None:
                raise LedgerError("reason is allowed only for blocked status")
            records[package] = dict(record)
        return records

    def set(self, package: str, status: str, reason: str | None = None) -> None:
        if package not in self.records:
            raise LedgerError("package is not in manifest")
        if status not in LEDGER_STATUSES:
            raise LedgerError("invalid ledger status")
        if status == "blocked":
            if reason not in BLOCK_REASONS:
                raise LedgerError("invalid ledger reason")
            record = {"status": status, "reason": reason}
        else:
            if reason is not None:
                raise LedgerError("reason is allowed only for blocked status")
            record = {"status": status}
        self.records[package] = record
        self._write()

    def _write(self) -> None:
        data = {
            "schema_version": 1,
            "manifest_digest": self.digest,
            "packages": self.records,
        }
        parent = self.path.parent
        if not parent.is_dir():
            raise LedgerError("ledger parent directory does not exist")
        temporary_path: Path | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=parent
            )
            temporary_path = Path(temporary_name)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(data, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.path)
            os.chmod(self.path, 0o600)
            temporary_path = None
        except OSError as error:
            raise LedgerError(f"cannot write ledger: {error.__class__.__name__}") from error
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass


@dataclass(frozen=True)
class AxiResult:
    """Exit code and stdout of one pinned chrome-devtools-axi invocation."""

    exit_code: int
    stdout: str


@dataclass(frozen=True)
class AxNode:
    """One parsed accessibility snapshot line: uid ref, role, name, attributes."""

    uid: str
    role: str
    name: str
    attrs: Mapping[str, Any]


def default_axi_script() -> str:
    """Path of the vendored, pinned chrome-devtools-axi entrypoint."""
    home = os.environ.get("CODEX_HOME") or os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(
        home,
        "tools",
        f"chrome-devtools-axi-{PINNED_AXI_VERSION}",
        "dist",
        "bin",
        "chrome-devtools-axi.js",
    )


class AxiTransport:
    """Subprocess transport bound to the vendored chrome-devtools-axi release.

    Never resolves the CLI through npx/bunx or a registry; the script path must
    already exist on disk (the reviewed, pinned install).
    """

    def __init__(self, script_path: str, bun_path: str = "bun"):
        script = Path(script_path)
        if not script.is_file():
            raise HarnessError("pinned chrome-devtools-axi script is missing")
        self.script = script
        self.bun = bun_path

    def run(self, *args: str) -> AxiResult:
        try:
            completed = subprocess.run(
                [self.bun, str(self.script), *args],
                capture_output=True,
                text=True,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise HarnessError("axi invocation failed") from error
        return AxiResult(exit_code=completed.returncode, stdout=completed.stdout)


_NODE_LINE_RE = re.compile(
    r"^\s*uid=(?P<uid>\S+)\s+(?P<role>[A-Za-z][A-Za-z0-9-]*)(?P<rest>.*)$",
    re.DOTALL,
)
_NODE_START_RE = re.compile(r"^\s*uid=\S")
_ATTRS_ONLY_RE = re.compile(r'(?:\s+[A-Za-z_][A-Za-z0-9_]*(?:="[^"]*")?)*\s*')
_ATTR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)(?:="([^"]*)")?')
_UID_RE = re.compile(r"^g\d+:\S+$")
_HELP_BLOCK_RE = re.compile(r"^help\[\d+\]:")
_PAGES_HEADER_RE = re.compile(r"^pages\[\d+\]\{id,url,selected\}:$")
_PAGES_ROW_RE = re.compile(r"^\s*(\d+),(.*),(true|false)$")
_PAGES_EMPTY_RE = re.compile(r"^pages: 0 pages open$", re.MULTILINE)
_STALE_CODE_RE = re.compile(r"^code: STALE_REF$", re.MULTILINE)


class AxiDriver:
    """Drives the npm access page through the restricted AXI command allowlist.

    Element targeting is always role+name against a fresh accessibility
    snapshot; generation-tagged refs are passed back exactly as printed, and a
    STALE_REF answer gets exactly one fresh re-snapshot retry before the run
    fails closed. An existing connection is only ever changed through the
    page's own in-place Edit form, and only when its tuple exactly matches the
    manifest's approved previous_publisher; the Delete control is never used.
    """

    def __init__(
        self,
        transport: Any,
        *,
        poll_attempts: int = 600,
        poll_interval: float = 0.5,
        render_attempts: int = 5,
        render_interval: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        run = getattr(transport, "run", None)
        if not callable(run):
            raise HarnessError("axi transport does not provide a run command")
        if poll_attempts < 1 or poll_interval < 0:
            raise HarnessError("invalid save polling bounds")
        if render_attempts < 1 or render_interval < 0:
            raise HarnessError("invalid render settling bounds")
        self.transport = transport
        self.poll_attempts = poll_attempts
        self.poll_interval = poll_interval
        self.render_attempts = render_attempts
        self.render_interval = render_interval
        self.sleeper = sleeper
        self.tabs: list[tuple[str, str, str]] = []

    # --- restricted CLI boundary ---

    def _axi(self, command: str, *args: str) -> str:
        if command not in AXI_ALLOWED_COMMANDS:
            raise HarnessError("axi command is not allowlisted")
        if any(not isinstance(argument, str) for argument in args):
            raise HarnessError("axi arguments must be strings")
        result = self.transport.run(command, *args)
        exit_code = getattr(result, "exit_code", None)
        stdout = getattr(result, "stdout", None)
        if not isinstance(exit_code, int) or isinstance(exit_code, bool):
            raise HarnessError("axi transport returned an invalid exit code")
        if not isinstance(stdout, str):
            raise HarnessError("axi transport returned invalid output")
        if exit_code != 0:
            if _STALE_CODE_RE.search(stdout):
                raise StaleRefError("axi ref is stale")
            raise HarnessError("axi command failed")
        return stdout

    # --- snapshot parsing ---

    @staticmethod
    def _parse_rest(rest: str) -> tuple[str, dict[str, Any]]:
        """Split a node line's remainder into accessible name and attributes.

        The upstream formatter renders names unescaped, so a name may itself
        contain quotes. The closing quote is found by scanning candidates left
        to right and accepting the first split whose remainder is a pure
        attribute sequence. Decision-critical bare tokens (checked, disabled,
        focused) cannot be forged through a name: the formatter's final closing
        quote always poisons a bare-token injection, and string-valued
        injections either parse into non-boolean values that fail closed or
        fall through to the full-name parse.
        """

        def attrs_of(text: str) -> dict[str, Any]:
            attrs: dict[str, Any] = {}
            for attr_match in _ATTR_RE.finditer(text):
                attrs[attr_match.group(1)] = (
                    attr_match.group(2) if attr_match.group(2) is not None else True
                )
            return attrs

        if _ATTRS_ONLY_RE.fullmatch(rest):
            return "", attrs_of(rest)
        opening = rest.find('"')
        if opening == -1 or rest[:opening].strip():
            raise HarnessError("unparseable snapshot line")
        for closing in range(opening + 1, len(rest)):
            if rest[closing] != '"':
                continue
            remainder = rest[closing + 1 :]
            if _ATTRS_ONLY_RE.fullmatch(remainder):
                return rest[opening + 1 : closing], attrs_of(remainder)
        raise HarnessError("unparseable snapshot line")

    @classmethod
    def _parse_node(cls, line: str) -> AxNode:
        node_match = _NODE_LINE_RE.match(line)
        if node_match is None:
            raise HarnessError("unparseable snapshot line")
        uid = node_match.group("uid")
        if not _UID_RE.fullmatch(uid):
            raise HarnessError("snapshot ref has no generation tag")
        name, attrs = cls._parse_rest(node_match.group("rest"))
        return AxNode(uid=uid, role=node_match.group("role"), name=name, attrs=attrs)

    def _parse_page(self, stdout: str) -> tuple[str, list[AxNode]]:
        lines = stdout.splitlines()
        start = next(
            (index for index, line in enumerate(lines) if line == "snapshot:"), None
        )
        if start is None:
            raise HarnessError("axi output has no snapshot section")
        # Accessible names are rendered unescaped by the upstream formatter, so
        # a name containing a literal newline (LineBreak nodes, multi-line
        # text) splits one node across several physical lines. Rejoin: a line
        # starting with "uid=" opens a node; anything else continues the
        # current node's name verbatim.
        logical_lines: list[str] = []
        for line in lines[start + 1 :]:
            if _HELP_BLOCK_RE.match(line):
                break
            if "(truncated," in line:
                raise HarnessError("snapshot was truncated")
            if _NODE_START_RE.match(line):
                logical_lines.append(line)
            elif logical_lines:
                logical_lines[-1] += "\n" + line
            elif line.strip():
                raise HarnessError("unparseable snapshot line")
        nodes = [self._parse_node(line) for line in logical_lines]
        if not nodes or nodes[0].role != "RootWebArea":
            raise HarnessError("snapshot has no root web area")
        url = nodes[0].attrs.get("url")
        if not isinstance(url, str):
            raise HarnessError("snapshot root has no page url")
        return url, [node for node in nodes if node.role != "ignored"]

    def _page_ids(self) -> list[str]:
        """List open page IDs.

        Only the ID column is trusted: in the pinned CLI the url column holds a
        title fragment and the selected flag misparses titled pages, so page
        identity is always re-proven from the snapshot root URL instead.
        """
        stdout = self._axi("pages")
        lines = stdout.splitlines()
        start = next(
            (index for index, line in enumerate(lines) if _PAGES_HEADER_RE.match(line)),
            None,
        )
        if start is None:
            if _PAGES_EMPTY_RE.search(stdout):
                return []
            raise HarnessError("axi pages output has no page table")
        ids: list[str] = []
        for line in lines[start + 1 :]:
            row_match = _PAGES_ROW_RE.match(line)
            if row_match is None:
                break
            ids.append(row_match.group(1))
        if not ids:
            raise HarnessError("axi pages output has no rows")
        return ids

    # --- tab management ---

    def open_package(self, package: str, url: str) -> str:
        before = set(self._page_ids())
        self._axi("newpage", url, "--full")
        added = [page_id for page_id in self._page_ids() if page_id not in before]
        if len(added) != 1:
            raise HarnessError("newly opened page is not uniquely identifiable")
        handle = added[0]
        self.tabs.append((handle, package, url))
        return handle

    def close_tabs(self) -> None:
        """Close every tab this run opened (never the user's own tabs)."""
        for handle, _, _ in self.tabs:
            try:
                self._axi("closepage", handle)
            except HarnessError:
                continue
        self.tabs = []

    def _expected(self, handle: Any) -> tuple[str, str]:
        matches = [
            (package, url)
            for candidate, package, url in self.tabs
            if candidate is handle or candidate == handle
        ]
        if len(matches) != 1:
            raise HarnessError("unknown or ambiguous tab handle")
        return matches[0]

    # --- semantic lookup ---

    @staticmethod
    def _matches(tree: list[AxNode], role: str, name: str) -> list[AxNode]:
        return [node for node in tree if node.role == role and node.name == name]

    def _one(self, tree: list[AxNode], role: str, name: str) -> AxNode:
        matches = self._matches(tree, role, name)
        if len(matches) != 1:
            raise HarnessError(f"ambiguous or missing semantic control: {role}/{name}")
        return matches[0]

    @staticmethod
    def _field_value(node: AxNode) -> str:
        value = node.attrs.get("value")
        if value is None:
            # The real npm UI omits the value attribute on empty textboxes.
            return ""
        if not isinstance(value, str):
            raise HarnessError("textbox has no readable value")
        return value

    @staticmethod
    def _checked(node: AxNode) -> bool:
        value = node.attrs.get("checked")
        if value is True:
            return True
        if value is None:
            return False
        raise HarnessError("allowed-action control has no boolean checked state")

    @staticmethod
    def _package_headings(tree: list[AxNode], package: str) -> list[AxNode]:
        # The real page appends icon alt text to the package heading
        # (e.g. "@scope/pkg TypeScript icon, ..."), so accept an exact name or
        # the package name followed by a space.
        return [
            node
            for node in tree
            if node.role == "heading"
            and node.attrs.get("level") == "1"
            and (node.name == package or node.name.startswith(f"{package} "))
        ]

    def _form(self, tree: list[AxNode]) -> dict[str, AxNode]:
        controls: dict[str, AxNode] = {}
        controls["provider"] = self._one(tree, "combobox", PROVIDER_COMBOBOX_LABEL)
        self._one(tree, "StaticText", PROVIDER_NAME)
        for field, label in FIELD_LABELS.items():
            controls[field] = self._one(tree, "textbox", label)
        for action, label in ACTION_CHECKBOX_LABELS.items():
            controls[action] = self._one(tree, "checkbox", label)
        controls["save"] = self._one(tree, "button", SAVE_BUTTON_LABEL)
        for name, control in controls.items():
            if name != "save" and "disabled" in control.attrs:
                raise HarnessError("semantic control is disabled")
        for action in SUPPORTED_ACTIONS:
            self._checked(controls[action])
        return controls

    def _summary(self, tree: list[AxNode]) -> tuple[str, str, str, set[str]]:
        """Parse the existing-connection summary view into its visible tuple.

        The Edit button's machine-readable description ("owner/repo workflow")
        must be corroborated by the visible StaticText nodes; any disagreement
        is drift. The environment is not rendered in this view, so only
        environment-less tuples can ever be matched from a summary.
        """
        edit = self._one(tree, "button", EDIT_BUTTON_LABEL)
        if "disabled" in edit.attrs:
            raise HarnessError("edit control is disabled")
        self._one(tree, "button", DELETE_BUTTON_LABEL)
        description = edit.attrs.get("description")
        if not isinstance(description, str):
            raise HarnessError("edit control has no tuple description")
        coordinate, separator, workflow = description.rpartition(" ")
        owner, slash, repository = coordinate.partition("/")
        if (
            separator != " "
            or slash != "/"
            or not OWNER_RE.fullmatch(owner)
            or not REPOSITORY_RE.fullmatch(repository)
            or not WORKFLOW_RE.fullmatch(workflow)
        ):
            raise HarnessError("edit control description is not a publisher tuple")
        self._one(tree, "StaticText", coordinate)
        self._one(tree, "StaticText", workflow)
        self._one(tree, "StaticText", PERMISSIONS_LABEL)
        actions = {
            action
            for action in SUPPORTED_ACTIONS
            if len(self._matches(tree, "StaticText", action)) == 1
        }
        return owner, repository, workflow, actions

    def _verify_identity(self, handle: Any, url: str, tree: list[AxNode]) -> None:
        package, expected_url = self._expected(handle)
        if url != expected_url:
            raise HarnessError("package page identity changed")
        if len(self._package_headings(tree, package)) != 1:
            raise HarnessError("visible package identity changed")
        if len(self._matches(tree, "heading", SECTION_HEADING)) != 1:
            raise HarnessError("Trusted Publisher section changed")

    def _current(self, handle: Any) -> tuple[str, list[AxNode], dict[str, AxNode]]:
        package, _ = self._expected(handle)
        url, tree = self._parse_page(self._axi("selectpage", handle, "--full"))
        self._verify_identity(handle, url, tree)
        return package, tree, self._form(tree)

    # --- interaction primitives ---

    def _click_control(
        self, handle: Any, tree: list[AxNode], role: str, name: str
    ) -> tuple[str, list[AxNode]]:
        node = self._one(tree, role, name)
        try:
            stdout = self._axi("click", f"@{node.uid}", "--full")
        except StaleRefError:
            url, fresh = self._parse_page(self._axi("snapshot", "--full"))
            self._verify_identity(handle, url, fresh)
            node = self._one(fresh, role, name)
            try:
                stdout = self._axi("click", f"@{node.uid}", "--full")
            except StaleRefError as error:
                raise HarnessError(
                    "element reference stayed stale after one refresh"
                ) from error
        return self._parse_page(stdout)

    def _press_char(self, key: str) -> tuple[str, list[AxNode]]:
        if len(key) != 1 or ord(key) < 32 or ord(key) == 127:
            raise HarnessError("only single printable characters may be typed")
        return self._parse_page(self._axi("press", key, "--full"))

    def _press_named(self, key: str) -> tuple[str, list[AxNode]]:
        if key not in CLEARING_KEYS:
            raise HarnessError("named key is not allowlisted for clearing")
        return self._parse_page(self._axi("press", key, "--full"))

    def _verified_form_field(
        self, handle: Any, url: str, tree: list[AxNode], field: str
    ) -> AxNode:
        self._verify_identity(handle, url, tree)
        controls = self._form(tree)
        node = controls[field]
        if node.attrs.get("focused") is not True:
            raise HarnessError("textbox lost focus during keyboard input")
        return node

    def _focus_field(
        self, handle: Any, tree: list[AxNode], field: str
    ) -> tuple[str, list[AxNode]]:
        url, tree = self._click_control(handle, tree, "textbox", FIELD_LABELS[field])
        node = self._verified_form_field(handle, url, tree, field)
        return url, tree

    def _clear_field(
        self, handle: Any, tree: list[AxNode], field: str, current: str
    ) -> tuple[str, list[AxNode]]:
        """Empty a prefilled field with End + per-key Backspace read-back."""
        url, tree = self._press_named("End")
        node = self._verified_form_field(handle, url, tree, field)
        if self._field_value(node) != current:
            raise HarnessError("textbox changed while positioning the caret")
        remaining = current
        while remaining:
            url, tree = self._press_named("Backspace")
            remaining = remaining[:-1]
            node = self._verified_form_field(handle, url, tree, field)
            if self._field_value(node) != remaining:
                raise HarnessError("textbox clearing read-back mismatch")
        return url, tree

    def _type_field(
        self, handle: Any, tree: list[AxNode], field: str, desired: str
    ) -> tuple[str, list[AxNode]]:
        prefix = ""
        url = ""
        for character in desired:
            url, tree = self._press_char(character)
            prefix += character
            node = self._verified_form_field(handle, url, tree, field)
            if self._field_value(node) != prefix:
                raise HarnessError("textbox prefix read-back mismatch")
        return url, tree

    def _set_actions(self, handle: Any, desired_actions: set[str]) -> None:
        for action in SUPPORTED_ACTIONS:
            _, tree, controls = self._current(handle)
            if self._checked(controls[action]) == (action in desired_actions):
                continue
            label = ACTION_CHECKBOX_LABELS[action]
            url, tree = self._click_control(handle, tree, "checkbox", label)
            self._verify_identity(handle, url, tree)
            refreshed = self._form(tree)
            if self._checked(refreshed[action]) != (action in desired_actions):
                raise HarnessError("allowed-action read-back mismatch")

    def _verify_staged(self, handle: Any, publisher: Publisher) -> None:
        _, _, controls = self._current(handle)
        final_values = {
            field: self._field_value(controls[field]) for field in FIELD_LABELS
        }
        final_actions = {
            action
            for action in SUPPORTED_ACTIONS
            if self._checked(controls[action]) is True
        }
        if final_values != _desired_values(publisher) or final_actions != set(
            publisher.allowed_actions
        ):
            raise HarnessError("staged form read-back mismatch")

    def _settled_page(self, handle: Any, package: str) -> tuple[str, list[AxNode]]:
        """Select the tab and wait briefly for the SPA to finish rendering.

        npm hydrates the access page after load; a snapshot taken too early
        lacks the package heading or publisher section. Re-snapshot a bounded
        number of times until the identity anchors and a publisher view are
        present, then hand the tree to the normal fail-closed checks (which
        still stop the run if the page never settles).
        """
        url, tree = self._parse_page(self._axi("selectpage", handle, "--full"))
        for _ in range(self.render_attempts - 1):
            if (
                url == package_url(package)
                and len(self._package_headings(tree, package)) == 1
                and len(self._matches(tree, "heading", SECTION_HEADING)) == 1
                and (
                    self._matches(tree, "button", EDIT_BUTTON_LABEL)
                    or self._matches(tree, "button", SAVE_BUTTON_LABEL)
                )
            ):
                break
            self.sleeper(self.render_interval)
            url, tree = self._parse_page(self._axi("snapshot", "--full"))
        return url, tree

    # --- driver surface used by the converger ---

    def inspect(
        self,
        handle: Any,
        package: str,
        publisher: Publisher,
        previous: Publisher | None = None,
    ) -> Observation:
        try:
            url, tree = self._settled_page(handle, package)
            if url != package_url(package):
                _debug_classify(f"{package}: canonical-url-match=False")
                return Observation("blocked", "identity-mismatch")
            if len(self._package_headings(tree, package)) != 1:
                step_up = bool(
                    self._matches(tree, "heading", "Two-Factor Authentication")
                )
                _debug_classify(
                    f"{package}: package-heading-unique=False "
                    f"auth-step-up-detected={step_up}"
                )
                return Observation("blocked", "identity-mismatch")
            if len(self._matches(tree, "heading", SECTION_HEADING)) != 1:
                _debug_classify(f"{package}: section-heading-unique=False")
                return Observation("blocked", "ui-drift")
            if self._matches(tree, "button", EDIT_BUTTON_LABEL):
                _debug_classify(f"{package}: view=summary")
                return self._classify_summary(tree, publisher, previous)
            _debug_classify(f"{package}: view=form")
            return self._classify_form(self._form(tree), publisher, previous)
        except HarnessError as error:
            _debug_classify(f"{package}: ui-drift ({error})")
            return Observation("blocked", "ui-drift")

    def _classify_summary(
        self, tree: list[AxNode], publisher: Publisher, previous: Publisher | None
    ) -> Observation:
        owner, repository, workflow, actions = self._summary(tree)
        observed = (owner, repository, workflow, None)
        _debug_classify(
            f"summary tuple {owner}/{repository} {workflow} actions={sorted(actions)} "
            f"target-match={observed == publisher.tuple_key()} "
            f"previous-match={previous is not None and observed == previous.tuple_key()}"
        )
        if observed == publisher.tuple_key() and actions == set(
            publisher.allowed_actions
        ):
            return Observation("exact")
        if previous is not None and observed == previous.tuple_key():
            return Observation("previous")
        return Observation("blocked", "unexpected-publisher")

    def _classify_form(
        self,
        controls: dict[str, AxNode],
        publisher: Publisher,
        previous: Publisher | None,
    ) -> Observation:
        values = {field: self._field_value(controls[field]) for field in FIELD_LABELS}
        checked = {
            action
            for action in SUPPORTED_ACTIONS
            if self._checked(controls[action]) is True
        }
        if all(value == "" for value in values.values()) and not checked:
            return Observation("absent")
        if values == _desired_values(publisher) and checked == set(
            publisher.allowed_actions
        ):
            return Observation("exact")
        if previous is not None and values == _desired_values(previous):
            return Observation("previous")
        return Observation("blocked", "unexpected-publisher")

    def stage(self, handle: Any, publisher: Publisher) -> None:
        """Fill the empty create form (absent publisher) with the target tuple."""
        desired_values = _desired_values(publisher)
        for field, desired in desired_values.items():
            _, tree, controls = self._current(handle)
            if self._field_value(controls[field]) != "":
                raise HarnessError("textbox is not empty before staging")
            if desired == "":
                continue
            url, tree = self._focus_field(handle, tree, field)
            node = self._form(tree)[field]
            if self._field_value(node) != "":
                raise HarnessError("textbox changed before keyboard input")
            url, tree = self._type_field(handle, tree, field, desired)

        for action in SUPPORTED_ACTIONS:
            _, _, controls = self._current(handle)
            if self._checked(controls[action]) is not False:
                raise HarnessError("allowed-action form was not initially empty")
        self._set_actions(handle, set(publisher.allowed_actions))
        self._verify_staged(handle, publisher)

    def migrate_stage(
        self, handle: Any, previous: Publisher, publisher: Publisher
    ) -> None:
        """Rewrite the approved previous tuple to the target via in-place Edit.

        Opens the page's own Edit form (never Delete), verifies every prefilled
        value equals the approved previous tuple, then per-field clears and
        retypes only the fields that differ, with a read-back after every key.
        Nothing is persisted until save_and_wait clicks Save.
        """
        package, _ = self._expected(handle)
        url, tree = self._parse_page(self._axi("selectpage", handle, "--full"))
        self._verify_identity(handle, url, tree)
        observed = self._summary(tree)
        if (observed[0], observed[1], observed[2], None) != previous.tuple_key():
            raise HarnessError("summary tuple changed before editing")
        url, tree = self._click_control(handle, tree, "button", EDIT_BUTTON_LABEL)
        self._verify_identity(handle, url, tree)
        controls = self._form(tree)

        previous_values = _desired_values(previous)
        desired_values = _desired_values(publisher)
        for field in FIELD_LABELS:
            if self._field_value(controls[field]) != previous_values[field]:
                raise HarnessError("edit form does not show the approved previous tuple")

        for field, desired in desired_values.items():
            current = previous_values[field]
            if desired == current:
                continue
            _, tree, controls = self._current(handle)
            if self._field_value(controls[field]) != current:
                raise HarnessError("textbox changed before editing")
            url, tree = self._focus_field(handle, tree, field)
            if current:
                url, tree = self._clear_field(handle, tree, field, current)
            if desired:
                url, tree = self._type_field(handle, tree, field, desired)

        self._set_actions(handle, set(publisher.allowed_actions))
        self._verify_staged(handle, publisher)

    @staticmethod
    def _messages(tree: list[AxNode]) -> Counter:
        return Counter(
            (node.role, node.name)
            for node in tree
            if node.role in {"alert", "status"}
        )

    def save_and_wait(self, handle: Any) -> str:
        _, tree, controls = self._current(handle)
        save = controls["save"]
        if "disabled" in save.attrs:
            return "save-failed"
        baseline_messages = self._messages(tree)
        url, tree = self._click_control(handle, tree, "button", SAVE_BUTTON_LABEL)

        for attempt in range(self.poll_attempts):
            package, expected_url = self._expected(handle)
            if url != expected_url:
                return "save-failed"
            if len(self._package_headings(tree, package)) != 1:
                return "save-failed"
            messages = self._messages(tree) - baseline_messages
            new_messages = [
                (role, message.casefold()) for role, message in messages.elements()
            ]
            authentication_messages = [
                lowered
                for _, lowered in new_messages
                if any(
                    word in lowered
                    for word in ("authentication", "webauthn", "passkey", "security key")
                )
            ]
            for lowered in authentication_messages:
                if any(
                    phrase in lowered
                    for phrase in (
                        "cancel",
                        "failed",
                        "failure",
                        "error",
                        "unsuccess",
                        "denied",
                        "rejected",
                        "aborted",
                        "expired",
                        "timed out",
                        "timeout",
                        "dismissed",
                    )
                ):
                    return "authentication-failed"
            for lowered in authentication_messages:
                if not re.search(
                    r"\b(approved|complete|completed|succeeded|successful|successfully|verified)\b",
                    lowered,
                ):
                    return "authentication-ambiguous"
            for _, lowered in new_messages:
                if "trusted publisher" in lowered and any(
                    phrase in lowered
                    for phrase in (
                        "unsuccess",
                        "not success",
                        "not saved",
                        "not added",
                        "not configured",
                        "failed",
                        "failure",
                        "error",
                    )
                ):
                    return "save-failed"
            for _, lowered in new_messages:
                if "trusted publisher" in lowered and re.search(
                    r"\b(saved|added|updated|configured|success|successful|successfully)\b",
                    lowered,
                ):
                    return "success"
            if attempt + 1 < self.poll_attempts:
                self.sleeper(self.poll_interval)
                url, tree = self._parse_page(self._axi("snapshot", "--full"))
        return "authentication-ambiguous"

    def reload(self, handle: Any) -> None:
        package, expected_url = self._expected(handle)
        url, tree = self._parse_page(self._axi("selectpage", handle, "--full"))
        if url != expected_url:
            raise HarnessError("package page identity changed")
        self._axi("open", expected_url, "--full")


def _desired_values(publisher: Publisher) -> dict[str, str]:
    return {
        "owner": publisher.owner,
        "repository": publisher.repository,
        "workflow": publisher.workflow,
        "environment": publisher.environment or "",
    }


class Converger:
    def __init__(
        self,
        manifest: Manifest,
        ledger: LedgerStore,
        driver: Any,
        *,
        stop_before_save: bool = False,
    ):
        self.manifest = manifest
        self.ledger = ledger
        self.driver = driver
        self.stop_before_save = stop_before_save
        self.handles: dict[str, Any] = {}

    def _blocked(self, package: str, reason: str, code: int) -> int:
        self.ledger.set(package, "blocked", reason)
        return code

    def run(self) -> int:
        publisher = self.manifest.publisher
        previous = self.manifest.previous_publisher
        for package in self.manifest.packages:
            self.handles[package] = self.driver.open_package(
                package, package_url(package)
            )

        pending: list[tuple[str, str]] = []
        for package in self.manifest.packages:
            prior_status = self.ledger.records[package]["status"]
            observation = self.driver.inspect(
                self.handles[package], package, publisher, previous
            )
            if observation.state == "blocked":
                return self._blocked(package, observation.reason or "ui-drift", 3)
            if observation.state == "exact":
                status = (
                    "saved-verified"
                    if prior_status == "saved-verified"
                    else "exact-match"
                )
                self.ledger.set(package, status)
            elif observation.state == "previous":
                self.ledger.set(package, "pending")
                pending.append((package, "migrate"))
            else:
                if prior_status == "migrating":
                    # A migration edit was in flight when the last run died and
                    # the publisher is now gone. Never repair that ambiguity
                    # automatically.
                    return self._blocked(package, "migration-interrupted", 3)
                self.ledger.set(package, "pending")
                pending.append((package, "create"))

        for package, mode in pending:
            handle = self.handles[package]
            if mode == "migrate":
                if previous is None:
                    return self._blocked(package, "unexpected-publisher", 3)
                self.ledger.set(package, "migrating")
                self.driver.migrate_stage(handle, previous, publisher)
                staged = self.driver.inspect(handle, package, publisher, previous)
                if staged.state != "exact":
                    return self._blocked(package, "partial-save", 4)
            else:
                self.driver.stage(handle, publisher)
                staged = self.driver.inspect(handle, package, publisher, previous)
                if staged.state != "exact":
                    return self._blocked(package, "partial-save", 4)
                self.ledger.set(package, "staged")
                self.ledger.set(package, "awaiting-human-auth")

            if self.stop_before_save:
                # Deliberate pre-authentication stop: the form is staged and
                # verified but nothing was persisted. Resume by rerunning
                # without --stop-before-save.
                return 4

            # Announce the human-authentication step so the operator log warns
            # before every Save; only the human acts on the credential prompt.
            print(
                f"{package}: saving now - complete the security-key prompt in Chrome",
                flush=True,
            )
            outcome = self.driver.save_and_wait(handle)
            if outcome != "success":
                reason = {
                    "authentication-failed": "authentication-failed",
                    "authentication-ambiguous": "authentication-ambiguous",
                    "save-failed": "save-failed",
                }.get(outcome, "harness-error")
                return self._blocked(package, reason, 4)

            self.driver.reload(handle)
            readback = self.driver.inspect(handle, package, publisher, previous)
            if readback.state != "exact":
                return self._blocked(package, "readback-mismatch", 4)
            self.ledger.set(package, "saved-verified")

        for package in self.manifest.packages:
            self.driver.reload(self.handles[package])
            observation = self.driver.inspect(
                self.handles[package], package, publisher, previous
            )
            if observation.state != "exact":
                return self._blocked(package, "readback-mismatch", 4)
        return 0


def _print_ledger(manifest: Manifest, ledger: LedgerStore) -> None:
    for package in manifest.packages:
        record = ledger.records[package]
        suffix = f" ({record['reason']})" if "reason" in record else ""
        print(f"{package}: {record['status']}{suffix}")


def run_axi(
    *,
    manifest_path: str,
    ledger_path: str,
    transport: Any,
    stop_before_save: bool = False,
) -> int:
    try:
        manifest = load_manifest(manifest_path)
        ledger = LedgerStore(ledger_path, manifest)
    except (ManifestError, LedgerError):
        print("blocked: invalid manifest or ledger")
        return 2

    driver = None
    try:
        driver = AxiDriver(transport)
        code = Converger(
            manifest, ledger, driver, stop_before_save=stop_before_save
        ).run()
    except Exception:
        package = next(
            (
                candidate
                for candidate in manifest.packages
                if ledger.records[candidate]["status"]
                not in {"exact-match", "saved-verified"}
            ),
            manifest.packages[0],
        )
        try:
            ledger.set(package, "blocked", "harness-error")
        except LedgerError:
            pass
        code = 5
    finally:
        if driver is not None:
            try:
                driver.close_tabs()
            except Exception:
                pass

    _print_ledger(manifest, ledger)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Converge npm Trusted Publishers through the pinned chrome-devtools-axi CLI."
    )
    parser.add_argument("--manifest", required=True, help="user-approved manifest JSON path")
    parser.add_argument("--ledger", required=True, help="local resume ledger path")
    parser.add_argument(
        "--axi-script",
        default=None,
        help="pinned chrome-devtools-axi.js path (default: vendored install)",
    )
    parser.add_argument("--bun", default="bun", help="bun executable used to run the pinned CLI")
    parser.add_argument(
        "--stop-before-save",
        action="store_true",
        help="stage and verify the first pending package, then stop before Save/authentication",
    )
    parser.add_argument(
        "--debug-classify",
        action="store_true",
        help="print redaction-safe classifier diagnostics (booleans and the public publisher tuple only)",
    )
    arguments = parser.parse_args(argv)
    if arguments.debug_classify:
        global DEBUG_CLASSIFY
        DEBUG_CLASSIFY = True

    try:
        transport = AxiTransport(
            arguments.axi_script or default_axi_script(), bun_path=arguments.bun
        )
    except HarnessError:
        print("blocked: pinned chrome-devtools-axi runtime is unavailable")
        return 5

    return run_axi(
        manifest_path=arguments.manifest,
        ledger_path=arguments.ledger,
        transport=transport,
        stop_before_save=arguments.stop_before_save,
    )


if __name__ == "__main__":
    raise SystemExit(main())
