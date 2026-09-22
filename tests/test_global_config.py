from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopforge.commands import global_settings
from loopforge.global_config import read_global_config, update_wecom_config


class GlobalConfigTests(unittest.TestCase):
    def test_global_settings_exposes_non_persistent_onboarding_owner_default(self) -> None:
        with patch("loopforge.commands.getpass.getuser", return_value="galaxy"):
            payload = global_settings()

        self.assertEqual(payload["settings"]["onboarding_defaults"], {"owner": "galaxy"})

    def test_read_global_config_uses_defaults_for_invalid_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "version": "bad",
                        "notifications": {"wecom": {"enabled": True, "webhook_env": "", "webhook_url": "https://example.test/webhook"}},
                        "scan": {"daily_minutes": 0, "initial_minutes": "bad", "budget_exceeded_alert_after": -1},
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            payload = read_global_config(config_path)

        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["notifications"]["wecom"]["webhook_env"], "LOOPFORGE_WECOM_WEBHOOK")
        self.assertEqual(payload["notifications"]["wecom"]["webhook_url"], "https://example.test/webhook")
        self.assertEqual(payload["scan"]["daily_minutes"], 15)
        self.assertEqual(payload["scan"]["initial_minutes"], 45)
        self.assertEqual(payload["scan"]["budget_exceeded_alert_after"], 2)

    def test_update_wecom_config_persists_global_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"

            payload = update_wecom_config(True, webhook_env="LOOPFORGE_TEST_WEBHOOK", webhook_url="", path=config_path)
            persisted = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertTrue(payload["notifications"]["wecom"]["enabled"])
        self.assertEqual(payload["notifications"]["wecom"]["webhook_env"], "LOOPFORGE_TEST_WEBHOOK")
        self.assertEqual(persisted["notifications"]["wecom"]["webhook_env"], "LOOPFORGE_TEST_WEBHOOK")


if __name__ == "__main__":
    unittest.main()
