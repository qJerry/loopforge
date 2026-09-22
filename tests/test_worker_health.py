from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from loopforge.config import ProjectProfile
from loopforge.worker_health import detect_worker_health


def completed(args: list[str], returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, returncode, stdout, stderr)


class WorkerHealthTests(unittest.TestCase):
    @patch("loopforge.worker_health.resolve_claude_binary", return_value="/tools/claude")
    @patch("loopforge.worker_health.resolve_codex_binary", return_value="/tools/codex")
    def test_project_proxy_environment_is_used_by_all_health_probes(self, _codex, _claude) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("/workspace/demo"))
        profile.worker_network = {
            "mode": "custom",
            "http_proxy": "http://127.0.0.1:8899",
            "https_proxy": "http://127.0.0.1:8899",
            "all_proxy": "socks5://127.0.0.1:8899",
        }
        runner = unittest.mock.Mock(
            side_effect=[
                completed([], stdout="codex-cli 1.0"),
                completed([], stdout="Logged in using ChatGPT"),
                completed([], stdout="Claude 2.0"),
                completed([], stdout='{"loggedIn": true, "authMethod": "claude.ai"}'),
                completed([], stdout="No installation issues found."),
            ]
        )

        payload = detect_worker_health(profile=profile, runner=runner)

        self.assertTrue(payload["providers"]["claude"]["ready"])
        for call in runner.call_args_list:
            env = call.kwargs["env"]
            self.assertEqual(env["HTTP_PROXY"], "http://127.0.0.1:8899")
            self.assertEqual(env["https_proxy"], "http://127.0.0.1:8899")
            self.assertEqual(env["ALL_PROXY"], "socks5://127.0.0.1:8899")

    @patch("loopforge.worker_health.resolve_claude_binary", return_value="/tools/claude")
    @patch("loopforge.worker_health.resolve_codex_binary", return_value="/tools/codex")
    def test_reports_both_ready_without_exposing_claude_identity(self, _codex, _claude) -> None:
        private_auth = {
            "loggedIn": True,
            "authMethod": "claude.ai",
            "email": "private@example.com",
            "orgId": "secret-org",
            "apiKey": "secret-key",
        }
        runner = unittest.mock.Mock(
            side_effect=[
                completed([], stdout="codex-cli 1.0\n"),
                completed([], stdout="Logged in using ChatGPT\n"),
                completed([], stdout="2.0.76 (Claude Code)\n"),
                completed([], stdout=json.dumps(private_auth)),
                completed([], stdout="No installation issues found.\n"),
            ]
        )

        payload = detect_worker_health(runner=runner)

        self.assertTrue(payload["providers"]["codex"]["ready"])
        self.assertEqual(payload["providers"]["codex"]["auth_method"], "chatgpt")
        self.assertTrue(payload["providers"]["claude"]["ready"])
        self.assertEqual(payload["providers"]["claude"]["auth_method"], "claude.ai")
        serialized = json.dumps(payload)
        self.assertNotIn("private@example.com", serialized)
        self.assertNotIn("secret-org", serialized)
        self.assertNotIn("secret-key", serialized)

    @patch("loopforge.worker_health.resolve_claude_binary", return_value="/tools/claude")
    @patch("loopforge.worker_health.resolve_codex_binary", return_value="missing-codex")
    def test_missing_codex_does_not_hide_claude_result(self, _codex, _claude) -> None:
        runner = unittest.mock.Mock(
            side_effect=[
                FileNotFoundError("missing"),
                completed([], stdout="2.0.76 (Claude Code)\n"),
                completed([], stdout='{"loggedIn": true, "authMethod": "claude.ai"}'),
                completed([], stdout="No installation issues found.\n"),
            ]
        )

        payload = detect_worker_health(runner=runner)

        self.assertEqual(payload["providers"]["codex"]["status"], "missing")
        self.assertFalse(payload["providers"]["codex"]["ready"])
        self.assertTrue(payload["providers"]["claude"]["ready"])

    @patch("loopforge.worker_health.resolve_claude_binary", return_value="/tools/claude")
    @patch("loopforge.worker_health.resolve_codex_binary", return_value="/tools/codex")
    def test_unauthenticated_providers_return_actions(self, _codex, _claude) -> None:
        runner = unittest.mock.Mock(
            side_effect=[
                completed([], stdout="codex-cli 1.0"),
                completed([], returncode=1, stderr="Not logged in. Please run codex login"),
                completed([], stdout="Claude 2.0"),
                completed([], returncode=1, stderr="Invalid API key · Please run /login"),
            ]
        )

        payload = detect_worker_health(runner=runner)

        for provider in ("codex", "claude"):
            self.assertEqual(payload["providers"][provider]["status"], "unauthenticated")
            self.assertTrue(payload["providers"][provider]["required_action"])

    @patch("loopforge.worker_health.resolve_claude_binary", return_value="/tools/claude")
    @patch("loopforge.worker_health.resolve_codex_binary", return_value="/tools/codex")
    def test_timeout_and_invalid_auth_output_are_isolated(self, _codex, _claude) -> None:
        runner = unittest.mock.Mock(
            side_effect=[
                subprocess.TimeoutExpired(["codex", "--version"], timeout=1),
                completed([], stdout="Claude 2.0"),
                completed([], stdout="not-json"),
            ]
        )

        payload = detect_worker_health(timeout_seconds=1, runner=runner)

        self.assertEqual(payload["providers"]["codex"]["status"], "error")
        self.assertIn("超时", payload["providers"]["codex"]["issue"])
        self.assertEqual(payload["providers"]["claude"]["status"], "error")
        self.assertIn("无法确认", payload["providers"]["claude"]["issue"])

    @patch("loopforge.worker_health.resolve_claude_binary", return_value="/tools/claude")
    @patch("loopforge.worker_health.resolve_codex_binary", return_value="/tools/codex")
    def test_version_failure_does_not_run_auth_probe(self, _codex, _claude) -> None:
        runner = unittest.mock.Mock(
            side_effect=[
                completed([], returncode=2, stderr="broken"),
                completed([], stdout="Claude 2.0"),
                completed([], stdout='{"loggedIn": true, "authMethod": "claude.ai"}'),
                completed([], stdout="No installation issues found.\n"),
            ]
        )

        payload = detect_worker_health(runner=runner)

        self.assertEqual(runner.call_count, 4)
        self.assertEqual(payload["providers"]["codex"]["status"], "error")
        self.assertTrue(payload["providers"]["claude"]["ready"])

    @patch("loopforge.worker_health.resolve_claude_binary", return_value="/tools/claude")
    @patch("loopforge.worker_health.resolve_codex_binary", return_value="/tools/codex")
    def test_claude_doctor_403_overrides_logged_in_state(self, _codex, _claude) -> None:
        runner = unittest.mock.Mock(
            side_effect=[
                completed([], stdout="codex-cli 1.0"),
                completed([], stdout="Logged in using ChatGPT"),
                completed([], stdout="Claude 2.0"),
                completed([], stdout='{"loggedIn": true, "authMethod": "claude.ai"}'),
                completed([], stdout="Organization policy: api.anthropic.com refused the request (HTTP 403)"),
            ]
        )

        payload = detect_worker_health(runner=runner)

        claude = payload["providers"]["claude"]
        self.assertTrue(claude["authenticated"])
        self.assertFalse(claude["ready"])
        self.assertEqual(claude["status"], "error")
        self.assertIn("HTTP 403", claude["issue"])
        self.assertNotIn("api.anthropic.com refused", json.dumps(payload))
