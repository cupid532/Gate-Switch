from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).with_name("sgate.py")
spec = importlib.util.spec_from_file_location("sgate", SCRIPT)
assert spec and spec.loader
sgate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sgate)


class SGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.originals = {
            "CODEX_HOME": sgate.CODEX_HOME,
            "CONFIG_PATH": sgate.CONFIG_PATH,
            "CHANNELS_PATH": sgate.CHANNELS_PATH,
            "OPENCODE_CONFIG_PATH": sgate.OPENCODE_CONFIG_PATH,
            "OPENCODE_CREDENTIALS_DIR": sgate.OPENCODE_CREDENTIALS_DIR,
            "CLAUDE_HOME": sgate.CLAUDE_HOME,
            "CLAUDE_CODE_SETTINGS_PATH": sgate.CLAUDE_CODE_SETTINGS_PATH,
            "CLAUDE_DESKTOP_CONFIG_PATH": sgate.CLAUDE_DESKTOP_CONFIG_PATH,
            "CLAUDE_BACKUP_DIR": sgate.CLAUDE_BACKUP_DIR,
            "keychain_get": sgate.keychain_get,
            "keychain_set": sgate.keychain_set,
            "fetch_models": sgate.fetch_models,
            "chatgpt_is_running": sgate.chatgpt_is_running,
            "ccswitch_is_running": sgate.ccswitch_is_running,
        }
        sgate.CODEX_HOME = root
        sgate.CONFIG_PATH = root / "config.toml"
        sgate.CHANNELS_PATH = root / "codex-channels.json"
        sgate.OPENCODE_CONFIG_PATH = root / "opencode.json"
        sgate.OPENCODE_CREDENTIALS_DIR = root / ".sgate"
        sgate.CLAUDE_HOME = root / ".claude"
        sgate.CLAUDE_CODE_SETTINGS_PATH = sgate.CLAUDE_HOME / "settings.json"
        sgate.CLAUDE_DESKTOP_CONFIG_PATH = root / "claude_desktop_config.json"
        sgate.CLAUDE_BACKUP_DIR = sgate.CLAUDE_HOME / "sgate-backups"
        sgate.keychain_get = lambda slug: f"secret-for-{slug}"
        sgate.chatgpt_is_running = lambda: False
        sgate.ccswitch_is_running = lambda: False
        sgate.CONFIG_PATH.write_text(
            'model_provider = "custom"\n'
            'model = "old-model"\n'
            'model_reasoning_effort = "low"\n'
            'model_catalog_json = "old-catalog.json"\n\n'
            '[model_providers.custom]\n'
            'name = "Old provider"\n'
            'base_url = "https://old.example/v1"\n'
            'wire_api = "responses"\n',
            encoding="utf-8",
        )
        sgate.save_channels({
            "version": 1,
            "active": None,
            "channels": {
                "test": {
                    "slug": "test",
                    "name": "Test",
                    "base_url": "https://new.example/v1",
                    "model": "model-1",
                    "reasoning_effort": "high",
                    "models": ["model-1", "model-2", "gpt-image-2"],
                },
                "second": {
                    "slug": "second",
                    "name": "Second",
                    "base_url": "https://second.example/v1",
                    "model": "s-model-1",
                    "reasoning_effort": "low",
                    "models": ["s-model-1", "s-model-2"],
                },
            },
        })

    def tearDown(self) -> None:
        for name, value in self.originals.items():
            setattr(sgate, name, value)
        self.temp.cleanup()

    def test_select_and_disable_round_trip(self) -> None:
        sgate.select_channel("test", model="model-2", effort="medium")
        active = sgate.current_config_info()
        self.assertEqual(active["provider_id"], "codex_channel_test")
        self.assertEqual(active["model"], "model-2")
        self.assertEqual(active["reasoning_effort"], "medium")

        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["fallback"]["model_provider"], "custom")
        self.assertTrue(data["channels"]["test"]["enabled"])

        sgate.deactivate_channel()
        restored = sgate.current_config_info()
        self.assertEqual(restored["provider_id"], "custom")
        self.assertEqual(restored["model"], "old-model")
        self.assertEqual(restored["reasoning_effort"], "low")
        self.assertEqual(restored["model_catalog_json"], "old-catalog.json")
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(list(sgate.CODEX_HOME.glob("config.toml.sgate-*.bak"))), 2)
        self.assertIsNone(data["active"])
        self.assertFalse(data["channels"]["test"]["enabled"])


    def test_multiple_models_and_efforts_are_written_to_catalog(self) -> None:
        sgate.select_channel(
            "test",
            model="model-2",
            effort="xhigh",
            selected_models=["model-1", "model-2"],
            selected_efforts=["low", "medium", "high", "xhigh"],
        )
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        channel = data["channels"]["test"]
        self.assertEqual(channel["selected_models"], ["model-1", "model-2"])
        self.assertEqual(channel["selected_efforts"], ["low", "medium", "high", "xhigh"])

        catalog_path = sgate.channel_catalog_path()
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        self.assertEqual([item["slug"] for item in catalog["models"]], ["model-2", "model-1"])
        for item in catalog["models"]:
            self.assertEqual(item["default_reasoning_level"], "xhigh")
            self.assertEqual(
                [level["effort"] for level in item["supported_reasoning_levels"]],
                ["low", "medium", "high", "xhigh"],
            )
        active = sgate.current_config_info()
        self.assertEqual(active["model_catalog_json"], str(catalog_path))

    def test_model_order_prioritizes_code_models(self) -> None:
        models = ["gpt-image-2", "other-model", "gpt-5.6", "codex-auto-review"]
        ordered = sorted(models, key=lambda name: sgate._model_sort_key(name, None))
        self.assertIn(ordered[0], {"gpt-5.6", "codex-auto-review"})
        self.assertEqual(ordered[-1], "gpt-image-2")

    def test_raw_key_collapses_crlf_without_losing_next_key(self) -> None:
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, b"\r\nx")
            self.assertEqual(sgate._raw_key(read_fd), "enter")
            self.assertEqual(sgate._raw_key(read_fd), "x")
        finally:
            os.close(read_fd)
            os.close(write_fd)

    def test_opencode_channel_writes_provider_model_and_variant(self) -> None:
        sgate.select_opencode_channel(
            "test",
            model="model-2",
            effort="xhigh",
        )
        config = json.loads(sgate.OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
        provider_id = "sgate_test"
        self.assertEqual(config["model"], f"{provider_id}/model-2")
        self.assertEqual(config["agent"]["build"]["variant"], "xhigh")
        self.assertEqual(config["agent"]["build"]["model"], f"{provider_id}/model-2")
        provider = config["provider"][provider_id]
        self.assertEqual(provider["options"]["apiKey"], "{file:" + str(sgate.opencode_credentials_path("test")) + "}")
        self.assertEqual(provider["models"]["model-2"]["variants"]["xhigh"]["reasoningEffort"], "xhigh")
        self.assertEqual(sgate.opencode_credentials_path("test").read_text(encoding="utf-8").strip(), "secret-for-test")
        self.assertEqual(sgate.current_opencode_info()["reasoning_effort"], "xhigh")

    def test_interactive_add_defers_tool_specific_choices(self) -> None:
        args = type("Args", (), {
            "name": "Deferred",
            "slug": "deferred",
            "base_url": "https://deferred.example/v1",
            "model": None,
            "reasoning": None,
            "force": False,
            "use": False,
            "restart_app": False,
        })()
        with patch.object(sgate.getpass, "getpass", return_value="secret"), \
                patch.object(sgate, "fetch_models", return_value=(["model-1", "model-2"], None)), \
                patch.object(sgate, "keychain_set") as save_key:
            sgate.add_channel(args, configure=False)
        save_key.assert_called_once_with("deferred", "secret")
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        channel = data["channels"]["deferred"]
        self.assertEqual(channel["models"], ["model-1", "model-2"])
        self.assertEqual(channel["selected_models"], [])
        self.assertEqual(channel["selected_efforts"], [])
        self.assertFalse(channel["enabled"])
        self.assertFalse(channel["opencode_enabled"])

    def test_updating_channel_preserves_opencode_selection(self) -> None:
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        data["channels"]["test"].update({
            "opencode_model": "oc-old",
            "opencode_reasoning_effort": "low",
            "opencode_selected_models": ["oc-old"],
            "opencode_selected_efforts": ["low"],
            "opencode_enabled": True,
        })
        sgate.save_channels(data)
        args = type("Args", (), {
            "name": "Updated",
            "slug": "test",
            "base_url": "https://updated.example/v1",
            "model": None,
            "reasoning": None,
            "force": True,
            "use": False,
            "restart_app": False,
        })()
        with patch.object(sgate.getpass, "getpass", return_value="secret"), \
                patch.object(sgate, "fetch_models", return_value=(["model-3"], None)), \
                patch.object(sgate, "keychain_set"):
            sgate.add_channel(args, configure=False)
        updated = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))["channels"]["test"]
        self.assertEqual(updated["opencode_model"], "oc-old")
        self.assertEqual(updated["opencode_selected_models"], ["oc-old"])
        self.assertTrue(updated["opencode_enabled"])

    def test_codex_picker_does_not_require_opencode_config(self) -> None:
        sgate.OPENCODE_CONFIG_PATH.write_text("not-json", encoding="utf-8")
        with patch.object(sgate, "terminal_menu", return_value=None):
            self.assertIsNone(sgate.choose_channel("Codex", runtime="codex"))

    def test_opencode_keeps_multiple_providers_enabled(self) -> None:
        sgate.select_opencode_channel("test", model="model-2", effort="high")
        sgate.select_opencode_channel("second", model="s-model-2", effort="low")
        config = json.loads(sgate.OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))

        # Both providers must coexist; enabling one must not drop the other.
        self.assertIn("sgate_test", config["provider"])
        self.assertIn("sgate_second", config["provider"])
        # The most recent selection becomes the default model.
        self.assertEqual(config["model"], "sgate_second/s-model-2")
        self.assertEqual(config["agent"]["build"]["variant"], "low")
        self.assertEqual(
            sorted(sgate.opencode_enabled_slugs()), ["second", "test"]
        )
        # Each provider keeps its own credential file.
        self.assertTrue(sgate.opencode_credentials_path("test").exists())
        self.assertTrue(sgate.opencode_credentials_path("second").exists())

    def test_opencode_add_without_default_preserves_current_default(self) -> None:
        sgate.select_opencode_channel("test", model="model-2", effort="high")
        sgate.select_opencode_channel(
            "second", model="s-model-2", effort="low", make_default=False
        )
        config = json.loads(sgate.OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertIn("sgate_second", config["provider"])
        self.assertEqual(config["model"], "sgate_test/model-2")
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["opencode_active"], "test")

    def test_opencode_sync_replaces_enabled_set(self) -> None:
        sgate.select_opencode_channel("test", model="model-2", effort="high")
        sgate.sync_opencode_channels(["second"], "second")
        config = json.loads(sgate.OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("sgate_test", config.get("provider", {}))
        self.assertIn("sgate_second", config["provider"])
        # sync reuses each channel's own saved default model.
        self.assertEqual(config["model"], "sgate_second/s-model-1")
        self.assertEqual(sgate.opencode_enabled_slugs(), ["second"])

    def test_disabling_default_promotes_remaining_channel(self) -> None:
        sgate.select_opencode_channel("test", model="model-2", effort="high")
        sgate.select_opencode_channel(
            "second", model="s-model-2", effort="low", make_default=False
        )
        sgate.deactivate_opencode_channel("test")
        config = json.loads(sgate.OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertNotIn("sgate_test", config.get("provider", {}))
        self.assertEqual(config["model"], "sgate_second/s-model-2")
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(data["opencode_active"], "second")

    def test_disabling_last_channel_restores_original_config(self) -> None:
        sgate.OPENCODE_CONFIG_PATH.write_text(
            json.dumps({"model": "other/original", "agent": {"build": {"variant": "medium"}}}),
            encoding="utf-8",
        )
        sgate.select_opencode_channel("test", model="model-2", effort="high")
        sgate.deactivate_opencode_channel("test")
        config = json.loads(sgate.OPENCODE_CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(config["model"], "other/original")
        self.assertNotIn("sgate_test", config.get("provider", {}))

    def test_styling_is_plain_without_color_support(self) -> None:
        with patch.dict(os.environ, {"NO_COLOR": "1"}, clear=False):
            self.assertFalse(sgate.color_enabled())
            self.assertEqual(sgate.bold("x"), "x")
            self.assertEqual(sgate.green("ok"), "ok")

    def test_display_width_handles_cjk_and_ansi(self) -> None:
        with patch.dict(os.environ, {"SGATE_FORCE_COLOR": "1"}, clear=False):
            colored = sgate.green("ok")
            self.assertNotEqual(colored, "ok")
            # ANSI escapes must not count toward visible width.
            self.assertEqual(sgate.display_width(colored), 2)
        self.assertEqual(sgate.display_width("渠道"), 4)
        self.assertEqual(sgate.display_width("ab"), 2)
        self.assertEqual(sgate.display_width(sgate.pad("渠道", 6)), 6)

    def test_opencode_refresh_keeps_codex_selection_separate(self) -> None:
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        data["channels"]["test"].update({
            "model": "codex-model",
            "selected_models": ["codex-model"],
            "reasoning_effort": "high",
            "selected_efforts": ["high"],
            "opencode_model": "open-model",
            "opencode_selected_models": ["open-model"],
            "opencode_reasoning_effort": "low",
            "opencode_selected_efforts": ["low"],
        })
        sgate.save_channels(data)
        with patch.object(sgate, "keychain_get", return_value="secret"), \
                patch.object(sgate, "fetch_models", return_value=(["open-model", "open-model-2"], None)), \
                patch.object(sgate, "choose_models", return_value=(["open-model-2"], "open-model-2")):
            sgate.refresh_models("test", runtime="opencode")
        updated = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))["channels"]["test"]
        self.assertEqual(updated["selected_models"], ["codex-model"])
        self.assertEqual(updated["model"], "codex-model")
        self.assertEqual(updated["opencode_selected_models"], ["open-model-2"])
        self.assertEqual(updated["opencode_model"], "open-model-2")

    def test_claude_planner_requires_complete_explicit_role_map(self) -> None:
        profile = {
            "slug": "test",
            "protocols": {"anthropic": {"base_url": "https://anthropic.example"}},
            "runtimes": {"claude_code": {"default_role": "sonnet", "effort": "high", "model_map": {
                "opus": "gw-opus", "sonnet": "gw-sonnet", "haiku": "gw-haiku"
            }}},
        }
        plan = sgate.compile_claude_managed_values(profile)
        self.assertTrue(plan.supported)
        self.assertEqual(plan.desired["/model"], "sonnet")
        self.assertEqual(plan.desired["/env/ANTHROPIC_DEFAULT_OPUS_MODEL"], "gw-opus")
        self.assertNotIn("/env/ANTHROPIC_MODEL", plan.desired)
        profile["runtimes"]["claude_code"]["model_map"].pop("haiku")
        self.assertFalse(sgate.compile_claude_managed_values(profile).supported)

    def test_claude_shared_openai_url_is_not_inferred(self) -> None:
        channel = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))["channels"]["test"]
        self.assertEqual(sgate.claude_channel_base_url(channel), "")
        profile = sgate._claude_profile(channel, model_map={"opus": "o", "sonnet": "s", "haiku": "h"}, default_role="sonnet", effort="high")
        plan = sgate.compile_claude_managed_values(profile)
        self.assertFalse(plan.supported)
        self.assertTrue(any("shared OpenAI base_url" in item for item in plan.diagnostics))

    def test_claude_endpoint_root_and_secret_ref_are_strict(self) -> None:
        base = {
            "slug": "test",
            "protocols": {"anthropic": {"base_url": "https://anthropic.example/v1", "auth": {"secret_ref": {"slug": "bad"}}}},
            "runtimes": {"claude_code": {"default_role": "sonnet", "effort": "high", "model_map": {"opus": "o", "sonnet": "s", "haiku": "h"}}},
        }
        plan = sgate.compile_claude_managed_values(base)
        self.assertFalse(plan.supported)
        self.assertTrue(any("ending in /v1" in item for item in plan.diagnostics))
        self.assertTrue(any("secret_ref" in item for item in plan.diagnostics))
        self.assertNotIn("apiKeyHelper", plan.desired)

    def test_claude_profile_malformed_containers_fail_closed(self) -> None:
        for profile in ({"protocols": []}, {"protocols": {"anthropic": []}}, {"runtimes": []}, {"runtimes": {"claude_code": []}}):
            plan = sgate.compile_claude_managed_values(profile)
            self.assertFalse(plan.supported)
            self.assertTrue(plan.diagnostics)

    def test_claude_code_writes_independent_anthropic_role_map(self) -> None:
        original = {
            "permissions": {"allow": ["Bash(pwd)"]},
            "hooks": {"Stop": ["keep"]},
            "env": {"ANTHROPIC_AUTH_TOKEN": "old-secret", "KEEP_ME": "yes"},
            "model": "old-model",
        }
        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps(original), encoding="utf-8")
        with patch.object(sgate, "keychain_set") as save_key:
            sgate.select_claude_code_channel(
                "test", anthropic_base_url="https://anthropic.example", default_role="opus", effort="high",
                model_map={"opus": "gw-opus", "sonnet": "gw-sonnet", "haiku": "gw-haiku"},
            )
        save_key.assert_called_once_with(sgate.CLAUDE_ORIGINAL_KEY_SLUG, "old-secret")
        settings = json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
        env = settings["env"]
        self.assertEqual(settings["permissions"], original["permissions"])
        self.assertEqual(settings["hooks"], original["hooks"])
        self.assertEqual(env["KEEP_ME"], "yes")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("ANTHROPIC_MODEL", env)
        self.assertNotIn("CLAUDE_CODE_EFFORT_LEVEL", env)
        self.assertEqual(settings["model"], "opus")
        self.assertEqual(settings["effortLevel"], "high")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://anthropic.example")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "gw-haiku")
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        entry = data["runtime_state"]["claude_code"]["takeover"]["entries"]
        auth_entry = next(item for item in entry if item["path"] == "/env/ANTHROPIC_AUTH_TOKEN")
        self.assertNotIn("old-secret", json.dumps(data))
        self.assertEqual(auth_entry["before"], {"keychain_restore_ref": sgate.CLAUDE_ORIGINAL_KEY_SLUG})

    def test_claude_code_disable_restores_settings_and_preserves_changes(self) -> None:
        original = {"env": {"ANTHROPIC_AUTH_TOKEN": "original-secret", "KEEP_ME": "yes"}, "model": "sonnet"}
        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps(original), encoding="utf-8")
        with patch.object(sgate, "keychain_set"), patch.object(sgate, "keychain_get", return_value="original-secret"):
            sgate.select_claude_code_channel(
                "test", anthropic_base_url="https://anthropic.example", default_role="sonnet", effort="high",
                model_map={"opus": "gw-opus", "sonnet": "gw-sonnet", "haiku": "gw-haiku"},
            )
            sgate.deactivate_claude_code_channel("test")
        restored = json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(restored["model"], "sonnet")
        self.assertEqual(restored["env"]["KEEP_ME"], "yes")
        self.assertEqual(restored["env"]["ANTHROPIC_AUTH_TOKEN"], "original-secret")

    def test_claude_map_all_and_cli_parser(self) -> None:
        self.assertEqual(sgate._parse_model_map([], "same"), {role: "same" for role in sgate.CLAUDE_ROLES})
        args = sgate.build_parser().parse_args([
            "claude-code", "use", "test", "--anthropic-base-url", "https://a.example",
            "--map", "opus=o", "--map", "sonnet=s", "--map", "haiku=h", "--default-role", "haiku", "--effort", "low",
        ])
        self.assertEqual(args.slug, "test")
        self.assertEqual(args.model_maps, ["opus=o", "sonnet=s", "haiku=h"])
        self.assertEqual(args.effort, "low")

    def test_legacy_claude_fields_migrate_without_guessing(self) -> None:
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        data["channels"]["test"].update({
            "claude_model": "legacy-sonnet",
            "claude_selected_models": ["legacy-sonnet"],
            "claude_reasoning_effort": "xhigh",
        })
        sgate.save_channels(data)
        migrated = sgate.load_channels()["channels"]["test"]
        anthropic = migrated["protocols"]["anthropic"]
        self.assertEqual(anthropic["migration_status"], "needs_configuration")
        self.assertEqual(anthropic["legacy_hint"]["claude_model"], "legacy-sonnet")
        self.assertNotIn("base_url", anthropic)
        self.assertNotIn("models", anthropic)
        self.assertNotIn("runtimes", migrated)

    def test_claude_disable_preserves_conflicting_local_edit(self) -> None:
        original = {"model": "old", "env": {"KEEP": "yes"}}
        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps(original), encoding="utf-8")
        sgate.select_claude_code_channel(
            "test", anthropic_base_url="https://anthropic.example", default_role="sonnet", effort="high",
            model_map={"opus": "o", "sonnet": "s", "haiku": "h"},
        )
        local = json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
        local["model"] = "user-choice"
        local["permissions"] = {"allow": ["Bash(pwd)"]}
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps(local), encoding="utf-8")
        sgate.deactivate_claude_code_channel("test")
        restored = json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
        # A managed-path conflict is fail-closed: the local file and journal
        # remain untouched so the user can retry after deciding what to keep.
        self.assertEqual(restored, local)
        state = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(state["claude_active"], "test")
        self.assertIn("takeover", state["runtime_state"]["claude_code"])

    def test_claude_switch_keeps_first_before_value(self) -> None:
        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps({"model": "before"}), encoding="utf-8")
        model_map = {"opus": "o", "sonnet": "s", "haiku": "h"}
        sgate.select_claude_code_channel(
            "test", anthropic_base_url="https://a.example", default_role="sonnet", effort="high", model_map=model_map,
        )
        sgate.select_claude_code_channel(
            "second", anthropic_base_url="https://b.example", default_role="opus", effort="low", model_map=model_map,
        )
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        entries = data["runtime_state"]["claude_code"]["takeover"]["entries"]
        model_entry = next(item for item in entries if item["path"] == "/model")
        self.assertEqual(model_entry["before"], "before")
        self.assertEqual(model_entry["applied"], "opus")
        sgate.deactivate_claude_code_channel("second")
        restored = json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(restored["model"], "before")

    def test_claude_disable_restores_file_existence_and_empty_env(self) -> None:
        model_map = {"opus": "o", "sonnet": "s", "haiku": "h"}
        sgate.select_claude_code_channel(
            "test", anthropic_base_url="https://a.example", default_role="sonnet", effort="high", model_map=model_map,
        )
        sgate.deactivate_claude_code_channel("test")
        self.assertFalse(sgate.CLAUDE_CODE_SETTINGS_PATH.exists())

        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps({"env": {}}), encoding="utf-8")
        sgate.select_claude_code_channel(
            "test", anthropic_base_url="https://a.example", default_role="sonnet", effort="high", model_map=model_map,
        )
        sgate.deactivate_claude_code_channel("test")
        self.assertEqual(json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8")), {"env": {}})

    def test_claude_env_wrong_shape_fails_closed(self) -> None:
        for invalid in ("not-an-object", ["bad"], None):
            sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps({"env": invalid}), encoding="utf-8")
            before = sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8")
            with self.assertRaises(SystemExit):
                sgate.select_claude_code_channel(
                    "test", anthropic_base_url="https://a.example", default_role="sonnet", effort="high",
                    model_map={"opus": "o", "sonnet": "s", "haiku": "h"},
                )
            self.assertEqual(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"), before)

    def test_claude_journal_target_path_is_fail_closed_before_restore(self) -> None:
        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps({"model": "before"}), encoding="utf-8")
        sgate.select_claude_code_channel(
            "test", anthropic_base_url="https://a.example", default_role="sonnet", effort="high",
            model_map={"opus": "o", "sonnet": "s", "haiku": "h"},
        )
        settings_before = sgate.CLAUDE_CODE_SETTINGS_PATH.read_bytes()
        backups_before = sorted(sgate.CLAUDE_BACKUP_DIR.glob("*.bak"))
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        data["runtime_state"]["claude_code"]["takeover"]["target_path"] = str(Path(self.temp.name) / "other.json")
        sgate.CHANNELS_PATH.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaises(SystemExit):
            sgate.deactivate_claude_code_channel("test")
        self.assertEqual(sgate.CLAUDE_CODE_SETTINGS_PATH.read_bytes(), settings_before)
        self.assertEqual(sorted(sgate.CLAUDE_BACKUP_DIR.glob("*.bak")), backups_before)

    def test_claude_profile_transition_rejects_external_edit_without_writes(self) -> None:
        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps({"model": "before"}), encoding="utf-8")
        maps = {"opus": "o", "sonnet": "s", "haiku": "h"}
        sgate.select_claude_code_channel(
            "test", anthropic_base_url="https://a.example", default_role="sonnet", effort="high", model_map=maps,
        )
        data_before = sgate.CHANNELS_PATH.read_bytes()
        backups_before = sorted(sgate.CLAUDE_BACKUP_DIR.glob("*.bak"))
        edited = json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
        edited["model"] = "user-model"
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps(edited), encoding="utf-8")
        with self.assertRaises(SystemExit):
            sgate.select_claude_code_channel(
                "second", anthropic_base_url="https://b.example", default_role="opus", effort="low", model_map=maps,
            )
        self.assertEqual(sgate.CHANNELS_PATH.read_bytes(), data_before)
        self.assertEqual(sorted(sgate.CLAUDE_BACKUP_DIR.glob("*.bak")), backups_before)

    def test_claude_effort_runtime_is_authoritative_and_conflicts_fail(self) -> None:
        profile = {
            "slug": "test",
            "protocols": {"anthropic": {"base_url": "https://a.example"}},
            "runtimes": {"claude_code": {
                "default_role": "sonnet", "effort": "high",
                "model_map": {"opus": "o", "sonnet": "s", "haiku": "h"},
            }},
            "claude_effort": "low",
        }
        plan = sgate.compile_claude_managed_values(profile)
        self.assertFalse(plan.supported)
        self.assertTrue(any("effort conflict" in item for item in plan.diagnostics))

    def test_claude_journal_distinguishes_json_null_from_missing(self) -> None:
        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps({"model": None}), encoding="utf-8")
        sgate.select_claude_code_channel(
            "test", anthropic_base_url="https://a.example", default_role="sonnet", effort="high",
            model_map={"opus": "o", "sonnet": "s", "haiku": "h"},
        )
        sgate.deactivate_claude_code_channel("test")
        restored = json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertIn("model", restored)
        self.assertIsNone(restored["model"])

    def test_claude_journal_explicit_exists_avoids_marker_collision(self) -> None:
        settings = {"model": {"__sgate_journal_missing__": True}}
        sgate.CLAUDE_CODE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        sgate.CLAUDE_CODE_SETTINGS_PATH.write_text(json.dumps(settings), encoding="utf-8")
        sgate.select_claude_code_channel(
            "test", anthropic_base_url="https://a.example", default_role="sonnet", effort="high",
            model_map={"opus": "o", "sonnet": "s", "haiku": "h"},
        )
        data = json.loads(sgate.CHANNELS_PATH.read_text(encoding="utf-8"))
        entry = next(item for item in data["runtime_state"]["claude_code"]["takeover"]["entries"] if item["path"] == "/model")
        self.assertTrue(entry["before_exists"])
        sgate.deactivate_claude_code_channel("test")
        restored = json.loads(sgate.CLAUDE_CODE_SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(restored, settings)

    def test_claude_desktop_use_never_rewrites_desktop_json(self) -> None:
        original = {"mcpServers": {"demo": {"command": "demo"}}, "other": True}
        sgate.CLAUDE_DESKTOP_CONFIG_PATH.write_text(json.dumps(original), encoding="utf-8")
        sgate.select_claude_desktop_channel(
            "test", anthropic_base_url="https://a.example", default_role="haiku", effort="medium",
            model_map={"opus": "o", "sonnet": "s", "haiku": "h"},
        )
        self.assertEqual(json.loads(sgate.CLAUDE_DESKTOP_CONFIG_PATH.read_text(encoding="utf-8")), original)

    def test_claude_desktop_status_reads_mcp_without_rewriting_config(self) -> None:
        original = {"deploymentMode": "3p", "mcpServers": {"demo": {"command": ["demo"]}}}
        sgate.CLAUDE_DESKTOP_CONFIG_PATH.write_text(json.dumps(original), encoding="utf-8")
        with patch("builtins.print") as output:
            sgate.claude_desktop_status()
        self.assertEqual(
            json.loads(sgate.CLAUDE_DESKTOP_CONFIG_PATH.read_text(encoding="utf-8")),
            original,
        )
        rendered = " ".join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn("MCP", rendered)
        self.assertIn("不提供自定义 API", rendered)


if __name__ == "__main__":
    unittest.main()
