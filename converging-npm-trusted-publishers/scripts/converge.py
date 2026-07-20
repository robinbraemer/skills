#!/usr/bin/env python3
"""Fail-closed npm Trusted Publisher convergence through a restricted page API."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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
    "saved-verified",
    "blocked",
}
HARNESS_API_KEYS = {
    "new_tab",
    "switch_tab",
    "wait_for_load",
    "page_identity",
    "accessibility_tree",
    "box_model",
    "reload_page",
    "click_at_xy",
    "press_key",
}
FIELD_LABELS = {
    "owner": "Organization or user",
    "repository": "Repository",
    "workflow": "Workflow filename",
    "environment": "Environment name (optional)",
}
BLOCK_REASONS = {
    "unexpected-publisher",
    "identity-mismatch",
    "ui-drift",
    "authentication-failed",
    "authentication-ambiguous",
    "save-failed",
    "partial-save",
    "readback-mismatch",
    "harness-error",
}


class ManifestError(ValueError):
    """The manifest cannot be interpreted without guessing."""


class LedgerError(ValueError):
    """The resume ledger is malformed or belongs to another manifest."""


class HarnessError(RuntimeError):
    """The restricted visible-page interface cannot be used safely."""


@dataclass(frozen=True, slots=True)
class Publisher:
    owner: str
    repository: str
    workflow: str
    environment: str | None
    allowed_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Manifest:
    packages: tuple[str, ...]
    publisher: Publisher


@dataclass(frozen=True, slots=True)
class Observation:
    state: str
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"absent", "exact", "blocked"}:
            raise ValueError("invalid observation state")
        if self.state == "blocked":
            if self.reason not in BLOCK_REASONS:
                raise ValueError("invalid blocked observation reason")
        elif self.reason is not None:
            raise ValueError("only blocked observations may have a reason")


def _exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    actual = set(value)
    unknown = sorted(actual - expected)
    missing = sorted(expected - actual)
    if unknown:
        raise ManifestError(f"unknown {label} keys: {', '.join(unknown)}")
    if missing:
        raise ManifestError(f"missing {label} keys: {', '.join(missing)}")
    return value


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ManifestError(f"{label} must be a non-empty exact string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ManifestError(f"{label} contains a control character")
    return value


def load_manifest(path: str) -> Manifest:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManifestError(f"cannot read manifest: {error.__class__.__name__}") from error

    root = _exact_keys(raw, {"schema_version", "packages", "publisher"}, "manifest")
    if root["schema_version"] != 1 or isinstance(root["schema_version"], bool):
        raise ManifestError("schema_version must equal 1")

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
    owner = _string(publisher_value["owner"], "owner")
    if not OWNER_RE.fullmatch(owner):
        raise ManifestError("owner is not a valid GitHub owner")
    repository = _string(publisher_value["repository"], "repository")
    if not REPOSITORY_RE.fullmatch(repository):
        raise ManifestError("repository is not a valid GitHub repository name")
    workflow = _string(publisher_value["workflow"], "workflow")
    if not WORKFLOW_RE.fullmatch(workflow):
        raise ManifestError("workflow must be a .yml or .yaml filename, not a path")

    environment_value = publisher_value["environment"]
    if environment_value is None:
        environment = None
    else:
        environment = _string(environment_value, "environment")

    actions_value = publisher_value["allowed_actions"]
    if not isinstance(actions_value, list) or not actions_value:
        raise ManifestError("allowed_actions must be a non-empty array")
    if any(not isinstance(action, str) for action in actions_value):
        raise ManifestError("allowed_actions must contain strings")
    if len(set(actions_value)) != len(actions_value):
        raise ManifestError("allowed_actions must not contain duplicates")
    unsupported = sorted(set(actions_value) - set(SUPPORTED_ACTIONS))
    if unsupported:
        raise ManifestError("allowed_actions contains an unsupported action")
    allowed_actions = tuple(action for action in SUPPORTED_ACTIONS if action in actions_value)

    return Manifest(
        packages=tuple(packages),
        publisher=Publisher(
            owner=owner,
            repository=repository,
            workflow=workflow,
            environment=environment,
            allowed_actions=allowed_actions,
        ),
    )


def package_url(package: str) -> str:
    if not PACKAGE_RE.fullmatch(package):
        raise ManifestError("invalid package name")
    return f"https://www.npmjs.com/package/{quote(package, safe='@/')}/access"


def _manifest_data(manifest: Manifest) -> dict[str, Any]:
    publisher = manifest.publisher
    return {
        "schema_version": 1,
        "packages": list(manifest.packages),
        "publisher": {
            "owner": publisher.owner,
            "repository": publisher.repository,
            "workflow": publisher.workflow,
            "environment": publisher.environment,
            "allowed_actions": list(publisher.allowed_actions),
        },
    }


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


def _ax_value(node: dict[str, Any], key: str) -> Any:
    value = node.get(key)
    return value.get("value") if isinstance(value, dict) else None


def _ax_property(node: dict[str, Any], key: str) -> Any:
    properties = node.get("properties")
    if not isinstance(properties, list):
        return None
    matches = [
        item.get("value", {}).get("value")
        for item in properties
        if isinstance(item, dict) and item.get("name") == key
    ]
    return matches[0] if len(matches) == 1 else None


class BrowserHarnessDriver:
    def __init__(
        self,
        api: Mapping[str, Callable[..., Any]],
        *,
        poll_attempts: int = 600,
        poll_interval: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if set(api) != HARNESS_API_KEYS or any(
            not callable(api[key]) for key in HARNESS_API_KEYS
        ):
            raise HarnessError("browser-harness API keys do not match restricted contract")
        if poll_attempts < 1 or poll_interval < 0:
            raise HarnessError("invalid save polling bounds")
        self.api = dict(api)
        self.poll_attempts = poll_attempts
        self.poll_interval = poll_interval
        self.sleeper = sleeper
        self.tabs: list[tuple[Any, str, str]] = []

    def open_package(self, package: str, url: str) -> Any:
        handle = self.api["new_tab"](url)
        self.api["wait_for_load"]()
        self.tabs.append((handle, package, url))
        return handle

    def _activate(self, handle: Any) -> None:
        self.api["switch_tab"](handle)
        self.api["wait_for_load"]()

    def _tree(self) -> list[dict[str, Any]]:
        tree = self.api["accessibility_tree"]()
        if not isinstance(tree, list) or any(not isinstance(node, dict) for node in tree):
            raise HarnessError("accessibility tree has an invalid shape")
        return [node for node in tree if not node.get("ignored", False)]

    @staticmethod
    def _matches(
        tree: list[dict[str, Any]], role: str, name: str
    ) -> list[dict[str, Any]]:
        return [
            node
            for node in tree
            if _ax_value(node, "role") == role and _ax_value(node, "name") == name
        ]

    def _one(
        self, tree: list[dict[str, Any]], role: str, name: str
    ) -> dict[str, Any]:
        matches = self._matches(tree, role, name)
        if len(matches) != 1:
            raise HarnessError(f"ambiguous or missing semantic control: {role}/{name}")
        return matches[0]

    def _form(self, tree: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        controls: dict[str, dict[str, Any]] = {}
        for field, label in FIELD_LABELS.items():
            controls[field] = self._one(tree, "textbox", label)
        for action in SUPPORTED_ACTIONS:
            controls[action] = self._one(tree, "checkbox", action)
        controls["save"] = self._one(tree, "button", "Save")
        for name, control in controls.items():
            if not isinstance(control.get("backendDOMNodeId"), int):
                raise HarnessError("semantic control has no backend node")
            if name != "save" and _ax_property(control, "disabled") is not False:
                raise HarnessError("semantic control enabled state is not explicit")
        for action in SUPPORTED_ACTIONS:
            if not isinstance(_ax_property(controls[action], "checked"), bool):
                raise HarnessError("allowed-action control has no checked state")
        return controls

    def _expected(self, handle: Any) -> tuple[str, str]:
        matches = [
            (package, url)
            for candidate, package, url in self.tabs
            if candidate is handle or candidate == handle
        ]
        if len(matches) != 1:
            raise HarnessError("unknown or ambiguous tab handle")
        return matches[0]

    def _current(
        self, handle: Any
    ) -> tuple[str, list[dict[str, Any]], dict[str, dict[str, Any]]]:
        package, expected_url = self._expected(handle)
        self._activate(handle)
        identity = self.api["page_identity"]()
        if not isinstance(identity, dict) or identity.get("url") != expected_url:
            raise HarnessError("package page identity changed")
        tree = self._tree()
        if len(self._matches(tree, "heading", package)) != 1:
            raise HarnessError("visible package identity changed")
        if len(self._matches(tree, "heading", "Trusted publishing")) != 1:
            raise HarnessError("Trusted publishing section changed")
        return package, tree, self._form(tree)

    def _center(self, node: dict[str, Any]) -> tuple[float, float]:
        model = self.api["box_model"](node["backendDOMNodeId"])
        content = model.get("content") if isinstance(model, dict) else None
        if (
            not isinstance(content, list)
            or len(content) != 8
            or any(not isinstance(value, (int, float)) for value in content)
        ):
            raise HarnessError("semantic control has no current box geometry")
        return (
            sum(content[index] for index in (0, 2, 4, 6)) / 4,
            sum(content[index] for index in (1, 3, 5, 7)) / 4,
        )

    def inspect(
        self, handle: Any, package: str, publisher: Publisher
    ) -> Observation:
        try:
            self._activate(handle)
            identity = self.api["page_identity"]()
            if not isinstance(identity, dict) or identity.get("url") != package_url(package):
                return Observation("blocked", "identity-mismatch")
            tree = self._tree()
            if len(self._matches(tree, "heading", package)) != 1:
                return Observation("blocked", "identity-mismatch")
            if len(self._matches(tree, "heading", "Trusted publishing")) != 1:
                return Observation("blocked", "ui-drift")
            try:
                controls = self._form(tree)
            except HarnessError:
                provider_names = ("GitHub Actions", "GitLab CI/CD", "CircleCI")
                providers = {
                    name: self._one(tree, "button", name) for name in provider_names
                }
                if any(
                    not isinstance(node.get("backendDOMNodeId"), int)
                    or _ax_property(node, "disabled") is not False
                    for node in providers.values()
                ):
                    raise HarnessError("provider selection is unavailable")
                self.api["click_at_xy"](*self._center(providers["GitHub Actions"]))
                self.api["wait_for_load"]()
                tree = self._tree()
                if len(self._matches(tree, "heading", package)) != 1 or len(
                    self._matches(tree, "heading", "Trusted publishing")
                ) != 1:
                    raise HarnessError("provider form changed package identity")
                controls = self._form(tree)
        except HarnessError:
            return Observation("blocked", "ui-drift")

        values = {
            field: _ax_value(controls[field], "value") for field in FIELD_LABELS
        }
        if any(not isinstance(value, str) for value in values.values()):
            return Observation("blocked", "ui-drift")
        checked = {
            action
            for action in SUPPORTED_ACTIONS
            if _ax_property(controls[action], "checked") is True
        }
        if all(value == "" for value in values.values()) and not checked:
            return Observation("absent")
        desired_values = {
            "owner": publisher.owner,
            "repository": publisher.repository,
            "workflow": publisher.workflow,
            "environment": publisher.environment or "",
        }
        if values == desired_values and checked == set(publisher.allowed_actions):
            return Observation("exact")
        return Observation("blocked", "unexpected-publisher")

    def stage(self, handle: Any, publisher: Publisher) -> None:
        desired_values = {
            "owner": publisher.owner,
            "repository": publisher.repository,
            "workflow": publisher.workflow,
            "environment": publisher.environment or "",
        }
        for field, desired in desired_values.items():
            _, _, controls = self._current(handle)
            node = controls[field]
            if _ax_value(node, "value") != "":
                raise HarnessError("textbox is not empty before staging")
            if desired == "":
                continue
            self.api["click_at_xy"](*self._center(node))
            _, _, controls = self._current(handle)
            node = controls[field]
            if _ax_property(node, "focused") is not True:
                raise HarnessError("textbox did not receive visible focus")
            if _ax_value(node, "value") != "":
                raise HarnessError("textbox changed before keyboard input")
            prefix = ""
            for character in desired:
                self.api["press_key"](character)
                prefix += character
                _, _, controls = self._current(handle)
                node = controls[field]
                if _ax_property(node, "focused") is not True:
                    raise HarnessError("textbox lost focus during keyboard input")
                if _ax_value(node, "value") != prefix:
                    raise HarnessError("textbox prefix read-back mismatch")

        desired_actions = set(publisher.allowed_actions)
        for action in SUPPORTED_ACTIONS:
            _, _, controls = self._current(handle)
            checked = _ax_property(controls[action], "checked")
            should_check = action in desired_actions
            if checked is not False:
                raise HarnessError("allowed-action form was not initially empty")
            if should_check:
                self.api["click_at_xy"](*self._center(controls[action]))
                _, _, refreshed = self._current(handle)
                if _ax_property(refreshed[action], "checked") is not True:
                    raise HarnessError("allowed-action read-back mismatch")

        _, _, controls = self._current(handle)
        final_values = {
            field: _ax_value(controls[field], "value") for field in FIELD_LABELS
        }
        final_actions = {
            action
            for action in SUPPORTED_ACTIONS
            if _ax_property(controls[action], "checked") is True
        }
        if final_values != desired_values or final_actions != desired_actions:
            raise HarnessError("staged form read-back mismatch")

    @staticmethod
    def _messages(tree: list[dict[str, Any]]) -> Counter[tuple[str, str]]:
        return Counter(
            (_ax_value(node, "role"), str(_ax_value(node, "name") or ""))
            for node in tree
            if _ax_value(node, "role") in {"alert", "status"}
        )

    def save_and_wait(self, handle: Any) -> str:
        _, tree, controls = self._current(handle)
        save = controls["save"]
        if _ax_property(save, "disabled") is not False:
            return "save-failed"
        baseline_messages = self._messages(tree)
        self.api["click_at_xy"](*self._center(save))

        for attempt in range(self.poll_attempts):
            package, expected_url = self._expected(handle)
            identity = self.api["page_identity"]()
            if not isinstance(identity, dict) or identity.get("url") != expected_url:
                return "save-failed"
            tree = self._tree()
            if len(self._matches(tree, "heading", package)) != 1:
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
                    r"\b(saved|added|configured|success|successful|successfully)\b",
                    lowered,
                ):
                    return "success"
            if attempt + 1 < self.poll_attempts:
                self.sleeper(self.poll_interval)
        return "authentication-ambiguous"

    def reload(self, handle: Any) -> None:
        self._current(handle)
        self.api["reload_page"]()
        self.api["wait_for_load"]()


class Converger:
    def __init__(self, manifest: Manifest, ledger: LedgerStore, driver: Any):
        self.manifest = manifest
        self.ledger = ledger
        self.driver = driver
        self.handles: dict[str, Any] = {}

    def _blocked(self, package: str, reason: str, code: int) -> int:
        self.ledger.set(package, "blocked", reason)
        return code

    def run(self) -> int:
        for package in self.manifest.packages:
            self.handles[package] = self.driver.open_package(
                package, package_url(package)
            )

        pending: list[str] = []
        for package in self.manifest.packages:
            observation = self.driver.inspect(
                self.handles[package], package, self.manifest.publisher
            )
            if observation.state == "blocked":
                return self._blocked(package, observation.reason or "ui-drift", 3)
            if observation.state == "exact":
                prior_status = self.ledger.records[package]["status"]
                status = (
                    "saved-verified"
                    if prior_status == "saved-verified"
                    else "exact-match"
                )
                self.ledger.set(package, status)
            else:
                self.ledger.set(package, "pending")
                pending.append(package)

        for package in pending:
            handle = self.handles[package]
            self.driver.stage(handle, self.manifest.publisher)
            staged = self.driver.inspect(handle, package, self.manifest.publisher)
            if staged.state != "exact":
                return self._blocked(package, "partial-save", 4)
            self.ledger.set(package, "staged")
            self.ledger.set(package, "awaiting-human-auth")

            outcome = self.driver.save_and_wait(handle)
            if outcome != "success":
                reason = {
                    "authentication-failed": "authentication-failed",
                    "authentication-ambiguous": "authentication-ambiguous",
                    "save-failed": "save-failed",
                }.get(outcome, "harness-error")
                return self._blocked(package, reason, 4)

            self.driver.reload(handle)
            readback = self.driver.inspect(handle, package, self.manifest.publisher)
            if readback.state != "exact":
                return self._blocked(package, "readback-mismatch", 4)
            self.ledger.set(package, "saved-verified")

        for package in self.manifest.packages:
            self.driver.reload(self.handles[package])
            observation = self.driver.inspect(
                self.handles[package], package, self.manifest.publisher
            )
            if observation.state != "exact":
                return self._blocked(package, "readback-mismatch", 4)
        return 0


def _print_ledger(manifest: Manifest, ledger: LedgerStore) -> None:
    for package in manifest.packages:
        record = ledger.records[package]
        suffix = f" ({record['reason']})" if "reason" in record else ""
        print(f"{package}: {record['status']}{suffix}")


def run_browser_harness(
    *,
    manifest_path: str,
    ledger_path: str,
    api: Mapping[str, Callable[..., Any]],
) -> int:
    try:
        manifest = load_manifest(manifest_path)
        ledger = LedgerStore(ledger_path, manifest)
    except (ManifestError, LedgerError):
        print("blocked: invalid manifest or ledger")
        return 2

    try:
        driver = BrowserHarnessDriver(api)
        code = Converger(manifest, ledger, driver).run()
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

    _print_ledger(manifest, ledger)
    return code
