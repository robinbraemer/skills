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


class ManifestTests(unittest.TestCase):
    def load(self, data):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return MODULE.load_manifest(str(path))

    def test_loads_exact_manifest(self):
        manifest = self.load(valid_manifest())

        self.assertEqual(manifest.packages, ("@example/widgets",))
        self.assertEqual(manifest.publisher.owner, "example-org")
        self.assertEqual(manifest.publisher.repository, "widgets")
        self.assertEqual(manifest.publisher.workflow, "release.yml")
        self.assertIsNone(manifest.publisher.environment)
        self.assertEqual(manifest.publisher.allowed_actions, ("npm publish",))

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


def model_manifest(packages=("@example/widgets",)):
    return MODULE.Manifest(
        packages=packages,
        publisher=MODULE.Publisher(
            owner="example-org",
            repository="widgets",
            workflow="release.yml",
            environment=None,
            allowed_actions=("npm publish",),
        ),
    )


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

    def inspect(self, handle, package, publisher):
        self.calls.append(("inspect", package))
        if not self.observations[package]:
            raise AssertionError(f"no observation queued for {package}")
        return self.observations[package].pop(0)

    def stage(self, handle, publisher):
        self.calls.append(("stage", handle))

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


class ConvergerPreflightTests(unittest.TestCase):
    def run_with(self, manifest, driver):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ledger = MODULE.LedgerStore(str(Path(directory.name) / "ledger.json"), manifest)
        code = MODULE.Converger(manifest, ledger, driver).run()
        return code, ledger

    def test_exact_match_skips_all_writes(self):
        package = "@example/widgets"
        exact = MODULE.Observation("exact")
        driver = FakeDriver({package: [exact, exact]})

        code, ledger = self.run_with(model_manifest(), driver)

        self.assertEqual(code, 0)
        self.assertFalse(any(call[0] in {"stage", "save"} for call in driver.calls))
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
        self.assertFalse(any(call[0] in {"stage", "save"} for call in driver.calls))
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
        self.assertFalse(any(call[0] in {"stage", "save"} for call in driver.calls))

    def test_ui_drift_stops_before_any_write(self):
        package = "@example/widgets"
        driver = FakeDriver({package: [MODULE.Observation("blocked", "ui-drift")]})

        code, _ = self.run_with(model_manifest(), driver)

        self.assertEqual(code, 3)
        self.assertFalse(any(call[0] in {"stage", "save"} for call in driver.calls))


PACKAGE_URL = "https://www.npmjs.com/package/@example/widgets/access"
FIELD_LABELS = (
    "Organization or user",
    "Repository",
    "Workflow filename",
    "Environment name (optional)",
)
FIELD_IDS = {
    "Organization or user": "1_10",
    "Repository": "1_11",
    "Workflow filename": "1_12",
    "Environment name (optional)": "1_13",
}
ACTION_IDS = {"npm publish": "1_20", "npm stage publish": "1_21"}
SAVE_ID = "1_30"
PROVIDER_IDS = {"GitHub Actions": "1_40", "GitLab CI/CD": "1_41", "CircleCI": "1_42"}


class FakeAxiTransport:
    """In-memory double of the pinned chrome-devtools-axi CLI for one npm page.

    Renders real-format output: TOON page/error blocks, `snapshot:` sections
    with generation-stamped `uid=g<N>:<id>` refs, `pages[...]` tables, and
    STALE_REF errors when a ref's generation is no longer current.
    """

    def __init__(
        self,
        *,
        url=PACKAGE_URL,
        package_heading="@example/widgets",
        section_heading="Trusted publishing",
        values=None,
        checked=(),
        save_result="success",
        corrupt_prefix=False,
        save_disabled=False,
        stale_success=False,
        auth_status_message=None,
        provider_choice=False,
        stale_clicks=0,
        garbage_selectpage=False,
        missing_field=None,
        duplicate_field=None,
        disabled_field=None,
    ):
        self.url = url
        self.package_heading = package_heading
        self.section_heading = section_heading
        self.values = {label: "" for label in FIELD_LABELS}
        if values:
            self.values.update(values)
        self.checked = set(checked)
        self.focused = None
        self.save_result = save_result
        self.corrupt_prefix = corrupt_prefix
        self.save_disabled = save_disabled
        self.stale_success = stale_success
        self.auth_status_message = auth_status_message
        self.provider_choice = provider_choice
        self.stale_clicks_remaining = stale_clicks
        self.garbage_selectpage = garbage_selectpage
        self.missing_field = missing_field
        self.duplicate_field = duplicate_field
        self.disabled_field = disabled_field
        self.save_clicked = False
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
            return self._page_output()
        if command == "click":
            return self._click(args[1])
        if command == "press":
            self._press(args[1])
            return self._page_output()
        return self._error(f"Unknown command: {command}", "VALIDATION_ERROR", 2)

    # --- interaction semantics ---

    def _click(self, ref):
        if self.stale_clicks_remaining > 0:
            self.stale_clicks_remaining -= 1
            return self._stale_error(ref)
        stripped = ref[1:] if ref.startswith("@") else ref
        generation, _, node_id = stripped.partition(":")
        if generation != f"g{self.generation}":
            return self._stale_error(ref)
        for label, candidate in FIELD_IDS.items():
            if node_id == candidate:
                self.focused = label
                return self._page_output()
        for action, candidate in ACTION_IDS.items():
            if node_id == candidate:
                self.checked.symmetric_difference_update({action})
                return self._page_output()
        if node_id == SAVE_ID and not self.save_disabled:
            self.save_clicked = True
            return self._page_output()
        if node_id == PROVIDER_IDS["GitHub Actions"] and self.provider_choice:
            self.provider_choice = False
            return self._page_output()
        return self._page_output()

    def _press(self, key):
        if self.focused is None or len(key) != 1:
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

    def _tree_lines(self):
        lines = [
            f'{self._uid("1_0")} RootWebArea "npm" url="{self.url}" focusable focused'
        ]
        if self.package_heading is not None:
            lines.append(f'  {self._uid("1_1")} heading "{self.package_heading}" level="1"')
        if self.section_heading is not None:
            lines.append(f'  {self._uid("1_2")} heading "{self.section_heading}" level="2"')
        if self.provider_choice:
            for name, node_id in PROVIDER_IDS.items():
                lines.append(f'  {self._uid(node_id)} button "{name}"')
            return lines
        for label in FIELD_LABELS:
            if label == self.missing_field:
                continue
            focused = " focused" if self.focused == label else ""
            disabled = " disabled" if label == self.disabled_field else ""
            lines.append(
                f'  {self._uid(FIELD_IDS[label])} textbox "{label}" '
                f'value="{self.values[label]}" focusable{focused}{disabled}'
            )
            if label == self.duplicate_field:
                lines.append(
                    f'  {self._uid("1_99")} textbox "{label}" '
                    f'value="{self.values[label]}" focusable'
                )
        for action in ("npm publish", "npm stage publish"):
            checked = " checked" if action in self.checked else ""
            lines.append(f'  {self._uid(ACTION_IDS[action])} checkbox "{action}"{checked}')
        save_state = " disableable disabled" if self.save_disabled else ""
        lines.append(f'  {self._uid(SAVE_ID)} button "Save"{save_state}')
        lines.extend(self._message_lines())
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
    def test_adapter_opens_unique_github_provider_form_before_classifying_absent(self):
        fake = FakeAxiTransport(provider_choice=True)
        driver = make_driver(fake)
        handle = open_widget(driver)

        observation = driver.inspect(
            handle, "@example/widgets", model_manifest().publisher
        )

        self.assertEqual(observation, MODULE.Observation("absent"))
        self.assertFalse(fake.provider_choice)

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
                    handle, "@example/widgets", model_manifest().publisher
                )
                self.assertEqual(
                    observation,
                    MODULE.Observation("blocked", "identity-mismatch"),
                )

        fake = FakeAxiTransport(package_heading=None)
        driver = make_driver(fake)
        handle = open_widget(driver)
        observation = driver.inspect(
            handle, "@example/widgets", model_manifest().publisher
        )
        self.assertEqual(observation.reason, "identity-mismatch")

    def test_adapter_rejects_ambiguous_missing_or_disabled_semantic_controls(self):
        variants = (
            {"missing_field": "Workflow filename"},
            {"duplicate_field": "Repository"},
            {"disabled_field": "Organization or user"},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                fake = FakeAxiTransport(**variant)
                driver = make_driver(fake)
                handle = open_widget(driver)
                observation = driver.inspect(
                    handle, "@example/widgets", model_manifest().publisher
                )
                self.assertEqual(observation, MODULE.Observation("blocked", "ui-drift"))


class AxiInteractionTests(unittest.TestCase):
    def staged_driver(self, fake, poll_attempts=1):
        driver = make_driver(fake, poll_attempts=poll_attempts)
        handle = open_widget(driver)
        return driver, handle

    def test_text_entry_uses_press_per_character_with_prefix_readback(self):
        fake = FakeAxiTransport()
        driver, handle = self.staged_driver(fake)

        driver.stage(handle, model_manifest().publisher)

        typed = "".join(call[1] for call in fake.calls if call[0] == "press")
        self.assertEqual(typed, "example-orgwidgetsrelease.yml")
        self.assertEqual(fake.values["Organization or user"], "example-org")
        self.assertEqual(fake.values["Repository"], "widgets")
        self.assertEqual(fake.values["Workflow filename"], "release.yml")

    def test_text_entry_stops_on_prefix_mismatch(self):
        fake = FakeAxiTransport(corrupt_prefix=True)
        driver, handle = self.staged_driver(fake)

        with self.assertRaisesRegex(MODULE.HarnessError, "prefix"):
            driver.stage(handle, model_manifest().publisher)

        self.assertEqual(len([call for call in fake.calls if call[0] == "press"]), 1)

    def test_actions_click_only_desired_unchecked_controls(self):
        fake = FakeAxiTransport()
        driver, handle = self.staged_driver(fake)

        driver.stage(handle, model_manifest().publisher)

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

        driver.stage(handle, model_manifest().publisher)
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
                fake = FakeAxiTransport(save_result=result)
                driver, handle = self.staged_driver(fake, poll_attempts=2)
                driver.stage(handle, model_manifest().publisher)
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
                fake = FakeAxiTransport(auth_status_message=message)
                driver, handle = self.staged_driver(fake)
                driver.stage(handle, model_manifest().publisher)
                self.assertEqual(driver.save_and_wait(handle), expected)

    def test_save_wait_rejects_stale_success(self):
        fake = FakeAxiTransport(save_result="none", stale_success=True)
        driver, handle = self.staged_driver(fake, poll_attempts=2)
        driver.stage(handle, model_manifest().publisher)

        outcome = driver.save_and_wait(handle)

        self.assertEqual(outcome, "authentication-ambiguous")

    def test_reload_requires_exact_persisted_tuple(self):
        fake = FakeAxiTransport()
        driver, handle = self.staged_driver(fake)
        driver.stage(handle, model_manifest().publisher)
        fake.values["Workflow filename"] = "release.yaml"

        driver.reload(handle)
        observation = driver.inspect(
            handle, "@example/widgets", model_manifest().publisher
        )

        self.assertEqual(
            observation, MODULE.Observation("blocked", "unexpected-publisher")
        )


class AxiStaleRefTests(unittest.TestCase):
    def test_stale_ref_click_is_retried_once_with_fresh_snapshot(self):
        fake = FakeAxiTransport(stale_clicks=1)
        driver = make_driver(fake)
        handle = open_widget(driver)

        driver.stage(handle, model_manifest().publisher)

        clicks = [index for index, call in enumerate(fake.calls) if call[0] == "click"]
        self.assertGreaterEqual(len(clicks), 2)
        between = [call[0] for call in fake.calls[clicks[0] + 1 : clicks[1]]]
        self.assertIn("snapshot", between)

    def test_stale_ref_twice_stops_without_further_browser_actions(self):
        fake = FakeAxiTransport(stale_clicks=10**6)
        driver = make_driver(fake)
        handle = open_widget(driver)

        with self.assertRaisesRegex(MODULE.HarnessError, "stale"):
            driver.stage(handle, model_manifest().publisher)

        self.assertEqual(
            len([call for call in fake.calls if call[0] == "click"]), 2
        )

    def test_stale_provider_click_blocks_inspect_as_ui_drift(self):
        fake = FakeAxiTransport(provider_choice=True, stale_clicks=10**6)
        driver = make_driver(fake)
        handle = open_widget(driver)

        observation = driver.inspect(
            handle, "@example/widgets", model_manifest().publisher
        )

        self.assertEqual(observation, MODULE.Observation("blocked", "ui-drift"))

    def test_refs_are_passed_back_exactly_as_printed_with_generation_prefix(self):
        fake = FakeAxiTransport()
        driver = make_driver(fake)
        handle = open_widget(driver)

        driver.stage(handle, model_manifest().publisher)

        clicks = [call[1] for call in fake.calls if call[0] == "click"]
        self.assertTrue(clicks)
        for ref in clicks:
            self.assertRegex(ref, r"^@g\d+:1_\d+$")

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
            with self.assertRaises(MODULE.StaleRefError):
                raise MODULE.StaleRefError("sanity: subclass relationship")
        finally:
            fake.run = original_run


class AxiSnapshotParseTests(unittest.TestCase):
    def test_unparseable_snapshot_blocks_inspect_as_ui_drift(self):
        fake = FakeAxiTransport(garbage_selectpage=True)
        driver = make_driver(fake)
        handle = open_widget(driver)

        observation = driver.inspect(
            handle, "@example/widgets", model_manifest().publisher
        )

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
            '  uid=g1:1_8 checkbox "npm publish" checked="true"\n'
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
            values={
                "Organization or user": "example-org",
                "Repository": "widgets",
                "Workflow filename": "release.yml",
            },
            checked=("npm publish",),
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
