import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "converge.py"
SPEC = importlib.util.spec_from_file_location("npm_trust_converge", SCRIPT_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def valid_manifest(**overrides):
    data = {
        "schema_version": 1,
        "packages": ["@example/widgets"],
        "publisher": {
            "owner": "example-org",
            "repository": "widgets",
            "workflow": "release.yml",
            "environment": None,
            "allowed_actions": ["npm publish"],
        },
    }
    data.update(overrides)
    return data


PREVIOUS_TUPLE = {
    "owner": "example-org",
    "repository": "widgets-old",
    "workflow": "publish-old.yml",
    "environment": None,
}


def valid_manifest_v2(**overrides):
    data = valid_manifest(schema_version=2, previous_publisher=dict(PREVIOUS_TUPLE))
    data.update(overrides)
    return data


class ManifestTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return MODULE.load_manifest(str(path))

    def test_loads_exact_manifest(self):
        manifest = self.load(valid_manifest())

        self.assertEqual(manifest.packages, ("@example/widgets",))
        self.assertEqual(manifest.schema_version, 1)
        self.assertEqual(manifest.publisher.owner, "example-org")
        self.assertEqual(manifest.publisher.repository, "widgets")
        self.assertEqual(manifest.publisher.workflow, "release.yml")
        self.assertIsNone(manifest.publisher.environment)
        self.assertEqual(manifest.publisher.allowed_actions, ("npm publish",))
        self.assertIsNone(manifest.previous_publisher)

    def test_rejects_unknown_keys(self):
        data = valid_manifest(extra="not-allowed")

        with self.assertRaisesRegex(MODULE.ManifestError, "unknown manifest keys"):
            self.load(data)

    def test_rejects_invalid_packages_and_workflow_paths(self):
        for package in ("", "Example/Widgets", "@example/widgets?tab=access", "../widgets"):
            with self.subTest(package=package):
                data = valid_manifest(packages=[package])
                with self.assertRaises(MODULE.ManifestError):
                    self.load(data)

        data = valid_manifest()
        data["publisher"]["workflow"] = ".github/workflows/release.yml"
        with self.assertRaisesRegex(MODULE.ManifestError, "workflow"):
            self.load(data)

    def test_rejects_unsupported_actions(self):
        data = valid_manifest()
        data["publisher"]["allowed_actions"] = ["npm publish", "npm deprecate"]

        with self.assertRaisesRegex(MODULE.ManifestError, "allowed_actions"):
            self.load(data)

    def test_package_url_is_canonical(self):
        self.assertEqual(
            MODULE.package_url("@example/widgets"),
            "https://www.npmjs.com/package/@example/widgets/access",
        )

    def test_v2_loads_previous_publisher(self):
        manifest = self.load(valid_manifest_v2())

        self.assertEqual(manifest.schema_version, 2)
        previous = manifest.previous_publisher
        self.assertIsNotNone(previous)
        self.assertEqual(previous.owner, "example-org")
        self.assertEqual(previous.repository, "widgets-old")
        self.assertEqual(previous.workflow, "publish-old.yml")
        self.assertIsNone(previous.environment)
        self.assertEqual(previous.allowed_actions, ("npm publish",))

    def test_v2_without_previous_publisher_is_valid(self):
        manifest = self.load(valid_manifest(schema_version=2))

        self.assertIsNone(manifest.previous_publisher)

    def test_v1_rejects_previous_publisher_field(self):
        data = valid_manifest(previous_publisher=dict(PREVIOUS_TUPLE))

        with self.assertRaisesRegex(MODULE.ManifestError, "unknown manifest keys"):
            self.load(data)

    def test_previous_publisher_must_differ_from_target(self):
        data = valid_manifest_v2(
            previous_publisher={
                "owner": "example-org",
                "repository": "widgets",
                "workflow": "release.yml",
                "environment": None,
            }
        )

        with self.assertRaisesRegex(MODULE.ManifestError, "must differ"):
            self.load(data)

    def test_previous_publisher_allowed_actions_must_match_target_if_present(self):
        matching = valid_manifest_v2()
        matching["previous_publisher"]["allowed_actions"] = ["npm publish"]
        manifest = self.load(matching)
        self.assertEqual(manifest.previous_publisher.allowed_actions, ("npm publish",))

        differing = valid_manifest_v2()
        differing["previous_publisher"]["allowed_actions"] = ["npm stage publish"]
        with self.assertRaisesRegex(MODULE.ManifestError, "allowed_actions"):
            self.load(differing)

    def test_digest_distinguishes_previous_publisher(self):
        with tempfile.TemporaryDirectory() as directory:
            with_previous = Path(directory) / "with.json"
            with_previous.write_text(json.dumps(valid_manifest_v2()), encoding="utf-8")
            without_previous = Path(directory) / "without.json"
            without_previous.write_text(
                json.dumps(valid_manifest(schema_version=2)), encoding="utf-8"
            )
            ledger_path = Path(directory) / "ledger.json"
            MODULE.LedgerStore(str(ledger_path), MODULE.load_manifest(str(with_previous)))

            with self.assertRaisesRegex(MODULE.LedgerError, "manifest"):
                MODULE.LedgerStore(
                    str(ledger_path), MODULE.load_manifest(str(without_previous))
                )


def model_publisher(**overrides):
    data = {
        "owner": "example-org",
        "repository": "widgets",
        "workflow": "release.yml",
        "environment": None,
        "allowed_actions": ("npm publish",),
    }
    data.update(overrides)
    return MODULE.Publisher(**data)


def model_manifest(packages=("@example/widgets",), previous=None):
    return MODULE.Manifest(
        packages=packages,
        publisher=model_publisher(),
        schema_version=2 if previous is not None else 1,
        previous_publisher=previous,
    )


def model_previous():
    return model_publisher(repository="widgets-old", workflow="publish-old.yml")


class FakeDriver:
    def __init__(self, observations, save_outcomes=None):
        self.observations = {
            package: list(states) for package, states in observations.items()
        }
        self.save_outcomes = {
            package: list(outcomes)
            for package, outcomes in (save_outcomes or {}).items()
        }
        self.calls = []

    def open_package(self, package, url):
        self.calls.append(("open", package, url))
        return package

    def inspect(self, handle, package, publisher, previous=None):
        self.calls.append(("inspect", package))
        if not self.observations[package]:
            raise AssertionError(f"no observation queued for {package}")
        return self.observations[package].pop(0)

    def stage(self, handle, publisher):
        self.calls.append(("stage", handle))

    def migrate_stage(self, handle, previous, publisher):
        self.calls.append(("migrate", handle))

    def save_and_wait(self, handle):
        self.calls.append(("save", handle))
        return self.save_outcomes[handle].pop(0)

    def reload(self, handle):
        self.calls.append(("reload", handle))


class LedgerTests(unittest.TestCase):
    def manifest(self, packages=("@example/widgets",)):
        return model_manifest(packages)

    def test_ledger_is_atomic_redacted_and_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            store = MODULE.LedgerStore(str(path), self.manifest())
            store.set("@example/widgets", "blocked", "unexpected-publisher")

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(data), {"schema_version", "manifest_digest", "packages"}
            )
            self.assertEqual(
                data["packages"]["@example/widgets"],
                {"status": "blocked", "reason": "unexpected-publisher"},
            )
            self.assertNotIn("https://", path.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(directory).iterdir()), [path])
            if os.name == "posix":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_ledger_rejects_changed_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            MODULE.LedgerStore(str(path), self.manifest())

            with self.assertRaisesRegex(MODULE.LedgerError, "manifest"):
                MODULE.LedgerStore(
                    str(path), self.manifest(("@example/widgets", "@example/icons"))
                )

    def test_ledger_rejects_unknown_or_sensitive_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            digest = MODULE.manifest_digest(self.manifest())
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "manifest_digest": digest,
                        "packages": {
                            "@example/widgets": {
                                "status": "pending",
                                "url": "https://www.npmjs.com/package/@example/widgets/access",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(MODULE.LedgerError, "record keys"):
                MODULE.LedgerStore(str(path), self.manifest())

    def test_ledger_accepts_migrating_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            store = MODULE.LedgerStore(str(path), self.manifest())
            store.set("@example/widgets", "migrating")

            reopened = MODULE.LedgerStore(str(path), self.manifest())
            self.assertEqual(
                reopened.records["@example/widgets"], {"status": "migrating"}
            )


class ConvergerPreflightTests(unittest.TestCase):
    def run_with(self, manifest, driver, ledger=None):
        if ledger is None:
            directory = tempfile.TemporaryDirectory()
            self.addCleanup(directory.cleanup)
            ledger = MODULE.LedgerStore(
                str(Path(directory.name) / "ledger.json"), manifest
            )
        code = MODULE.Converger(manifest, ledger, driver).run()
        return code, ledger

    def test_exact_match_skips_all_writes(self):
        package = "@example/widgets"
        exact = MODULE.Observation("exact")
        driver = FakeDriver({package: [exact, exact]})

        code, ledger = self.run_with(model_manifest(), driver)

        self.assertEqual(code, 0)
        self.assertFalse(
            any(call[0] in {"stage", "migrate", "save"} for call in driver.calls)
        )
        self.assertEqual(ledger.records[package]["status"], "exact-match")

    def test_unexpected_publisher_stops_before_any_write(self):
        packages = ("@example/widgets", "@example/icons")
        driver = FakeDriver(
            {
                packages[0]: [MODULE.Observation("absent")],
                packages[1]: [
                    MODULE.Observation("blocked", "unexpected-publisher")
                ],
            }
        )

        code, ledger = self.run_with(model_manifest(packages), driver)

        self.assertEqual(code, 3)
        self.assertFalse(
            any(call[0] in {"stage", "migrate", "save"} for call in driver.calls)
        )
        self.assertEqual(
            ledger.records[packages[1]],
            {"status": "blocked", "reason": "unexpected-publisher"},
        )

    def test_identity_mismatch_stops_before_any_write(self):
        package = "@example/widgets"
        driver = FakeDriver(
            {package: [MODULE.Observation("blocked", "identity-mismatch")]}
        )

        code, _ = self.run_with(model_manifest(), driver)

        self.assertEqual(code, 3)
        self.assertFalse(
            any(call[0] in {"stage", "migrate", "save"} for call in driver.calls)
        )

    def test_ui_drift_stops_before_any_write(self):
        package = "@example/widgets"
        driver = FakeDriver({package: [MODULE.Observation("blocked", "ui-drift")]})

        code, _ = self.run_with(model_manifest(), driver)

        self.assertEqual(code, 3)
        self.assertFalse(
            any(call[0] in {"stage", "migrate", "save"} for call in driver.calls)
        )

    def test_previous_match_converges_through_migration(self):
        package = "@example/widgets"
        exact = MODULE.Observation("exact")
        driver = FakeDriver(
            {package: [MODULE.Observation("previous"), exact, exact, exact]},
            {package: ["success"]},
        )

        code, ledger = self.run_with(model_manifest(previous=model_previous()), driver)

        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in driver.calls].count("migrate"), 1)
        self.assertNotIn("stage", [call[0] for call in driver.calls])
        self.assertEqual(ledger.records[package]["status"], "saved-verified")

    def test_interrupted_migration_blocks_on_absent_resume(self):
        package = "@example/widgets"
        manifest = model_manifest(previous=model_previous())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ledger = MODULE.LedgerStore(str(Path(directory.name) / "ledger.json"), manifest)
        ledger.set(package, "migrating")
        driver = FakeDriver({package: [MODULE.Observation("absent")]})

        code, ledger = self.run_with(manifest, driver, ledger=ledger)

        self.assertEqual(code, 3)
        self.assertFalse(
            any(call[0] in {"stage", "migrate", "save"} for call in driver.calls)
        )
        self.assertEqual(
            ledger.records[package],
            {"status": "blocked", "reason": "migration-interrupted"},
        )

    def test_interrupted_migration_resumes_when_previous_is_intact(self):
        package = "@example/widgets"
        manifest = model_manifest(previous=model_previous())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ledger = MODULE.LedgerStore(str(Path(directory.name) / "ledger.json"), manifest)
        ledger.set(package, "migrating")
        exact = MODULE.Observation("exact")
        driver = FakeDriver(
            {package: [MODULE.Observation("previous"), exact, exact, exact]},
            {package: ["success"]},
        )

        code, ledger = self.run_with(manifest, driver, ledger=ledger)

        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in driver.calls].count("migrate"), 1)
        self.assertEqual(ledger.records[package]["status"], "saved-verified")

    def test_interrupted_migration_resumes_when_target_is_already_saved(self):
        package = "@example/widgets"
        manifest = model_manifest(previous=model_previous())
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ledger = MODULE.LedgerStore(str(Path(directory.name) / "ledger.json"), manifest)
        ledger.set(package, "migrating")
        exact = MODULE.Observation("exact")
        driver = FakeDriver({package: [exact, exact]})

        code, ledger = self.run_with(manifest, driver, ledger=ledger)

        self.assertEqual(code, 0)
        self.assertFalse(
            any(call[0] in {"stage", "migrate", "save"} for call in driver.calls)
        )
        self.assertEqual(ledger.records[package]["status"], "exact-match")


PACKAGE_URL = "https://www.npmjs.com/package/@example/widgets/access"
FIELD_LABELS = {
    "owner": "Organization or user*",
    "repository": "Repository*",
    "workflow": "Workflow filename*",
    "environment": "Environment name",
}
FIELD_IDS = {
    "Organization or user*": "1_10",
    "Repository*": "1_11",
    "Workflow filename*": "1_12",
    "Environment name": "1_13",
}
ACTION_IDS = {"Allow npm publish": "1_20", "Allow npm stage publish": "1_21"}
SAVE_ID = "1_30"
CANCEL_ID = "1_31"
COMBO_ID = "1_35"
EDIT_ID = "2_10"
DELETE_ID = "2_11"
SAVE_LABEL = "Save changes to trusted publisher connection"
DELETE_LABEL = "Delete OIDC trusted publisher connection"


def summary_state(
    owner="example-org",
    repository="widgets-old",
    workflow="publish-old.yml",
    actions=("npm publish",),
):
    return {
        "owner": owner,
        "repository": repository,
        "workflow": workflow,
        "actions": set(actions),
    }


class FakeAxiTransport:
    """In-memory double of the pinned chrome-devtools-axi CLI for one npm page.

    Mirrors the real deployed access page: a summary view for an existing
    connection (StaticText tuple, Edit button with tuple description, Delete
    button) and the create/edit form (asterisked labels, Allow-prefixed
    checkboxes, empty textboxes rendered without a value attribute). Output is
    real-format: TOON page/error blocks, `snapshot:` sections with
    generation-stamped `uid=g<N>:<id>` refs, `pages[...]` tables whose
    url/selected columns are unreliable, and STALE_REF errors for old refs.
    """

    def __init__(
        self,
        *,
        url=PACKAGE_URL,
        package_heading=(
            "@example/widgets TypeScript icon, indicating that this package "
            "has built-in type declarations"
        ),
        section_heading="Trusted Publisher",
        publisher_state=None,
        provider_text="GitHub Actions",
        save_result="success",
        commit_on_save=True,
        corrupt_prefix=False,
        corrupt_backspace=False,
        save_disabled=False,
        stale_success=False,
        auth_status_message=None,
        stale_clicks=0,
        garbage_selectpage=False,
        missing_field=None,
        duplicate_field=None,
        disabled_field=None,
        edit_prefill_override=None,
    ):
        self.url = url
        self.package_heading = package_heading
        self.section_heading = section_heading
        self.publisher_state = publisher_state
        self.provider_text = provider_text
        self.save_result = save_result
        self.commit_on_save = commit_on_save
        self.corrupt_prefix = corrupt_prefix
        self.corrupt_backspace = corrupt_backspace
        self.save_disabled = save_disabled
        self.stale_success = stale_success
        self.auth_status_message = auth_status_message
        self.stale_clicks_remaining = stale_clicks
        self.garbage_selectpage = garbage_selectpage
        self.missing_field = missing_field
        self.duplicate_field = duplicate_field
        self.disabled_field = disabled_field
        self.edit_prefill_override = edit_prefill_override
        self.editing = False
        self.values = {label: "" for label in FIELD_IDS}
        self.checked = set()
        self.focused = None
        self.save_clicked = False
        self.delete_clicked = False
        self.generation = 0
        self.pages = {"0": "about:blank"}
        self.selected = "0"
        self.next_page_id = 1
        self.calls = []

    # --- CLI dispatch ---

    def run(self, *args):
        self.calls.append(tuple(args))
        command = args[0]
        if command == "newpage":
            page_id = str(self.next_page_id)
            self.next_page_id += 1
            self.pages[page_id] = args[1]
            self.selected = page_id
            return self._page_output()
        if command == "pages":
            # Mirror the pinned CLI's real output: the url column carries a
            # title fragment and the selected flag misparses to false, so any
            # driver reliance on those columns fails these tests.
            rows = [f"  {page_id},npm,false" for page_id in self.pages]
            body = "\n".join(
                [f"pages[{len(rows)}]{{id,url,selected}}:", *rows, "help[1]:",
                 "  Run `chrome-devtools-axi selectpage <id>` to switch tabs"]
            )
            return MODULE.AxiResult(0, body + "\n")
        if command == "selectpage":
            if args[1] not in self.pages:
                return self._error("No page with ID " + args[1], "VALIDATION_ERROR", 2)
            self.selected = args[1]
            if self.garbage_selectpage:
                return MODULE.AxiResult(0, "page:\n  refs: 0\nnonsense without a tree\n")
            return self._page_output()
        if command == "snapshot":
            return self._page_output()
        if command == "open":
            self.pages[self.selected] = args[1]
            self.focused = None
            self.save_clicked = False
            self.editing = False
            self.values = {label: "" for label in FIELD_IDS}
            self.checked = set()
            return self._page_output()
        if command == "click":
            return self._click(args[1])
        if command == "press":
            self._press(args[1])
            return self._page_output()
        return self._error(f"Unknown command: {command}", "VALIDATION_ERROR", 2)

    # --- interaction semantics ---

    def _in_form(self):
        return self.editing or self.publisher_state is None

    def _click(self, ref):
        if self.stale_clicks_remaining > 0:
            self.stale_clicks_remaining -= 1
            return self._stale_error(ref)
        stripped = ref[1:] if ref.startswith("@") else ref
        generation, _, node_id = stripped.partition(":")
        if generation != f"g{self.generation}":
            return self._stale_error(ref)
        if node_id == EDIT_ID and not self._in_form():
            state = self.edit_prefill_override or self.publisher_state
            self.editing = True
            self.values = {
                FIELD_LABELS["owner"]: state["owner"],
                FIELD_LABELS["repository"]: state["repository"],
                FIELD_LABELS["workflow"]: state["workflow"],
                FIELD_LABELS["environment"]: "",
            }
            self.checked = set(state["actions"])
            return self._page_output()
        if node_id == DELETE_ID:
            self.delete_clicked = True
            self.publisher_state = None
            return self._page_output()
        for label, candidate in FIELD_IDS.items():
            if node_id == candidate:
                self.focused = label
                return self._page_output()
        for label, candidate in ACTION_IDS.items():
            if node_id == candidate:
                action = label.removeprefix("Allow ")
                self.checked.symmetric_difference_update({action})
                return self._page_output()
        if node_id == SAVE_ID and not self.save_disabled:
            self.save_clicked = True
            if self.save_result == "success" and self.commit_on_save:
                self.publisher_state = {
                    "owner": self.values[FIELD_LABELS["owner"]],
                    "repository": self.values[FIELD_LABELS["repository"]],
                    "workflow": self.values[FIELD_LABELS["workflow"]],
                    "actions": set(self.checked),
                }
                self.editing = False
            return self._page_output()
        return self._page_output()

    def _press(self, key):
        if self.focused is None:
            return
        if key == "End":
            return
        if key == "Backspace":
            cut = 2 if self.corrupt_backspace else 1
            self.values[self.focused] = self.values[self.focused][:-cut]
            return
        if len(key) != 1:
            return
        value = self.values[self.focused] + key
        if self.corrupt_prefix and not self.values[self.focused]:
            value = "!"
        self.values[self.focused] = value

    # --- output rendering ---

    def _error(self, message, code, exit_code=1):
        return MODULE.AxiResult(exit_code, f'error: "{message}"\ncode: {code}\n')

    def _stale_error(self, ref):
        return MODULE.AxiResult(
            1,
            f'error: "Stale ref {ref}: from an older snapshot generation. '
            'Re-snapshot to get fresh refs."\n'
            "code: STALE_REF\n"
            "help[1]:\n"
            "  Run `chrome-devtools-axi snapshot` to get fresh refs\n",
        )

    def _uid(self, node_id):
        return f"uid=g{self.generation}:{node_id}"

    def _form_lines(self):
        lines = [
            f'  {self._uid(COMBO_ID)} combobox "Publisher*" expandable haspopup="menu"',
            f'  {self._uid("1_36")} StaticText "{self.provider_text}"',
        ]
        for label in FIELD_IDS:
            if label == self.missing_field:
                continue
            value = f' value="{self.values[label]}"' if self.values[label] else ""
            focused = " focused" if self.focused == label else ""
            disabled = " disabled" if label == self.disabled_field else ""
            lines.append(
                f'  {self._uid(FIELD_IDS[label])} textbox "{label}"'
                f"{value} focusable{focused}{disabled}"
            )
            if label == self.duplicate_field:
                lines.append(f'  {self._uid("1_99")} textbox "{label}" focusable')
        for label, node_id in ACTION_IDS.items():
            checked = " checked" if label.removeprefix("Allow ") in self.checked else ""
            lines.append(f'  {self._uid(node_id)} checkbox "{label}"{checked}')
        save_state = " disableable disabled" if self.save_disabled else ""
        lines.append(f'  {self._uid(SAVE_ID)} button "{SAVE_LABEL}"{save_state}')
        lines.append(
            f'  {self._uid(CANCEL_ID)} button "Cancel trusted publisher setup"'
        )
        return lines

    def _summary_lines(self):
        state = self.publisher_state
        coordinate = f"{state['owner']}/{state['repository']}"
        lines = [
            f'  {self._uid("2_1")} StaticText "{coordinate}"',
            f'  {self._uid("2_2")} StaticText "{state["workflow"]}"',
            f'  {self._uid("2_3")} StaticText "Permissions: "',
        ]
        for index, action in enumerate(sorted(state["actions"])):
            lines.append(f'  {self._uid(f"2_{4 + index}")} StaticText "{action}"')
        lines.append(
            f'  {self._uid(EDIT_ID)} button "Edit" '
            f'description="{coordinate} {state["workflow"]}"'
        )
        lines.append(f'  {self._uid(DELETE_ID)} button "{DELETE_LABEL}"')
        return lines

    def _chrome_prefix_lines(self):
        # Faithful replica of the live access page chrome, including LineBreak
        # nodes whose accessible name is a literal newline (rendered across two
        # physical lines by the real formatter) and named/empty live regions.
        return [
            f'  {self._uid("5_1")} region "Site notifications"',
            f'    {self._uid("5_2")} StaticText "⚠️"',
            f'    {self._uid("5_3")} alert atomic live="assertive" relevant="additions text"',
            f'      {self._uid("5_4")} StaticText "npm tokens that bypass 2FA are being restricted — account changes (Aug 2026) and direct publishing (Jan 2027). "',
            f'      {self._uid("5_5")} link "Learn how to prepare for the npm bypass 2FA token deprecation" url="https://github.blog/changelog/example"',
            f'    {self._uid("5_6")} button "Close notification"',
            f'  {self._uid("5_7")} link "skip to content" url="{self.url}#main"',
            f'  {self._uid("5_8")} StaticText "npm"',
            f'  {self._uid("5_9")} link "Npm" url="https://www.npmjs.com/"',
            f'  {self._uid("5_10")} form',
            f'    {self._uid("5_11")} combobox "Search packages" expandable haspopup="listbox"',
            f'    {self._uid("5_12")} generic atomic live="polite" relevant="additions text"',
            f'    {self._uid("5_13")} button "Search"',
            f'  {self._uid("5_14")} navigation',
            f'    {self._uid("5_15")} button "Profile menu"',
            f'  {self._uid("5_16")} main',
        ]

    def _chrome_tabs_lines(self):
        return [
            f'    {self._uid("5_20")} StaticText "0.8.25"',
            f'    {self._uid("5_21")} StaticText " • "',
            f'    {self._uid("5_22")} StaticText "Public"',
            f'    {self._uid("5_23")} tab " Readme" selectable',
            f'    {self._uid("5_24")} tab "Code Beta" selectable',
            f'    {self._uid("5_25")} tab "4 Dependencies" selectable',
            f'    {self._uid("5_26")} tab "0 Dependents" selectable',
            f'    {self._uid("5_27")} tab "26 Versions" selectable',
            f'    {self._uid("5_28")} tab " Settings" selectable selected',
            f'    {self._uid("5_29")} tabpanel " Readme"',
        ]

    def _section_intro_lines(self):
        return [
            f'    {self._uid("5_40")} StaticText "Establish a trust between your package and your repository using OpenID Connect (OIDC)."',
            f'    {self._uid("5_41")} LineBreak "\n"',
            f'    {self._uid("5_42")} link "Learn more about OpenID Connect." url="https://gh.io/npm-docs-trusted-publishers"',
            f'      {self._uid("5_43")} StaticText "Learn more about OpenID Connect."',
        ]

    def _chrome_suffix_lines(self):
        return [
            f'    {self._uid("5_50")} heading "Package access" level="2"',
            f'    {self._uid("5_51")} StaticText "Status:"',
            f'    {self._uid("5_52")} StaticText "public"',
            f'    {self._uid("5_53")} form',
            f'      {self._uid("5_54")} heading "Publishing access" level="2"',
            f'      {self._uid("5_55")} radio "Require two-factor authentication and disallow bypass 2fa tokens (recommended)"',
            f'      {self._uid("5_56")} LineBreak "\n"',
            f'      {self._uid("5_57")} radio "Require two-factor authentication or a granular access token with bypass 2fa enabled" checked',
            f'      {self._uid("5_58")} LineBreak "\n"',
            f'      {self._uid("5_59")} StaticText "Note about trusted publishers"',
            f'      {self._uid("5_60")} StaticText ": All publishing access options above are compatible with OIDC trusted publishers. If you have configured trusted publishers for this package, they will continue to work regardless of which option you select."',
            f'      {self._uid("5_61")} button "Update Package Settings"',
            f'    {self._uid("5_62")} heading "Maintainers 1" level="2"',
            f'    {self._uid("5_63")} heading "Package Sidebar" level="2"',
            f'    {self._uid("5_64")} heading "Install" level="3"',
            f'    {self._uid("5_65")} StaticText "@example/widgets"',
            f'    {self._uid("5_66")} heading "Repository" level="3"',
            f'    {self._uid("5_67")} heading "Version" level="3"',
        ]

    def _tree_lines(self):
        lines = [
            f'{self._uid("1_0")} RootWebArea "npm" url="{self.url}" focusable focused'
        ]
        lines.extend(self._chrome_prefix_lines())
        if self.package_heading is not None:
            lines.append(
                f'    {self._uid("1_1")} heading "{self.package_heading}" level="1"'
            )
        lines.extend(self._chrome_tabs_lines())
        if self.section_heading is not None:
            lines.append(
                f'    {self._uid("1_2")} heading "{self.section_heading}" level="1"'
            )
        lines.extend(self._section_intro_lines())
        if self._in_form():
            lines.extend(self._form_lines())
        else:
            lines.extend(self._summary_lines())
        lines.extend(self._message_lines())
        lines.extend(self._chrome_suffix_lines())
        return lines

    def _message_lines(self):
        nodes = []
        if self.save_clicked and self.auth_status_message is not None:
            nodes = [
                ("status", "Trusted publisher saved successfully"),
                ("status", self.auth_status_message),
            ]
        elif self.save_clicked and self.save_result in {
            "positive-then-negative",
            "negative-then-positive",
            "status-auth-negative",
        }:
            positive = ("status", "Trusted publisher saved successfully")
            negative = (
                "status" if self.save_result == "status-auth-negative" else "alert",
                "Authentication canceled",
            )
            nodes = (
                [positive, negative]
                if self.save_result in {"positive-then-negative", "status-auth-negative"}
                else [negative, positive]
            )
        elif self.stale_success or (self.save_clicked and self.save_result == "success"):
            nodes = [("status", "Trusted publisher saved successfully")]
        elif self.save_clicked and self.save_result == "authentication-failed":
            nodes = [("alert", "Authentication canceled")]
        elif self.save_clicked and self.save_result == "save-failed":
            nodes = [("alert", "Trusted publisher save failed")]
        elif self.save_clicked and self.save_result == "negative-success":
            nodes = [("status", "Trusted publisher save unsuccessful")]
        return [
            f'  {self._uid(f"1_5{index}")} {role} "{message}"'
            for index, (role, message) in enumerate(nodes)
        ]

    def _page_output(self):
        self.generation += 1
        tree = self._tree_lines()
        body = "\n".join(
            [
                "page:",
                '  title: "npm"',
                f"  refs: {len(tree)}",
                "snapshot:",
                *tree,
                "help[1]:",
                "  Run `chrome-devtools-axi snapshot` to re-orient",
            ]
        )
        return MODULE.AxiResult(0, body + "\n")


class RaisingTransport:
    def run(self, *args):
        raise RuntimeError("sensitive browser detail")


def make_driver(transport, poll_attempts=1):
    return MODULE.AxiDriver(
        transport, poll_attempts=poll_attempts, sleeper=lambda _: None
    )


def open_widget(driver):
    return driver.open_package("@example/widgets", PACKAGE_URL)


class AxiAdapterContractTests(unittest.TestCase):
    def test_absent_form_requires_github_actions_provider(self):
        fake = FakeAxiTransport()
        driver = make_driver(fake)
        handle = open_widget(driver)
        observation = driver.inspect(
            handle, "@example/widgets", model_publisher()
        )
        self.assertEqual(observation, MODULE.Observation("absent"))

        drifted = FakeAxiTransport(provider_text="GitLab CI/CD")
        driver = make_driver(drifted)
        handle = open_widget(driver)
        observation = driver.inspect(
            handle, "@example/widgets", model_publisher()
        )
        self.assertEqual(observation, MODULE.Observation("blocked", "ui-drift"))

    def test_adapter_rejects_transport_without_run_command(self):
        with self.assertRaisesRegex(MODULE.HarnessError, "run command"):
            MODULE.AxiDriver(object())

    def test_adapter_refuses_non_allowlisted_commands(self):
        driver = make_driver(FakeAxiTransport())
        for forbidden in (
            ("eval", "document.title"),
            ("fill", "@g1:1_10", "example-org"),
            ("fillform", "@g1:1_10=example-org"),
            ("type", "example-org"),
            ("screenshot", "page.png"),
            ("network",),
            ("console",),
            ("update",),
        ):
            with self.subTest(command=forbidden[0]):
                with self.assertRaisesRegex(MODULE.HarnessError, "allowlisted"):
                    driver._axi(*forbidden)

    def test_adapter_refuses_non_allowlisted_clearing_keys(self):
        driver = make_driver(FakeAxiTransport())
        for key in ("Delete", "Meta+A", "Escape", "Tab"):
            with self.subTest(key=key):
                with self.assertRaisesRegex(MODULE.HarnessError, "allowlisted"):
                    driver._press_named(key)

    def test_full_run_only_issues_allowlisted_commands(self):
        fake = FakeAxiTransport()
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()):
                code = MODULE.run_axi(
                    manifest_path=str(manifest),
                    ledger_path=str(Path(directory) / "ledger.json"),
                    transport=fake,
                )

        self.assertEqual(code, 0)
        issued = {call[0] for call in fake.calls}
        self.assertLessEqual(issued, set(MODULE.AXI_ALLOWED_COMMANDS))
        self.assertFalse(issued & {"eval", "run", "fill", "fillform", "type"})

    def test_adapter_uses_page_ids_as_opaque_handles(self):
        fake = FakeAxiTransport()
        driver = make_driver(fake)

        handle = open_widget(driver)

        self.assertEqual(handle, "1")
        self.assertEqual(fake.calls[0], ("pages",))
        self.assertEqual(fake.calls[1], ("newpage", PACKAGE_URL, "--full"))

    def test_adapter_rejects_wrong_origin_path_or_package_identity(self):
        for url in (
            "https://example.invalid/package/@example/widgets/access",
            "https://www.npmjs.com/package/@example/icons/access",
        ):
            with self.subTest(url=url):
                fake = FakeAxiTransport(url=url)
                driver = make_driver(fake)
                handle = open_widget(driver)
                observation = driver.inspect(
                    handle, "@example/widgets", model_publisher()
                )
                self.assertEqual(
                    observation,
                    MODULE.Observation("blocked", "identity-mismatch"),
                )

        fake = FakeAxiTransport(package_heading=None)
        driver = make_driver(fake)
        handle = open_widget(driver)
        observation = driver.inspect(handle, "@example/widgets", model_publisher())
        self.assertEqual(observation.reason, "identity-mismatch")

    def test_package_heading_with_icon_alt_suffix_is_accepted(self):
        fake = FakeAxiTransport(package_heading="@example/widgets")
        driver = make_driver(fake)
        handle = open_widget(driver)
        observation = driver.inspect(handle, "@example/widgets", model_publisher())
        self.assertEqual(observation, MODULE.Observation("absent"))

        lookalike = FakeAxiTransport(package_heading="@example/widgets-extra")
        driver = make_driver(lookalike)
        handle = open_widget(driver)
        observation = driver.inspect(handle, "@example/widgets", model_publisher())
        self.assertEqual(observation.reason, "identity-mismatch")

    def test_adapter_rejects_ambiguous_missing_or_disabled_semantic_controls(self):
        variants = (
            {"missing_field": "Workflow filename*"},
            {"duplicate_field": "Repository*"},
            {"disabled_field": "Organization or user*"},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                fake = FakeAxiTransport(**variant)
                driver = make_driver(fake)
                handle = open_widget(driver)
                observation = driver.inspect(
                    handle, "@example/widgets", model_publisher()
                )
                self.assertEqual(observation, MODULE.Observation("blocked", "ui-drift"))


class SummaryClassificationTests(unittest.TestCase):
    def observe(self, state, previous=None):
        fake = FakeAxiTransport(publisher_state=state)
        driver = make_driver(fake)
        handle = open_widget(driver)
        return fake, driver.inspect(
            handle, "@example/widgets", model_publisher(), previous
        )

    def test_summary_matching_target_is_exact(self):
        state = summary_state(repository="widgets", workflow="release.yml")
        _, observation = self.observe(state)
        self.assertEqual(observation, MODULE.Observation("exact"))

    def test_summary_matching_target_with_wrong_actions_is_unexpected(self):
        state = summary_state(
            repository="widgets",
            workflow="release.yml",
            actions=("npm publish", "npm stage publish"),
        )
        _, observation = self.observe(state)
        self.assertEqual(
            observation, MODULE.Observation("blocked", "unexpected-publisher")
        )

    def test_summary_matching_previous_is_previous_only_with_approval(self):
        state = summary_state()
        _, with_approval = self.observe(state, previous=model_previous())
        self.assertEqual(with_approval, MODULE.Observation("previous"))

        _, without_approval = self.observe(summary_state())
        self.assertEqual(
            without_approval, MODULE.Observation("blocked", "unexpected-publisher")
        )

    def test_summary_matching_neither_tuple_is_unexpected(self):
        state = summary_state(owner="other-org", repository="legacy")
        fake, observation = self.observe(state, previous=model_previous())
        self.assertEqual(
            observation, MODULE.Observation("blocked", "unexpected-publisher")
        )
        self.assertFalse(any(call[0] == "click" for call in fake.calls))

    def test_summary_with_non_null_previous_environment_never_matches(self):
        previous = model_publisher(
            repository="widgets-old", workflow="publish-old.yml", environment="prod"
        )
        _, observation = self.observe(summary_state(), previous=previous)
        self.assertEqual(
            observation, MODULE.Observation("blocked", "unexpected-publisher")
        )


class AxiInteractionTests(unittest.TestCase):
    def staged_driver(self, fake, poll_attempts=1):
        driver = make_driver(fake, poll_attempts=poll_attempts)
        handle = open_widget(driver)
        return driver, handle

    def test_text_entry_uses_press_per_character_with_prefix_readback(self):
        fake = FakeAxiTransport()
        driver, handle = self.staged_driver(fake)

        driver.stage(handle, model_publisher())

        typed = "".join(
            call[1] for call in fake.calls if call[0] == "press" and len(call[1]) == 1
        )
        self.assertEqual(typed, "example-orgwidgetsrelease.yml")
        self.assertEqual(fake.values["Organization or user*"], "example-org")
        self.assertEqual(fake.values["Repository*"], "widgets")
        self.assertEqual(fake.values["Workflow filename*"], "release.yml")

    def test_text_entry_stops_on_prefix_mismatch(self):
        fake = FakeAxiTransport(corrupt_prefix=True)
        driver, handle = self.staged_driver(fake)

        with self.assertRaisesRegex(MODULE.HarnessError, "prefix"):
            driver.stage(handle, model_publisher())

        self.assertEqual(len([call for call in fake.calls if call[0] == "press"]), 1)

    def test_actions_click_only_desired_unchecked_controls(self):
        fake = FakeAxiTransport()
        driver, handle = self.staged_driver(fake)

        driver.stage(handle, model_publisher())

        self.assertEqual(fake.checked, {"npm publish"})
        action_clicks = [
            call
            for call in fake.calls
            if call[0] == "click"
            and call[1].split(":", 1)[1] in set(ACTION_IDS.values())
        ]
        self.assertEqual(len(action_clicks), 1)

    def test_disabled_save_stops(self):
        fake = FakeAxiTransport(save_disabled=True)
        driver, handle = self.staged_driver(fake)

        driver.stage(handle, model_publisher())
        outcome = driver.save_and_wait(handle)

        self.assertEqual(outcome, "save-failed")
        self.assertFalse(fake.save_clicked)

    def test_save_wait_requires_visible_success(self):
        for result, expected in (
            ("success", "success"),
            ("authentication-failed", "authentication-failed"),
            ("negative-success", "save-failed"),
            ("positive-then-negative", "authentication-failed"),
            ("negative-then-positive", "authentication-failed"),
            ("status-auth-negative", "authentication-failed"),
            ("none", "authentication-ambiguous"),
        ):
            with self.subTest(result=result):
                fake = FakeAxiTransport(save_result=result, commit_on_save=False)
                driver, handle = self.staged_driver(fake, poll_attempts=2)
                driver.stage(handle, model_publisher())
                self.assertEqual(driver.save_and_wait(handle), expected)

    def test_authentication_status_cannot_be_overridden_by_publisher_success(self):
        for message, expected in (
            ("Authentication unsuccessful", "authentication-failed"),
            ("Passkey denied", "authentication-failed"),
            ("Security key rejected", "authentication-failed"),
            ("WebAuthn pending", "authentication-ambiguous"),
            ("Authentication verified", "success"),
        ):
            with self.subTest(message=message):
                fake = FakeAxiTransport(
                    auth_status_message=message, commit_on_save=False
                )
                driver, handle = self.staged_driver(fake)
                driver.stage(handle, model_publisher())
                self.assertEqual(driver.save_and_wait(handle), expected)

    def test_save_wait_rejects_stale_success(self):
        fake = FakeAxiTransport(save_result="none", stale_success=True)
        driver, handle = self.staged_driver(fake, poll_attempts=2)
        driver.stage(handle, model_publisher())

        outcome = driver.save_and_wait(handle)

        self.assertEqual(outcome, "authentication-ambiguous")


class MigrationTests(unittest.TestCase):
    def write_manifest(self, directory, data):
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def run_entrypoint(self, fake, manifest_data, prepare_ledger=None):
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = self.write_manifest(directory, manifest_data)
            ledger_path = Path(directory) / "ledger.json"
            if prepare_ledger is not None:
                manifest = MODULE.load_manifest(str(manifest_path))
                ledger = MODULE.LedgerStore(str(ledger_path), manifest)
                prepare_ledger(ledger)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = MODULE.run_axi(
                    manifest_path=str(manifest_path),
                    ledger_path=str(ledger_path),
                    transport=fake,
                )
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
        return code, data, output.getvalue()

    def test_matched_previous_is_migrated_in_place_without_delete(self):
        fake = FakeAxiTransport(publisher_state=summary_state())
        code, ledger, _ = self.run_entrypoint(fake, valid_manifest_v2())

        self.assertEqual(code, 0)
        self.assertEqual(
            ledger["packages"]["@example/widgets"]["status"], "saved-verified"
        )
        self.assertFalse(fake.delete_clicked)
        self.assertEqual(
            fake.publisher_state,
            {
                "owner": "example-org",
                "repository": "widgets",
                "workflow": "release.yml",
                "actions": {"npm publish"},
            },
        )
        pressed = [call[1] for call in fake.calls if call[0] == "press"]
        self.assertIn("End", pressed)
        self.assertIn("Backspace", pressed)

    def test_mismatched_existing_publisher_still_blocks(self):
        fake = FakeAxiTransport(
            publisher_state=summary_state(owner="other-org", repository="legacy")
        )
        code, ledger, _ = self.run_entrypoint(fake, valid_manifest_v2())

        self.assertEqual(code, 3)
        self.assertEqual(
            ledger["packages"]["@example/widgets"],
            {"status": "blocked", "reason": "unexpected-publisher"},
        )
        self.assertFalse(any(call[0] == "click" for call in fake.calls))
        self.assertFalse(fake.editing)

    def test_v1_manifest_never_migrates_an_existing_publisher(self):
        fake = FakeAxiTransport(publisher_state=summary_state())
        code, ledger, _ = self.run_entrypoint(fake, valid_manifest())

        self.assertEqual(code, 3)
        self.assertEqual(
            ledger["packages"]["@example/widgets"],
            {"status": "blocked", "reason": "unexpected-publisher"},
        )
        self.assertFalse(any(call[0] == "click" for call in fake.calls))

    def test_interrupted_migration_fails_closed_when_publisher_is_gone(self):
        fake = FakeAxiTransport(publisher_state=None)
        code, ledger, _ = self.run_entrypoint(
            fake,
            valid_manifest_v2(),
            prepare_ledger=lambda ledger: ledger.set("@example/widgets", "migrating"),
        )

        self.assertEqual(code, 3)
        self.assertEqual(
            ledger["packages"]["@example/widgets"],
            {"status": "blocked", "reason": "migration-interrupted"},
        )
        self.assertFalse(any(call[0] == "press" for call in fake.calls))
        self.assertFalse(any(call[0] == "click" for call in fake.calls))

    def test_interrupted_migration_resumes_when_previous_survived(self):
        fake = FakeAxiTransport(publisher_state=summary_state())
        code, ledger, _ = self.run_entrypoint(
            fake,
            valid_manifest_v2(),
            prepare_ledger=lambda ledger: ledger.set("@example/widgets", "migrating"),
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            ledger["packages"]["@example/widgets"]["status"], "saved-verified"
        )
        self.assertFalse(fake.delete_clicked)

    def test_interrupted_migration_resumes_when_target_already_saved(self):
        fake = FakeAxiTransport(
            publisher_state=summary_state(repository="widgets", workflow="release.yml")
        )
        code, ledger, _ = self.run_entrypoint(
            fake,
            valid_manifest_v2(),
            prepare_ledger=lambda ledger: ledger.set("@example/widgets", "migrating"),
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            ledger["packages"]["@example/widgets"]["status"], "exact-match"
        )
        self.assertFalse(any(call[0] == "click" for call in fake.calls))

    def test_migration_readback_must_show_the_target_tuple(self):
        fake = FakeAxiTransport(publisher_state=summary_state(), commit_on_save=False)
        code, ledger, _ = self.run_entrypoint(fake, valid_manifest_v2())

        self.assertEqual(code, 4)
        self.assertEqual(
            ledger["packages"]["@example/widgets"],
            {"status": "blocked", "reason": "readback-mismatch"},
        )
        self.assertFalse(fake.delete_clicked)

    def test_stop_before_save_stages_then_halts_without_saving(self):
        fake = FakeAxiTransport(publisher_state=summary_state())
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = self.write_manifest(directory, valid_manifest_v2())
            ledger_path = Path(directory) / "ledger.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = MODULE.run_axi(
                    manifest_path=str(manifest_path),
                    ledger_path=str(ledger_path),
                    transport=fake,
                    stop_before_save=True,
                )
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 4)
        self.assertEqual(
            ledger["packages"]["@example/widgets"], {"status": "migrating"}
        )
        self.assertFalse(fake.save_clicked)
        self.assertFalse(fake.delete_clicked)
        # The staged form holds the target, but nothing was persisted.
        self.assertEqual(fake.values["Repository*"], "widgets")
        self.assertEqual(fake.publisher_state["repository"], "widgets-old")

    def test_debug_classify_prints_only_redaction_safe_tuple_data(self):
        fake = FakeAxiTransport(
            publisher_state=summary_state(owner="other-org", repository="legacy")
        )
        output = io.StringIO()
        MODULE.DEBUG_CLASSIFY = True
        try:
            with tempfile.TemporaryDirectory() as directory:
                manifest_path = self.write_manifest(directory, valid_manifest_v2())
                with contextlib.redirect_stdout(output):
                    MODULE.run_axi(
                        manifest_path=str(manifest_path),
                        ledger_path=str(Path(directory) / "ledger.json"),
                        transport=fake,
                    )
        finally:
            MODULE.DEBUG_CLASSIFY = False

        text = output.getvalue()
        self.assertIn("debug-classify: @example/widgets: view=summary", text)
        self.assertIn("summary tuple other-org/legacy", text)
        self.assertIn("target-match=False", text)
        self.assertNotIn("https://", text)

    def test_migration_clearing_readback_mismatch_stops(self):
        fake = FakeAxiTransport(publisher_state=summary_state(), corrupt_backspace=True)
        driver = make_driver(fake)
        handle = open_widget(driver)

        with self.assertRaisesRegex(MODULE.HarnessError, "clearing"):
            driver.migrate_stage(handle, model_previous(), model_publisher())

        self.assertFalse(fake.delete_clicked)

    def test_migration_edit_prefill_mismatch_stops(self):
        fake = FakeAxiTransport(
            publisher_state=summary_state(),
            edit_prefill_override=summary_state(repository="tampered"),
        )
        driver = make_driver(fake)
        handle = open_widget(driver)

        with self.assertRaisesRegex(MODULE.HarnessError, "previous tuple"):
            driver.migrate_stage(handle, model_previous(), model_publisher())

        self.assertEqual(len([call for call in fake.calls if call[0] == "press"]), 0)


class AxiStaleRefTests(unittest.TestCase):
    def test_stale_ref_click_is_retried_once_with_fresh_snapshot(self):
        fake = FakeAxiTransport(stale_clicks=1)
        driver = make_driver(fake)
        handle = open_widget(driver)

        driver.stage(handle, model_publisher())

        clicks = [index for index, call in enumerate(fake.calls) if call[0] == "click"]
        self.assertGreaterEqual(len(clicks), 2)
        between = [call[0] for call in fake.calls[clicks[0] + 1 : clicks[1]]]
        self.assertIn("snapshot", between)

    def test_stale_ref_twice_stops_without_further_browser_actions(self):
        fake = FakeAxiTransport(stale_clicks=10**6)
        driver = make_driver(fake)
        handle = open_widget(driver)

        with self.assertRaisesRegex(MODULE.HarnessError, "stale"):
            driver.stage(handle, model_publisher())

        self.assertEqual(
            len([call for call in fake.calls if call[0] == "click"]), 2
        )

    def test_stale_edit_click_blocks_migration(self):
        fake = FakeAxiTransport(publisher_state=summary_state(), stale_clicks=10**6)
        driver = make_driver(fake)
        handle = open_widget(driver)

        with self.assertRaisesRegex(MODULE.HarnessError, "stale"):
            driver.migrate_stage(handle, model_previous(), model_publisher())

        self.assertFalse(fake.editing)
        self.assertFalse(fake.delete_clicked)

    def test_refs_are_passed_back_exactly_as_printed_with_generation_prefix(self):
        fake = FakeAxiTransport()
        driver = make_driver(fake)
        handle = open_widget(driver)

        driver.stage(handle, model_publisher())

        clicks = [call[1] for call in fake.calls if call[0] == "click"]
        self.assertTrue(clicks)
        for ref in clicks:
            self.assertRegex(ref, r"^@g\d+:[12]_\d+$")

    def test_non_stale_axi_error_fails_closed(self):
        fake = FakeAxiTransport()
        driver = make_driver(fake)
        open_widget(driver)

        original_run = fake.run
        fake.run = lambda *args: MODULE.AxiResult(
            1, 'error: "Bridge unreachable"\ncode: BROWSER_ERROR\n'
        )
        try:
            with self.assertRaises(MODULE.HarnessError):
                driver._axi("snapshot", "--full")
        finally:
            fake.run = original_run


class AxiSnapshotParseTests(unittest.TestCase):
    def test_unparseable_snapshot_blocks_inspect_as_ui_drift(self):
        fake = FakeAxiTransport(garbage_selectpage=True)
        driver = make_driver(fake)
        handle = open_widget(driver)

        observation = driver.inspect(handle, "@example/widgets", model_publisher())

        self.assertEqual(observation, MODULE.Observation("blocked", "ui-drift"))

    def test_unparseable_snapshot_refuses_preflight_in_entrypoint(self):
        fake = FakeAxiTransport(garbage_selectpage=True)
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps(valid_manifest()), encoding="utf-8")
            ledger = Path(directory) / "ledger.json"
            with contextlib.redirect_stdout(io.StringIO()):
                code = MODULE.run_axi(
                    manifest_path=str(manifest),
                    ledger_path=str(ledger),
                    transport=fake,
                )
            data = json.loads(ledger.read_text(encoding="utf-8"))

        self.assertEqual(code, 3)
        self.assertEqual(
            data["packages"]["@example/widgets"],
            {"status": "blocked", "reason": "ui-drift"},
        )

    def test_truncated_snapshot_fails_closed(self):
        driver = make_driver(FakeAxiTransport())
        truncated = (
            "snapshot:\n"
            'uid=g1:1_0 RootWebArea "npm" url="https://www.npmjs.com/x"\n'
            "    ... (truncated, 90000 chars total)\n"
        )
        with self.assertRaisesRegex(MODULE.HarnessError, "truncated"):
            driver._parse_page(truncated)

    def test_refs_without_generation_tag_fail_closed(self):
        driver = make_driver(FakeAxiTransport())
        untagged = (
            "snapshot:\n"
            'uid=1_0 RootWebArea "npm" url="https://www.npmjs.com/x"\n'
        )
        with self.assertRaisesRegex(MODULE.HarnessError, "generation"):
            driver._parse_page(untagged)

    def test_embedded_quotes_in_names_are_recovered_without_forging_attributes(self):
        driver = make_driver(FakeAxiTransport())
        page = (
            "snapshot:\n"
            'uid=g1:1_0 RootWebArea "npm" url="https://www.npmjs.com/x"\n'
            '  uid=g1:1_9 button "He said "hi"" focusable\n'
            '  uid=g1:1_8 checkbox "Allow npm publish" checked="true"\n'
        )
        _, nodes = driver._parse_page(page)

        button = next(node for node in nodes if node.role == "button")
        self.assertEqual(button.name, 'He said "hi"')
        self.assertEqual(button.attrs, {"focusable": True})
        checkbox = next(node for node in nodes if node.role == "checkbox")
        with self.assertRaisesRegex(MODULE.HarnessError, "checked"):
            MODULE.AxiDriver._checked(checkbox)

    def test_unterminated_names_fail_closed(self):
        driver = make_driver(FakeAxiTransport())
        broken = (
            "snapshot:\n"
            'uid=g1:1_0 RootWebArea "npm" url="https://www.npmjs.com/x"\n'
            '  uid=g1:1_9 button "Unterminated\n'
        )
        with self.assertRaisesRegex(MODULE.HarnessError, "unparseable"):
            driver._parse_page(broken)

    def test_linebreak_nodes_spanning_physical_lines_are_parsed(self):
        # Live regression: the npm access page renders LineBreak nodes whose
        # accessible name is a literal newline, so one node spans two physical
        # lines. The parser must rejoin them instead of failing the page.
        driver = make_driver(FakeAxiTransport())
        page = (
            "snapshot:\n"
            'uid=g1:1_0 RootWebArea "npm" url="https://www.npmjs.com/x"\n'
            '  uid=g1:1_1 StaticText "Establish a trust between your package and your repository using OpenID Connect (OIDC)."\n'
            '  uid=g1:1_2 LineBreak "\n'
            '"\n'
            '  uid=g1:1_3 link "Learn more about OpenID Connect." url="https://gh.io/npm-docs-trusted-publishers"\n'
        )
        _, nodes = driver._parse_page(page)

        self.assertEqual(len(nodes), 4)
        linebreak = next(node for node in nodes if node.role == "LineBreak")
        self.assertEqual(linebreak.name, "\n")
        self.assertEqual(nodes[-1].attrs["url"], "https://gh.io/npm-docs-trusted-publishers")

    def test_multiline_names_with_trailing_attributes_are_parsed(self):
        driver = make_driver(FakeAxiTransport())
        page = (
            "snapshot:\n"
            'uid=g1:1_0 RootWebArea "npm" url="https://www.npmjs.com/x"\n'
            '  uid=g1:1_1 StaticText "first line\n'
            "second line\n"
            '\n'
            'third after blank" focusable\n'
        )
        _, nodes = driver._parse_page(page)

        self.assertEqual(len(nodes), 2)
        self.assertEqual(nodes[1].name, "first line\nsecond line\n\nthird after blank")
        self.assertEqual(nodes[1].attrs, {"focusable": True})

    def test_orphan_text_before_first_node_fails_closed(self):
        driver = make_driver(FakeAxiTransport())
        page = (
            "snapshot:\n"
            "stray text before any node\n"
            'uid=g1:1_0 RootWebArea "npm" url="https://www.npmjs.com/x"\n'
        )
        with self.assertRaisesRegex(MODULE.HarnessError, "unparseable"):
            driver._parse_page(page)

    def test_full_live_page_chrome_classifies_summary_and_form(self):
        # The fake renders the complete live page chrome (notification alert,
        # tabs, LineBreak intro, radios, sidebar). Both views must classify.
        summary_fake = FakeAxiTransport(
            publisher_state=summary_state(repository="widgets", workflow="release.yml")
        )
        driver = make_driver(summary_fake)
        handle = open_widget(driver)
        self.assertEqual(
            driver.inspect(handle, "@example/widgets", model_publisher()),
            MODULE.Observation("exact"),
        )

        form_fake = FakeAxiTransport()
        driver = make_driver(form_fake)
        handle = open_widget(driver)
        self.assertEqual(
            driver.inspect(handle, "@example/widgets", model_publisher()),
            MODULE.Observation("absent"),
        )

    def test_inline_single_item_help_lines_are_not_parsed_as_nodes(self):
        driver = make_driver(FakeAxiTransport())
        page = (
            "snapshot:\n"
            'uid=g1:1_0 RootWebArea "npm" url="https://www.npmjs.com/x"\n'
            'help[1]: "Run `chrome-devtools-axi click @g1:1_0` to interact"\n'
        )
        url, nodes = driver._parse_page(page)

        self.assertEqual(url, "https://www.npmjs.com/x")
        self.assertEqual(len(nodes), 1)


class AxiEntrypointTests(unittest.TestCase):
    def write_manifest(self, directory):
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
        return path

    def test_entrypoint_converges_exact_match_with_restricted_transport(self):
        fake = FakeAxiTransport(
            publisher_state=summary_state(repository="widgets", workflow="release.yml")
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            ledger = Path(directory) / "ledger.json"
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = MODULE.run_axi(
                    manifest_path=str(manifest),
                    ledger_path=str(ledger),
                    transport=fake,
                )

            data = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(
            data["packages"]["@example/widgets"]["status"], "exact-match"
        )
        self.assertEqual(output.getvalue(), "@example/widgets: exact-match\n")

    def test_entrypoint_redacts_transport_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            ledger = Path(directory) / "ledger.json"
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = MODULE.run_axi(
                    manifest_path=str(manifest),
                    ledger_path=str(ledger),
                    transport=RaisingTransport(),
                )

            data = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(code, 5)
        self.assertNotIn("sensitive", output.getvalue())
        self.assertEqual(
            data["packages"]["@example/widgets"],
            {"status": "blocked", "reason": "harness-error"},
        )

    def test_main_fails_closed_when_pinned_cli_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = MODULE.main(
                    [
                        "--manifest",
                        str(manifest),
                        "--ledger",
                        str(Path(directory) / "ledger.json"),
                        "--axi-script",
                        str(Path(directory) / "missing" / "chrome-devtools-axi.js"),
                    ]
                )
        self.assertEqual(code, 5)
        self.assertIn("blocked", output.getvalue())


class ConvergerSequentialTests(unittest.TestCase):
    def make_ledger(self, directory, manifest):
        return MODULE.LedgerStore(str(Path(directory) / "ledger.json"), manifest)

    def test_advances_only_after_success_and_exact_readback(self):
        package = "@example/widgets"
        exact = MODULE.Observation("exact")
        driver = FakeDriver(
            {package: [MODULE.Observation("absent"), exact, exact, exact]},
            {package: ["success"]},
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = model_manifest()
            ledger = self.make_ledger(directory, manifest)

            code = MODULE.Converger(manifest, ledger, driver).run()

        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in driver.calls].count("stage"), 1)
        self.assertEqual([call[0] for call in driver.calls].count("save"), 1)
        self.assertEqual(ledger.records[package]["status"], "saved-verified")

    def test_authentication_cancellation_stops_before_next_package(self):
        packages = ("@example/alpha", "@example/beta", "@example/gamma")
        exact = MODULE.Observation("exact")
        driver = FakeDriver(
            {
                packages[0]: [MODULE.Observation("absent"), exact, exact],
                packages[1]: [MODULE.Observation("absent"), exact],
                packages[2]: [MODULE.Observation("absent")],
            },
            {
                packages[0]: ["success"],
                packages[1]: ["authentication-failed"],
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = model_manifest(packages)
            ledger = self.make_ledger(directory, manifest)

            code = MODULE.Converger(manifest, ledger, driver).run()

        self.assertEqual(code, 4)
        self.assertNotIn(("stage", packages[2]), driver.calls)
        self.assertEqual(ledger.records[packages[0]]["status"], "saved-verified")
        self.assertEqual(
            ledger.records[packages[1]],
            {"status": "blocked", "reason": "authentication-failed"},
        )

    def test_migration_authentication_failure_keeps_migrating_context(self):
        package = "@example/widgets"
        exact = MODULE.Observation("exact")
        driver = FakeDriver(
            {package: [MODULE.Observation("previous"), exact]},
            {package: ["authentication-failed"]},
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = model_manifest(previous=model_previous())
            ledger = self.make_ledger(directory, manifest)

            code = MODULE.Converger(manifest, ledger, driver).run()

        self.assertEqual(code, 4)
        self.assertEqual(
            ledger.records[package],
            {"status": "blocked", "reason": "authentication-failed"},
        )

    def test_readback_mismatch_stops_without_retry(self):
        package = "@example/widgets"
        driver = FakeDriver(
            {
                package: [
                    MODULE.Observation("absent"),
                    MODULE.Observation("exact"),
                    MODULE.Observation("absent"),
                ]
            },
            {package: ["success"]},
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = model_manifest()
            ledger = self.make_ledger(directory, manifest)

            code = MODULE.Converger(manifest, ledger, driver).run()

        self.assertEqual(code, 4)
        self.assertEqual([call[0] for call in driver.calls].count("stage"), 1)
        self.assertEqual([call[0] for call in driver.calls].count("save"), 1)
        self.assertEqual(
            ledger.records[package],
            {"status": "blocked", "reason": "readback-mismatch"},
        )

    def test_partial_completion_resumes_by_rereading_every_package(self):
        packages = ("@example/alpha", "@example/beta")
        manifest = model_manifest(packages)
        exact = MODULE.Observation("exact")
        with tempfile.TemporaryDirectory() as directory:
            ledger = self.make_ledger(directory, manifest)
            first = FakeDriver(
                {
                    packages[0]: [MODULE.Observation("absent"), exact, exact],
                    packages[1]: [MODULE.Observation("absent"), exact],
                },
                {
                    packages[0]: ["success"],
                    packages[1]: ["authentication-failed"],
                },
            )
            self.assertEqual(MODULE.Converger(manifest, ledger, first).run(), 4)

            resumed_ledger = self.make_ledger(directory, manifest)
            second = FakeDriver(
                {
                    packages[0]: [exact, exact],
                    packages[1]: [MODULE.Observation("absent"), exact, exact, exact],
                },
                {packages[1]: ["success"]},
            )
            code = MODULE.Converger(manifest, resumed_ledger, second).run()

        self.assertEqual(code, 0)
        self.assertGreaterEqual(
            [call[:2] for call in second.calls].count(("inspect", packages[0])), 2
        )
        self.assertNotIn(("stage", packages[0]), second.calls)
        self.assertEqual(resumed_ledger.records[packages[1]]["status"], "saved-verified")

    def test_final_sweep_reads_every_package(self):
        packages = ("@example/alpha", "@example/beta")
        exact = MODULE.Observation("exact")
        driver = FakeDriver(
            {
                package: [MODULE.Observation("absent"), exact, exact, exact]
                for package in packages
            },
            {package: ["success"] for package in packages},
        )
        with tempfile.TemporaryDirectory() as directory:
            manifest = model_manifest(packages)
            ledger = self.make_ledger(directory, manifest)

            code = MODULE.Converger(manifest, ledger, driver).run()

        self.assertEqual(code, 0)
        self.assertEqual([call[0] for call in driver.calls].count("reload"), 4)
        for package in packages:
            self.assertEqual(
                [call[:2] for call in driver.calls].count(("inspect", package)), 4
            )


if __name__ == "__main__":
    unittest.main()
