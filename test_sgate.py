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
                }
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


if __name__ == "__main__":
    unittest.main()
