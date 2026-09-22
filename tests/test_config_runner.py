from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loopforge.config import (
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CLAUDE_PERMISSION_MODE,
    DEFAULT_CODEX_MODEL,
    load_projects,
    profile_to_dict,
)
from loopforge.commands import validate_config
from loopforge.runner import validate_runner_json


class ConfigRunnerTests(unittest.TestCase):
    def test_tracked_project_contract_overrides_local_portable_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            contract = root / ".loopforge" / "project.json"
            contract.parent.mkdir(parents=True)
            contract.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_type": "go-backend",
                    }
                ),
                encoding="utf-8",
            )
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "go-api",
                                "name": "Go API",
                                "root_dir": str(root),
                                "project_type": "python-scripts",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            profile = load_projects(config)[0]

            self.assertTrue(profile.is_valid, profile.errors)
            self.assertEqual(profile.project_type, "go-backend")

    def test_project_group_children_load_tracked_contracts_from_each_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "parent"
            child = root / "admin-ui"
            contract = child / ".loopforge" / "project.json"
            contract.parent.mkdir(parents=True)
            contract.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "project_type": "react-frontend",
                    }
                ),
                encoding="utf-8",
            )
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "group",
                                "name": "Group",
                                "root_dir": str(root),
                                "project_type": "project-group",
                                "project_group": {
                                    "children": [{"key": "admin-ui", "path": str(child)}]
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            profile = load_projects(config)[0]

            self.assertTrue(profile.is_valid, profile.errors)
            loaded = profile.project_group["children"][0]
            self.assertEqual(loaded["project_type"], "react-frontend")

    def test_rejects_invalid_tracked_project_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            contract = root / ".loopforge" / "project.json"
            contract.parent.mkdir(parents=True)
            contract.write_text('{"schema_version":2,"project_type":"go-backend"}', encoding="utf-8")
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps({"projects": [{"id": "broken", "name": "Broken", "root_dir": str(root)}]}),
                encoding="utf-8",
            )

            profile = load_projects(config)[0]

            self.assertFalse(profile.is_valid)
            self.assertTrue(any("schema_version" in error for error in profile.errors), profile.errors)


    def test_project_group_children_keep_independent_bdd_gates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "parent"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "group",
                                "name": "Group",
                                "root_dir": str(root),
                                "project_group": {
                                    "children": [
                                        {
                                            "key": "api",
                                            "path": "api",
                                            "project_type": "go-backend",
                                            "bdd": {
                                                "command": ["scripts/test.sh", "bdd"],
                                                "feature_glob": "bdd/features/**/*.feature",
                                                "runner": "godog",
                                            },
                                        },
                                        {"key": "web", "path": "web"},
                                    ]
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            profile = load_projects(config)[0]

            self.assertTrue(profile.is_valid, profile.errors)
            children = profile.project_group["children"]
            self.assertEqual(children[0]["bdd"]["runner"], "godog")
            self.assertNotIn("bdd", children[1])

    def test_load_fixture_project(self) -> None:
        profiles = load_projects(Path("tests/fixtures/projects.json"))
        by_id = {profile.project_id: profile for profile in profiles}
        self.assertIn("demo", by_id)
        self.assertIn("fake", by_id)
        self.assertTrue(all(profile.is_valid for profile in profiles))
        for profile in [by_id["demo"], by_id["fake"]]:
            self.assertEqual(profile.runner_command, [])
            self.assertEqual(profile.default_agent, "codex")
            self.assertEqual(profile.executor, "fake_codex")
            self.assertEqual(profile.codex_model, DEFAULT_CODEX_MODEL)
            self.assertEqual(profile.codex_reasoning_effort, "high")
            self.assertEqual(profile.codex_sandbox, "workspace-write")
            self.assertEqual(profile.schedule_frequency, "hourly")
            self.assertEqual(profile.automation_mode, "execute")
            self.assertFalse(profile.auto_commit)
        self.assertEqual(by_id["sample-go-service"].automation_mode, "off")
        for profile in profiles:
            profile_payload = profile_to_dict(profile)
            self.assertTrue(Path(profile_payload["root_dir"]).is_absolute())
            self.assertTrue(Path(profile_payload["state_dir"]).is_absolute())
            self.assertTrue(Path(profile_payload["report_dir"]).is_absolute())
            self.assertEqual(profile_payload["codex_model_options"][0]["slug"], DEFAULT_CODEX_MODEL)
            self.assertIn("display_name", profile_payload["codex_model_options"][0])
            self.assertIn(profile_payload["automation_mode"], {"off", "execute"})
            self.assertIn("automation_mode_label", profile_payload)
            self.assertIn("automation_allowed_loops", profile_payload)

    def test_historical_project_defaults_to_codex_worker_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps({"projects": [{"id": "legacy", "name": "Legacy", "root_dir": str(root)}]}),
                encoding="utf-8",
            )

            profile = load_projects(config)[0]
            runtime = profile_to_dict(profile)["worker_runtime"]

            self.assertTrue(profile.is_valid, profile.errors)
            self.assertEqual(profile.executor, "codex_cli")
            self.assertEqual(runtime["provider"], "codex")
            self.assertEqual(runtime["settings"]["model"], DEFAULT_CODEX_MODEL)
            self.assertEqual([item["value"] for item in runtime["provider_options"]], ["codex", "claude"])
            self.assertEqual([item["key"] for item in runtime["controls"]], ["model", "reasoning_effort", "sandbox"])
            self.assertEqual(runtime["providers"]["claude"]["settings"]["permission_mode"], "bypassPermissions")
            self.assertEqual(
                [control["key"] for control in runtime["providers"]["claude"]["controls"]],
                ["model", "permission_mode"],
            )

    def test_claude_worker_runtime_defaults_to_fully_automatic_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps({"projects": [{"id": "claude", "name": "Claude", "root_dir": str(root), "executor": "claude_cli"}]}),
                encoding="utf-8",
            )

            profile = load_projects(config)[0]
            runtime = profile_to_dict(profile)["worker_runtime"]

            self.assertTrue(profile.is_valid, profile.errors)
            self.assertEqual(profile.default_agent, "claude")
            self.assertEqual(profile.claude_model, DEFAULT_CLAUDE_MODEL)
            self.assertEqual(profile.claude_permission_mode, DEFAULT_CLAUDE_PERMISSION_MODE)
            self.assertEqual(runtime["provider"], "claude")
            self.assertEqual(runtime["settings"], {"model": DEFAULT_CLAUDE_MODEL, "permission_mode": "bypassPermissions"})
            self.assertEqual([item["key"] for item in runtime["controls"]], ["model", "permission_mode"])
            self.assertEqual(runtime["controls"][1]["risk"], "high")
            self.assertEqual(runtime["providers"]["codex"]["settings"]["reasoning_effort"], "high")
            self.assertEqual(
                runtime["network"]["settings"],
                {
                    "mode": "inherit",
                    "http_proxy": "",
                    "https_proxy": "",
                    "all_proxy": "",
                },
            )
            self.assertEqual(
                [item["value"] for item in runtime["network"]["controls"][0]["options"]],
                ["custom", "inherit", "direct"],
            )

    def test_rejects_unknown_executor_and_invalid_claude_permission(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {"id": "unknown", "name": "Unknown", "root_dir": str(root), "executor": "other_cli"},
                            {
                                "id": "claude",
                                "name": "Claude",
                                "root_dir": str(root),
                                "executor": "claude_cli",
                                "claude_permission_mode": "workspace-write",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            profiles = load_projects(config)

            self.assertFalse(profiles[0].is_valid)
            self.assertIn("非法 executor：other_cli", profiles[0].errors)
            self.assertFalse(profiles[1].is_valid)
            self.assertIn("非法 Claude 权限模式：workspace-write", profiles[1].errors)

    def test_fake_executor_exposes_read_only_worker_runtime(self) -> None:
        profile = next(item for item in load_projects(Path("tests/fixtures/projects.json")) if item.executor == "fake_codex")

        runtime = profile_to_dict(profile)["worker_runtime"]

        self.assertEqual(runtime["provider"], "fake")
        self.assertEqual(runtime["provider_label"], "演示 Worker")
        self.assertFalse(runtime["selectable"])
        self.assertEqual(runtime["controls"], [])

    def test_automation_mode_overrides_legacy_schedule_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "observe",
                                "name": "Observe",
                                "root_dir": str(root),
                                "schedule_enabled": True,
                                "automation_mode": "observe",
                                "notification_channel": "none",
                            },
                            {
                                "id": "off",
                                "name": "Off",
                                "root_dir": str(root),
                                "schedule_enabled": True,
                                "automation_mode": "off",
                                "notification_channel": "none",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profiles = {profile.project_id: profile for profile in load_projects(config)}
            self.assertTrue(profiles["observe"].schedule_enabled)
            self.assertEqual(profiles["observe"].automation_mode, "observe")
            self.assertFalse(profiles["off"].schedule_enabled)
            self.assertEqual(profiles["off"].automation_mode, "off")

    def test_invalid_automation_mode_is_misconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "broken",
                                "name": "Broken",
                                "root_dir": str(root),
                                "automation_mode": "danger",
                                "notification_channel": "none",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile = load_projects(config)[0]
            self.assertFalse(profile.is_valid)
            self.assertEqual(profile.automation_mode, "off")
            self.assertIn("非法自动化模式：danger", profile.errors)

    def test_validate_config_success(self) -> None:
        payload = validate_config(Path("tests/fixtures/projects.json"))
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["valid"])

    def test_wecom_without_project_webhook_uses_global_notification_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "broken",
                                "name": "Broken",
                                "root_dir": str(root),
                                "notification_channel": "wecom_robot",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile = load_projects(config)[0]
            self.assertTrue(profile.is_valid)
            self.assertEqual(profile.notification_channel, "wecom_robot")

    def test_invalid_schedule_frequency_is_misconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "broken",
                                "name": "Broken",
                                "root_dir": str(root),
                                "schedule_frequency": "monthly",
                                "notification_channel": "none",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile = load_projects(config)[0]
            self.assertFalse(profile.is_valid)
            self.assertIn("非法调度频率：monthly", profile.errors)

    def test_invalid_codex_reasoning_effort_is_misconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "broken",
                                "name": "Broken",
                                "root_dir": str(root),
                                "codex_reasoning_effort": "extreme",
                                "notification_channel": "none",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile = load_projects(config)[0]
            self.assertFalse(profile.is_valid)
            self.assertIn("非法 Codex reasoning 级别：extreme", profile.errors)

    def test_invalid_codex_sandbox_is_misconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "broken",
                                "name": "Broken",
                                "root_dir": str(root),
                                "codex_sandbox": "browser",
                                "notification_channel": "none",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile = load_projects(config)[0]
            self.assertFalse(profile.is_valid)
            self.assertIn("非法 Codex sandbox：browser", profile.errors)

    def test_project_group_is_loaded_and_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "parent"
            root.mkdir()
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "ad",
                                "name": "AD",
                                "root_dir": str(root),
                                "notification_channel": "none",
                                "project_group": {
                                    "key": "ad",
                                    "children": [
                                        {"key": "ad-api-go", "path": "../ad-api-go"},
                                        {"key": "ad-admin-react", "path": "../ad-admin-react"},
                                    ],
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            profile = load_projects(config)[0]

            self.assertTrue(profile.is_valid)
            self.assertEqual(profile.project_group["key"], "ad")
            self.assertEqual(profile.project_group["children"][0]["key"], "ad-api-go")
            self.assertEqual(profile_to_dict(profile)["project_group"]["children"][1]["path"], "../ad-admin-react")

    def test_runner_core_schema_strict(self) -> None:
        valid, errors = validate_runner_json(
            "status",
            {
                "schema_version": "1",
                "project_id": "fake",
                "command": "status",
                "status": "completed",
                "observed_at": "2026-06-30T00:00:00+00:00",
                "summary": "ok",
                "project_status": "idle",
                "extra": "保留扩展字段",
            },
        )
        self.assertTrue(valid, errors)

        valid, errors = validate_runner_json("status", {"status": "whatever"})
        self.assertFalse(valid)
        self.assertTrue(any("缺少核心字段" in error for error in errors))
        self.assertTrue(any("非法 status" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
