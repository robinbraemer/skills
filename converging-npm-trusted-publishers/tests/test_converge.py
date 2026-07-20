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


def ax_node(role, name, *, value=None, backend=None, **properties):
    node = {
        "role": {"value": role},
        "name": {"value": name},
        "properties": [
            {"name": key, "value": {"value": prop_value}}
            for key, prop_value in properties.items()
        ],
    }
    if value is not None:
        node["value"] = {"value": value}
    if backend is not None:
        node["backendDOMNodeId"] = backend
    return node


def form_tree(package="@example/widgets", values=None, checked=("npm publish",)):
    values = values or {
        "Organization or user": "example-org",
        "Repository": "widgets",
        "Workflow filename": "release.yml",
        "Environment name (optional)": "",
    }
    nodes = [
        ax_node("heading", package),
        ax_node("heading", "Trusted publishing"),
    ]
    backend = 10
    for label, value in values.items():
        nodes.append(
            ax_node(
                "textbox",
                label,
                value=value,
                backend=backend,
                focused=False,
                disabled=False,
            )
        )
        backend += 1
    for action in ("npm publish", "npm stage publish"):
        nodes.append(
            ax_node(
                "checkbox",
                action,
                backend=backend,
                checked=action in checked,
                disabled=False,
            )
        )
        backend += 1
    nodes.append(ax_node("button", "Save", backend=backend, disabled=False))
    return nodes


class StaticBrowserApi:
    def __init__(self, tree=None, identity=None):
        self.tree = tree or form_tree()
        self.identity = identity or {
            "url": "https://www.npmjs.com/package/@example/widgets/access",
            "title": "Package access",
        }
        self.calls = []
        self.handle = object()

    def mapping(self):
        return {
            "new_tab": self.new_tab,
            "switch_tab": self.switch_tab,
            "wait_for_load": self.wait_for_load,
            "page_identity": self.page_identity,
            "accessibility_tree": self.accessibility_tree,
            "box_model": self.box_model,
            "reload_page": self.reload_page,
            "click_at_xy": self.click_at_xy,
            "press_key": self.press_key,
        }

    def new_tab(self, url):
        self.calls.append(("new_tab", url))
        return self.handle

    def switch_tab(self, handle):
        self.calls.append(("switch_tab", handle))

    def wait_for_load(self):
        self.calls.append(("wait_for_load",))

    def page_identity(self):
        return dict(self.identity)

    def accessibility_tree(self):
        return list(self.tree)

    def box_model(self, node):
        return {"content": [0, 0, 10, 0, 10, 10, 0, 10]}

    def reload_page(self):
        self.calls.append(("reload_page",))

    def click_at_xy(self, x, y):
        self.calls.append(("click_at_xy", x, y))

    def press_key(self, key, modifiers=0):
        self.calls.append(("press_key", key, modifiers))


class InteractiveBrowserApi(StaticBrowserApi):
    FIELD_BACKENDS = {
        "Organization or user": 10,
        "Repository": 11,
        "Workflow filename": 12,
        "Environment name (optional)": 13,
    }
    ACTION_BACKENDS = {"npm publish": 20, "npm stage publish": 21}
    SAVE_BACKEND = 30

    def __init__(
        self,
        *,
        save_result="success",
        corrupt_prefix=False,
        save_disabled=False,
        stale_success=False,
        auth_status_message=None,
    ):
        super().__init__(tree=[])
        self.values = {label: "" for label in self.FIELD_BACKENDS}
        self.checked = set()
        self.focused = None
        self.save_result = save_result
        self.corrupt_prefix = corrupt_prefix
        self.save_disabled = save_disabled
        self.stale_success = stale_success
        self.auth_status_message = auth_status_message
        self.save_clicked = False

    def accessibility_tree(self):
        nodes = [
            ax_node("heading", "@example/widgets"),
            ax_node("heading", "Trusted publishing"),
        ]
        for label, backend in self.FIELD_BACKENDS.items():
            nodes.append(
                ax_node(
                    "textbox",
                    label,
                    value=self.values[label],
                    backend=backend,
                    focused=self.focused == label,
                    disabled=False,
                )
            )
        for action, backend in self.ACTION_BACKENDS.items():
            nodes.append(
                ax_node(
                    "checkbox",
                    action,
                    backend=backend,
                    checked=action in self.checked,
                    disabled=False,
                )
            )
        nodes.append(
            ax_node(
                "button",
                "Save",
                backend=self.SAVE_BACKEND,
                disabled=self.save_disabled,
            )
        )
        if self.save_clicked and self.auth_status_message is not None:
            nodes.extend(
                [
                    ax_node("status", "Trusted publisher saved successfully"),
                    ax_node("status", self.auth_status_message),
                ]
            )
        elif self.save_clicked and self.save_result in {
            "positive-then-negative",
            "negative-then-positive",
            "status-auth-negative",
        }:
            positive = ax_node("status", "Trusted publisher saved successfully")
            negative = ax_node(
                "status" if self.save_result == "status-auth-negative" else "alert",
                "Authentication canceled",
            )
            nodes.extend(
                [positive, negative]
                if self.save_result in {"positive-then-negative", "status-auth-negative"}
                else [negative, positive]
            )
        elif self.stale_success or (self.save_clicked and self.save_result == "success"):
            nodes.append(ax_node("status", "Trusted publisher saved successfully"))
        elif self.save_clicked and self.save_result == "authentication-failed":
            nodes.append(ax_node("alert", "Authentication canceled"))
        elif self.save_clicked and self.save_result == "save-failed":
            nodes.append(ax_node("alert", "Trusted publisher save failed"))
        elif self.save_clicked and self.save_result == "negative-success":
            nodes.append(ax_node("status", "Trusted publisher save unsuccessful"))
        return nodes

    def box_model(self, node):
        left = node * 10
        return {
            "content": [left, 0, left + 8, 0, left + 8, 8, left, 8]
        }

    def click_at_xy(self, x, y):
        super().click_at_xy(x, y)
        backend = int(x // 10)
        for label, candidate in self.FIELD_BACKENDS.items():
            if backend == candidate:
                self.focused = label
                return
        for action, candidate in self.ACTION_BACKENDS.items():
            if backend == candidate:
                if action in self.checked:
                    self.checked.remove(action)
                else:
                    self.checked.add(action)
                return
        if backend == self.SAVE_BACKEND and not self.save_disabled:
            self.save_clicked = True

    def press_key(self, key, modifiers=0):
        super().press_key(key, modifiers)
        if self.focused is None or modifiers != 0 or len(key) != 1:
            return
        value = self.values[self.focused] + key
        if self.corrupt_prefix and not self.values[self.focused]:
            value = "!"
        self.values[self.focused] = value


class BrowserHarnessAdapterContractTests(unittest.TestCase):
    def test_adapter_opens_unique_github_provider_form_before_classifying_absent(self):
        fake = InteractiveBrowserApi()
        choice_tree = [
            ax_node("heading", "@example/widgets"),
            ax_node("heading", "Trusted publishing"),
            ax_node("button", "GitHub Actions", backend=40, disabled=False),
            ax_node("button", "GitLab CI/CD", backend=41, disabled=False),
            ax_node("button", "CircleCI", backend=42, disabled=False),
        ]
        original_tree = fake.accessibility_tree
        choosing = {"value": True}

        def tree():
            return choice_tree if choosing["value"] else original_tree()

        def click(x, y):
            if int(x // 10) == 40:
                choosing["value"] = False
            InteractiveBrowserApi.click_at_xy(fake, x, y)

        api = fake.mapping()
        api["accessibility_tree"] = tree
        api["click_at_xy"] = click
        driver = MODULE.BrowserHarnessDriver(api)
        handle = driver.open_package(
            "@example/widgets",
            "https://www.npmjs.com/package/@example/widgets/access",
        )

        observation = driver.inspect(
            handle, "@example/widgets", model_manifest().publisher
        )

        self.assertEqual(observation, MODULE.Observation("absent"))
        self.assertFalse(choosing["value"])

    def test_adapter_rejects_missing_or_extra_api(self):
        api = StaticBrowserApi().mapping()
        for candidate in ({key: value for key, value in api.items() if key != "press_key"},
                          {**api, "type_text": lambda text: None}):
            with self.subTest(keys=sorted(candidate)):
                with self.assertRaisesRegex(MODULE.HarnessError, "API keys"):
                    MODULE.BrowserHarnessDriver(candidate)

    def test_adapter_uses_opaque_tab_handles_without_logging(self):
        fake = StaticBrowserApi()
        driver = MODULE.BrowserHarnessDriver(fake.mapping())

        handle = driver.open_package(
            "@example/widgets",
            "https://www.npmjs.com/package/@example/widgets/access",
        )

        self.assertIs(handle, fake.handle)
        self.assertEqual(fake.calls[0][0], "new_tab")

    def test_adapter_rejects_wrong_origin_path_or_package_identity(self):
        identities = (
            {
                "url": "https://example.invalid/package/@example/widgets/access",
                "title": "Package access",
            },
            {
                "url": "https://www.npmjs.com/package/@example/icons/access",
                "title": "Package access",
            },
        )
        for identity in identities:
            with self.subTest(url=identity["url"]):
                fake = StaticBrowserApi(identity=identity)
                driver = MODULE.BrowserHarnessDriver(fake.mapping())
                observation = driver.inspect(
                    fake.handle, "@example/widgets", model_manifest().publisher
                )
                self.assertEqual(
                    observation,
                    MODULE.Observation("blocked", "identity-mismatch"),
                )

        fake = StaticBrowserApi(
            tree=[node for node in form_tree() if node["name"]["value"] != "@example/widgets"]
        )
        observation = MODULE.BrowserHarnessDriver(fake.mapping()).inspect(
            fake.handle, "@example/widgets", model_manifest().publisher
        )
        self.assertEqual(observation.reason, "identity-mismatch")

    def test_adapter_rejects_ambiguous_or_missing_semantic_controls(self):
        missing = [
            node
            for node in form_tree()
            if node["name"]["value"] != "Workflow filename"
        ]
        duplicate = form_tree() + [
            ax_node(
                "textbox",
                "Repository",
                value="widgets",
                backend=99,
                focused=False,
                disabled=False,
            )
        ]
        missing_enabled_state = form_tree()
        missing_enabled_state[2]["properties"] = [
            prop
            for prop in missing_enabled_state[2]["properties"]
            if prop["name"] != "disabled"
        ]
        for tree in (missing, duplicate, missing_enabled_state):
            with self.subTest(size=len(tree)):
                fake = StaticBrowserApi(tree=tree)
                observation = MODULE.BrowserHarnessDriver(fake.mapping()).inspect(
                    fake.handle, "@example/widgets", model_manifest().publisher
                )
                self.assertEqual(observation, MODULE.Observation("blocked", "ui-drift"))


class BrowserHarnessInteractionTests(unittest.TestCase):
    def driver_for(self, fake, poll_attempts=1):
        driver = MODULE.BrowserHarnessDriver(
            fake.mapping(), poll_attempts=poll_attempts, sleeper=lambda _: None
        )
        driver.open_package(
            "@example/widgets",
            "https://www.npmjs.com/package/@example/widgets/access",
        )
        return driver

    def test_text_entry_uses_press_key_per_character_with_prefix_readback(self):
        fake = InteractiveBrowserApi()
        driver = self.driver_for(fake)

        driver.stage(fake.handle, model_manifest().publisher)

        typed = "".join(
            call[1] for call in fake.calls if call[0] == "press_key"
        )
        self.assertEqual(typed, "example-orgwidgetsrelease.yml")
        self.assertEqual(fake.values["Organization or user"], "example-org")
        self.assertEqual(fake.values["Repository"], "widgets")
        self.assertEqual(fake.values["Workflow filename"], "release.yml")

    def test_text_entry_stops_on_prefix_mismatch(self):
        fake = InteractiveBrowserApi(corrupt_prefix=True)
        driver = self.driver_for(fake)

        with self.assertRaisesRegex(MODULE.HarnessError, "prefix"):
            driver.stage(fake.handle, model_manifest().publisher)

        self.assertEqual(
            len([call for call in fake.calls if call[0] == "press_key"]), 1
        )

    def test_actions_click_only_desired_unchecked_controls(self):
        fake = InteractiveBrowserApi()
        driver = self.driver_for(fake)

        driver.stage(fake.handle, model_manifest().publisher)

        self.assertEqual(fake.checked, {"npm publish"})
        action_clicks = [
            call for call in fake.calls
            if call[0] == "click_at_xy" and int(call[1] // 10) in {20, 21}
        ]
        self.assertEqual(len(action_clicks), 1)

    def test_disabled_save_stops(self):
        fake = InteractiveBrowserApi(save_disabled=True)
        driver = self.driver_for(fake)

        driver.stage(fake.handle, model_manifest().publisher)
        outcome = driver.save_and_wait(fake.handle)

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
                fake = InteractiveBrowserApi(save_result=result)
                driver = self.driver_for(fake, poll_attempts=2)
                driver.stage(fake.handle, model_manifest().publisher)
                self.assertEqual(driver.save_and_wait(fake.handle), expected)

    def test_authentication_status_cannot_be_overridden_by_publisher_success(self):
        for message, expected in (
            ("Authentication unsuccessful", "authentication-failed"),
            ("Passkey denied", "authentication-failed"),
            ("Security key rejected", "authentication-failed"),
            ("WebAuthn pending", "authentication-ambiguous"),
            ("Authentication verified", "success"),
        ):
            with self.subTest(message=message):
                fake = InteractiveBrowserApi(auth_status_message=message)
                driver = self.driver_for(fake, poll_attempts=1)
                driver.stage(fake.handle, model_manifest().publisher)
                self.assertEqual(driver.save_and_wait(fake.handle), expected)

    def test_save_wait_rejects_stale_success(self):
        fake = InteractiveBrowserApi(save_result="none", stale_success=True)
        driver = self.driver_for(fake, poll_attempts=2)
        driver.stage(fake.handle, model_manifest().publisher)

        outcome = driver.save_and_wait(fake.handle)

        self.assertEqual(outcome, "authentication-ambiguous")

    def test_reload_requires_exact_persisted_tuple(self):
        fake = InteractiveBrowserApi()
        driver = self.driver_for(fake)
        driver.stage(fake.handle, model_manifest().publisher)
        fake.values["Workflow filename"] = "release.yaml"

        driver.reload(fake.handle)
        observation = driver.inspect(
            fake.handle, "@example/widgets", model_manifest().publisher
        )

        self.assertEqual(
            observation, MODULE.Observation("blocked", "unexpected-publisher")
        )


class BrowserHarnessEntrypointTests(unittest.TestCase):
    def write_manifest(self, directory):
        path = Path(directory) / "manifest.json"
        path.write_text(json.dumps(valid_manifest()), encoding="utf-8")
        return path

    def test_entrypoint_converges_exact_match_with_restricted_api(self):
        fake = StaticBrowserApi()
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            ledger = Path(directory) / "ledger.json"
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = MODULE.run_browser_harness(
                    manifest_path=str(manifest),
                    ledger_path=str(ledger),
                    api=fake.mapping(),
                )

            data = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(
            data["packages"]["@example/widgets"]["status"], "exact-match"
        )
        self.assertEqual(output.getvalue(), "@example/widgets: exact-match\n")

    def test_entrypoint_redacts_harness_exception(self):
        fake = StaticBrowserApi()

        def fail_without_disclosure(url):
            raise RuntimeError("sensitive browser detail")

        api = fake.mapping()
        api["new_tab"] = fail_without_disclosure
        with tempfile.TemporaryDirectory() as directory:
            manifest = self.write_manifest(directory)
            ledger = Path(directory) / "ledger.json"
            output = io.StringIO()

            with contextlib.redirect_stdout(output):
                code = MODULE.run_browser_harness(
                    manifest_path=str(manifest),
                    ledger_path=str(ledger),
                    api=api,
                )

            data = json.loads(ledger.read_text(encoding="utf-8"))
        self.assertEqual(code, 5)
        self.assertNotIn("sensitive", output.getvalue())
        self.assertEqual(
            data["packages"]["@example/widgets"],
            {"status": "blocked", "reason": "harness-error"},
        )


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
