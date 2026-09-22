from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from loopforge.config import load_projects
from loopforge.commands import (
    dashboard,
    project_cancel_run,
    project_clue_add,
    project_clue_decide,
    project_clues,
    project_import,
    project_notification_config,
    project_pause,
    project_preview_run,
    project_remove,
    project_resolve_and_resume,
    project_resume_once,
    project_start_run,
    project_run_once,
    project_runtime_config,
    project_status,
    project_task_add,
    project_task_cleanup,
    project_task_ai_create,
    project_task_merge,
    project_task_review,
    project_tasks,
    read_report_html,
    schedule_tick,
    task_token_usage,
)
from loopforge.domain import utc_now
from loopforge.run_dispatch import claim_active_dispatch, mark_dispatch_running


CONFIG = Path("tests/fixtures/projects.json")
FAKE_HISTORY = Path("tests/fixtures/fake_project/.loopforge/index.jsonl")
FAKE_EVENTS = Path("tests/fixtures/fake_project/.loopforge/events.jsonl")
FAKE_LOCK = Path("tests/fixtures/fake_project/.loopforge/lock.json")
FAKE_RUNS = Path("tests/fixtures/fake_project/.loopforge/runs")
FAKE_CLUES = Path("tests/fixtures/fake_project/.loopforge/clues")
FAKE_CANCEL_REQUESTS = Path("tests/fixtures/fake_project/.loopforge/cancel-requests")
FAKE_ACTIVE_DISPATCH = Path("tests/fixtures/fake_project/.loopforge/active-dispatch.json")
FAKE_DISPATCHES = Path("tests/fixtures/fake_project/.loopforge/dispatches")
DEMO_HISTORY = Path("tests/fixtures/demo_project/.loopforge/index.jsonl")
DEMO_EVENTS = Path("tests/fixtures/demo_project/.loopforge/events.jsonl")
DEMO_LOCK = Path("tests/fixtures/demo_project/.loopforge/lock.json")
DEMO_RUNS = Path("tests/fixtures/demo_project/.loopforge/runs")
DEMO_CLUES = Path("tests/fixtures/demo_project/.loopforge/clues")
DEMO_CANCEL_REQUESTS = Path("tests/fixtures/demo_project/.loopforge/cancel-requests")
DEMO_DEV_TASK = Path("tests/fixtures/demo_project/data/dev-task.json")
DEMO_DOCS = Path("tests/fixtures/demo_project/docs")
FAKE_DEV_TASK = Path("tests/fixtures/fake_project/data/dev-task.json")
FAKE_DOCS = Path("tests/fixtures/fake_project/docs")
IMPORT_PROJECT = Path("tests/fixtures/import_project")


class CommandsCliTests(unittest.TestCase):
    def test_task_token_usage_returns_task_projection(self) -> None:
        projected = {
            "items": [{"owner_project_id": "demo", "task_id": "task-1", "coverage": "complete"}],
            "issues": [],
        }
        with patch("loopforge.commands.build_task_usage", return_value=projected) as aggregate:
            payload = task_token_usage(CONFIG, project_id="demo", limit=7)

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["items"], projected["items"])
        self.assertEqual(payload["project_id"], "demo")
        self.assertEqual(payload["limit"], 7)
        aggregate.assert_called_once()

    def setUp(self) -> None:
        self._clean_runtime_fixtures()
        DEMO_DEV_TASK.write_text(json.dumps(demo_dev_task_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        FAKE_DEV_TASK.write_text(json.dumps(fake_dev_task_payload(), ensure_ascii=False, indent=2), encoding="utf-8")

    def tearDown(self) -> None:
        self._clean_runtime_fixtures()
        DEMO_DEV_TASK.write_text(json.dumps(demo_dev_task_payload(), ensure_ascii=False, indent=2), encoding="utf-8")
        FAKE_DEV_TASK.write_text(json.dumps(fake_dev_task_payload(), ensure_ascii=False, indent=2), encoding="utf-8")

    def _clean_runtime_fixtures(self) -> None:
        for path in [FAKE_HISTORY, FAKE_EVENTS, FAKE_LOCK, FAKE_ACTIVE_DISPATCH, DEMO_HISTORY, DEMO_EVENTS, DEMO_LOCK]:
            path.unlink(missing_ok=True)
        for path in [
            FAKE_RUNS,
            FAKE_DISPATCHES,
            DEMO_RUNS,
            FAKE_CLUES,
            DEMO_CLUES,
            FAKE_CANCEL_REQUESTS,
            DEMO_CANCEL_REQUESTS,
        ]:
            shutil.rmtree(path, ignore_errors=True)
        for path in [DEMO_DOCS, FAKE_DOCS]:
            shutil.rmtree(path, ignore_errors=True)
        for path in DEMO_DEV_TASK.parent.glob("dev-task-history-*.json"):
            path.unlink(missing_ok=True)
        for path in FAKE_DEV_TASK.parent.glob("dev-task-history-*.json"):
            path.unlink(missing_ok=True)
        for path in [DEMO_DEV_TASK.parent / "history", FAKE_DEV_TASK.parent / "history"]:
            shutil.rmtree(path, ignore_errors=True)

    def test_project_run_once_writes_history(self) -> None:
        payload = project_run_once(CONFIG, "fake")
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(FAKE_HISTORY.exists())
        line = FAKE_HISTORY.read_text(encoding="utf-8").strip()
        self.assertIn("fake-task-001", line)
        self.assertIn('"loop_type": "dev"', line)
        self.assertEqual(payload["run"]["associated_project_ids"], ["fake"])
        self.assertEqual(payload["run"]["worker_provider"], "fake")
        self.assertEqual(payload["run"]["worker_settings"], {})
        self.assertNotIn("token_usage", payload["run"])

    def test_project_status_uses_launching_dispatch_before_task_lock_exists(self) -> None:
        profile = next(profile for profile in load_projects(CONFIG) if profile.project_id == "fake")
        claim_active_dispatch(
            profile,
            {
                "version": 1,
                "dispatch_id": "dispatch-launching",
                "project_id": "fake",
                "action": "run_once",
                "loop_type": "dev",
                "reason": "",
                "trigger": "manual",
                "config_path": str(CONFIG.resolve()),
                "created_at": utc_now(),
            },
        )

        payload = project_status(CONFIG, "fake")

        self.assertEqual(payload["runner"]["project_status"], "running")
        self.assertEqual(payload["runner"]["dispatch"]["runtime_status"], "launching")

    def test_project_status_keeps_running_dispatch_visible_after_console_restart(self) -> None:
        profile = next(profile for profile in load_projects(CONFIG) if profile.project_id == "fake")
        claim_active_dispatch(
            profile,
            {
                "version": 1,
                "dispatch_id": "dispatch-running",
                "project_id": "fake",
                "action": "run_once",
                "loop_type": "dev",
                "reason": "",
                "trigger": "manual",
                "config_path": str(CONFIG.resolve()),
                "created_at": utc_now(),
            },
        )
        mark_dispatch_running(profile, "dispatch-running", os.getpid())

        payload = project_status(CONFIG, "fake")

        self.assertEqual(payload["runner"]["project_status"], "running")
        self.assertEqual(payload["runner"]["dispatch"]["dispatch"]["pid"], os.getpid())

    def test_stale_dispatch_does_not_report_project_as_running(self) -> None:
        profile = next(profile for profile in load_projects(CONFIG) if profile.project_id == "fake")
        claim_active_dispatch(
            profile,
            {
                "version": 1,
                "dispatch_id": "dispatch-stale",
                "project_id": "fake",
                "action": "run_once",
                "loop_type": "dev",
                "reason": "",
                "trigger": "manual",
                "config_path": str(CONFIG.resolve()),
                "created_at": utc_now(),
            },
        )
        mark_dispatch_running(profile, "dispatch-stale", 99_999_999)

        payload = project_status(CONFIG, "fake")

        self.assertNotEqual(payload["runner"]["project_status"], "running")
        self.assertEqual(payload["runner"]["dispatch"]["runtime_status"], "stale")

    def test_project_scan_once_generates_doc_clues_without_task_dependency(self) -> None:
        payload = project_run_once(CONFIG, "demo", loop_type="scan")

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["run"]["loop_type"], "scan")
        self.assertGreaterEqual(payload["run"]["clues_created"], 1)
        clues = project_clues(CONFIG, "demo")
        self.assertEqual(clues["status"], "completed")
        self.assertTrue(any(clue["type"] == "docs_dir_missing" for clue in clues["clues"]))

    def test_project_scan_once_flags_empty_docs_without_module_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._init_scan_project(Path(tmp) / "empty_docs_project")
            (root / "docs").mkdir()
            config_path = self._scan_project_config(root)

            payload = project_run_once(config_path, "scan-demo", loop_type="scan")

            self.assertEqual(payload["status"], "completed")
            self.assertTrue(any(clue["type"] == "docs_modules_missing" for clue in payload["clues"]))

    def test_project_scan_once_checks_doc_indexes_and_spec_references(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._init_scan_project(Path(tmp) / "scan_project")
            module_dir = root / "docs" / "rule" / "delivery-cap"
            module_dir.mkdir(parents=True)
            (root / "docs" / "README.md").write_text("# 文档索引\n", encoding="utf-8")
            (module_dir / "requirements.md").write_text(_doc("rule.delivery-cap", "Requirements", "需求说明"), encoding="utf-8")
            (module_dir / "design.md").write_text(_doc("rule.delivery-cap", "Design", "流程说明"), encoding="utf-8")
            (module_dir / "specs.md").write_text(
                _doc(
                    "rule.delivery-cap",
                    "Specs",
                    "\n".join(
                        [
                            "## 场景规则",
                            "",
                            "| When | Then |",
                            "| --- | --- |",
                            "| 当用户保存规则 | 系统持久化配置 |",
                            "",
                            "## 代码引用",
                            "",
                            "- app/missing_service.py",
                            "",
                            "## 验证引用",
                            "",
                            "- tests/test_missing_service.py",
                        ]
                    ),
                ),
                encoding="utf-8",
            )
            config_path = self._scan_project_config(root)

            payload = project_run_once(config_path, "scan-demo", loop_type="scan")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["run"]["scan_status"], "completed")
            clue_types = {clue["type"] for clue in payload["clues"]}
            self.assertIn("docs_index_missing_module", clue_types)
            self.assertIn("spec_code_reference_missing", clue_types)
            self.assertIn("spec_test_reference_missing", clue_types)
            self.assertTrue((root / ".loopforge" / "scan-state.json").exists())
            second = project_run_once(config_path, "scan-demo", loop_type="scan")
            self.assertEqual(second["run"]["scan_scope"], "daily")

    def test_project_scan_once_skips_front_matter_clues_for_v2_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._init_v2_scan_project(Path(tmp) / "v2_scan_project")
            self._write_plain_doc_trio(root, "rule/routing")
            config_path = self._scan_project_config(root)

            payload = project_run_once(config_path, "scan-demo", loop_type="scan")

            clue_types = {clue["type"] for clue in payload["clues"]}
            self.assertNotIn("doc_front_matter_missing", clue_types)
            self.assertNotIn("doc_front_matter_missing_fields", clue_types)
            self.assertIn("spec_missing_when_then", clue_types)

    def test_project_scan_once_keeps_front_matter_clues_for_legacy_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._init_scan_project(Path(tmp) / "legacy_scan_project")
            self._write_plain_doc_trio(root, "rule/routing")
            config_path = self._scan_project_config(root)

            payload = project_run_once(config_path, "scan-demo", loop_type="scan")

            clue_types = {clue["type"] for clue in payload["clues"]}
            self.assertIn("doc_front_matter_missing", clue_types)
            self.assertIn("spec_missing_when_then", clue_types)

    def test_project_scan_once_keeps_existing_front_matter_clue_open_after_v2_switch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._init_scan_project(Path(tmp) / "storage_switch_project")
            self._write_plain_doc_trio(root, "rule/routing")
            config_path = self._scan_project_config(root)
            project_run_once(config_path, "scan-demo", loop_type="scan")

            (root / "data" / "dev-task.json").unlink()
            (root / "data" / "tasks").mkdir()
            project_run_once(config_path, "scan-demo", loop_type="scan")

            clues = project_clues(config_path, "scan-demo")["clues"]
            front_matter_clues = [clue for clue in clues if clue["type"] == "doc_front_matter_missing"]
            self.assertTrue(front_matter_clues)
            self.assertTrue(all(clue["status"] == "open" for clue in front_matter_clues))

    def test_project_scan_once_uses_git_diff_for_code_without_docs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._init_scan_project(Path(tmp) / "git_scan_project")
            (root / "app").mkdir()
            (root / "tests").mkdir()
            (root / "app" / "service.py").write_text("def existing():\n    return True\n", encoding="utf-8")
            (root / "tests" / "test_service.py").write_text("def test_existing():\n    assert True\n", encoding="utf-8")
            self._write_confirmed_doc_pair(root, "rule/delivery-cap", code_ref="app/service.py", test_ref="tests/test_service.py")
            self._init_git_repo(root)
            config_path = self._scan_project_config(root)

            first = project_run_once(config_path, "scan-demo", loop_type="scan")
            first_head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
            self.assertEqual(first["run"]["scan_scope"], "initial")
            self.assertEqual(first["run"]["scan_head_ref"], first_head)

            (root / "app" / "new_feature.py").write_text("def create_feature():\n    return True\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "app/new_feature.py"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "add feature"], check=True, capture_output=True, text=True)
            second_head = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()

            second = project_run_once(config_path, "scan-demo", loop_type="scan")

            self.assertEqual(second["run"]["scan_scope"], "daily")
            self.assertEqual(second["run"]["scan_base_ref"], first_head)
            self.assertEqual(second["run"]["scan_head_ref"], second_head)
            self.assertTrue(any(clue["type"] == "code_without_docs" and clue["subject"]["id"] == "app/new_feature.py" for clue in second["clues"]))
            state = json.loads((root / ".loopforge" / "scan-state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["last_completed_scan_ref"], second_head)

    def test_project_scan_once_saves_cursor_and_resumes_when_budget_exceeded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._init_scan_project(Path(tmp) / "cursor_scan_project")
            for name in ["one", "two", "three"]:
                self._write_confirmed_doc_pair(root, f"rule/{name}", body="## 场景规则\n\n缺少表格。")
            config_path = self._scan_project_config(root)

            with patch.dict(os.environ, {"LOOPFORGE_SCAN_FILE_BUDGET": "1"}):
                first = project_run_once(config_path, "scan-demo", loop_type="scan")
            state = json.loads((root / ".loopforge" / "scan-state.json").read_text(encoding="utf-8"))
            self.assertEqual(first["run"]["outcome"], "scan_budget_exceeded")
            self.assertEqual(first["run"]["scan_status"], "incomplete")
            self.assertEqual(state["active_scan"]["status"], "incomplete")

            with patch.dict(os.environ, {"LOOPFORGE_SCAN_FILE_BUDGET": "20"}):
                second = project_run_once(config_path, "scan-demo", loop_type="scan")
            resumed_state = json.loads((root / ".loopforge" / "scan-state.json").read_text(encoding="utf-8"))
            self.assertEqual(second["run"]["scan_scope"], "resume")
            self.assertEqual(second["run"]["scan_status"], "completed")
            self.assertIsNone(resumed_state["active_scan"])
            self.assertTrue(any(clue["type"] == "spec_missing_when_then" for clue in second["clues"]))

    def test_project_scan_once_notifies_when_budget_exceeded_repeatedly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._init_scan_project(Path(tmp) / "repeated_budget_scan_project")
            for name in ["one", "two", "three"]:
                self._write_confirmed_doc_pair(root, f"rule/{name}", body="## 场景规则\n\n缺少表格。")
            config_path = self._scan_project_config(root)

            with patch.dict(os.environ, {"LOOPFORGE_SCAN_FILE_BUDGET": "1"}):
                with patch("loopforge.commands.send_run_notification", return_value={"status": "sent", "channel": "wecom_robot"}) as send_mock:
                    first = project_run_once(config_path, "scan-demo", loop_type="scan")
                    second = project_run_once(config_path, "scan-demo", loop_type="scan")

        self.assertEqual(first["run"]["outcome"], "scan_budget_exceeded")
        self.assertEqual(first["run"]["scan_budget_exceeded_count"], 1)
        self.assertEqual(first["run"]["notification"]["status"], "skipped")
        self.assertEqual(second["run"]["outcome"], "scan_budget_exceeded")
        self.assertEqual(second["run"]["scan_budget_exceeded_count"], 2)
        self.assertEqual(second["run"]["notification_event"], "scan_budget_exceeded_repeated")
        self.assertEqual(second["run"]["notification"]["status"], "sent")
        send_mock.assert_called_once()


    def test_project_clue_api_dedupes_and_can_upgrade_to_task(self) -> None:
        body = {
            "type": "code_without_docs",
            "severity": "medium",
            "subject": {"kind": "code", "id": "route:POST /api/demo"},
            "summary": "新增 API 没有对应模块三件套",
            "evidence": ["routes/demo.py"],
        }

        created = project_clue_add(CONFIG, "demo", body)
        updated = project_clue_add(CONFIG, "demo", {**body, "evidence": ["services/demo.py"]})
        clue_id = created["clue"]["id"]
        decided = project_clue_decide(CONFIG, "demo", clue_id, "create_task", "确认需要补规格")

        self.assertEqual(created["status"], "completed")
        self.assertTrue(created["created"])
        self.assertFalse(updated["created"])
        self.assertEqual(updated["clue"]["seen_count"], 2)
        self.assertEqual(decided["clue"]["status"], "task_created")
        self.assertEqual(decided["mapped_status"], "spec_blocked")
        self.assertEqual(decided["task"]["status"], "spec_blocked")

    def test_project_clue_false_positive_suppresses_same_signal_on_later_scan(self) -> None:
        body = {
            "type": "doc_front_matter_missing",
            "severity": "low",
            "subject": {"kind": "doc", "id": "docs/rule/requirements.md"},
            "summary": "文档缺少 front matter",
            "evidence": ["docs/rule/requirements.md"],
        }

        created = project_clue_add(CONFIG, "demo", body)
        project_clue_decide(CONFIG, "demo", created["clue"]["id"], "false_positive", "v2 文档不要求 front matter")
        seen_again = project_clue_add(CONFIG, "demo", body)

        self.assertFalse(seen_again["created"])
        self.assertEqual(seen_again["clue"]["id"], created["clue"]["id"])
        self.assertEqual(seen_again["clue"]["status"], "false_positive")
        self.assertEqual(seen_again["clue"]["seen_count"], 2)

    def test_project_cancel_run_records_request_for_active_lock(self) -> None:
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(microsecond=0).isoformat()
        DEMO_LOCK.parent.mkdir(parents=True, exist_ok=True)
        DEMO_LOCK.write_text(
            json.dumps({"run_id": "active-run", "pid": os.getpid(), "started_at": utc_now(), "expires_at": future, "loop_type": "dev"}),
            encoding="utf-8",
        )

        payload = project_cancel_run(CONFIG, "demo", "active-run", "测试取消")

        self.assertEqual(payload["status"], "cancel_requested")
        self.assertEqual(payload["run_id"], "active-run")
        self.assertTrue((DEMO_CANCEL_REQUESTS / "active-run.json").exists())

    def test_project_cancel_run_skips_when_no_active_lock(self) -> None:
        payload = project_cancel_run(CONFIG, "demo")

        self.assertEqual(payload["status"], "skipped_no_running")

    def test_project_run_once_writes_durable_run_artifacts(self) -> None:
        payload = project_run_once(CONFIG, "fake")
        self.assertEqual(payload["status"], "completed")

        run = payload["run"]
        artifacts = run["artifacts"]
        for key in [
            "prompt_path",
            "command_path",
            "request_path",
            "events_path",
            "last_message_path",
            "result_path",
        ]:
            self.assertTrue(Path(artifacts[key]).exists(), key)

        prompt = Path(artifacts["prompt_path"]).read_text(encoding="utf-8")
        self.assertIn("当前 item id：fake-task-001", prompt)
        command = json.loads(Path(artifacts["command_path"]).read_text(encoding="utf-8"))
        self.assertEqual(command["cwd"], "tests/fixtures/fake_project")
        request = json.loads(Path(artifacts["request_path"]).read_text(encoding="utf-8"))
        self.assertEqual(request["executor"], "fake_codex")
        self.assertEqual(request["item"]["id"], "fake-task-001")
        result_payload = json.loads(Path(artifacts["result_path"]).read_text(encoding="utf-8"))
        self.assertEqual(result_payload["status"], "completed")
        self.assertEqual(result_payload["artifacts"]["events_path"], artifacts["events_path"])
        self.assertIn("worker_result_path", result_payload["artifacts"])
        self.assertEqual(run["result_path"], artifacts["result_path"])
        self.assertEqual(run["log_path"], artifacts["events_path"])

    def test_project_run_once_claims_open_item_before_executor(self) -> None:
        captured = {}

        def fake_executor(profile, item, run_id):
            captured["status_seen_by_executor"] = item["status"]
            captured["task_id"] = item["id"]
            task_payload = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
            captured["status_on_disk_during_executor"] = task_payload["items"][0]["status"]
            captured["agent_status_on_disk_during_executor"] = task_payload["items"][0]["agent_status"]
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "stub executor",
                "last_message_path": "",
                "log_path": "",
                "final_status": "completed",
            }

        with patch("loopforge.commands.run_executor", fake_executor):
            payload = project_run_once(CONFIG, "demo")

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(captured["task_id"], "demo-task-001")
        self.assertEqual(captured["status_seen_by_executor"], "claimed")
        self.assertEqual(captured["status_on_disk_during_executor"], "claimed")
        self.assertEqual(captured["agent_status_on_disk_during_executor"], "running")
        task_payload = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertNotIn("demo-task-001", [item["id"] for item in task_payload["items"]])

    def test_project_run_once_refreshes_lock_while_executor_runs(self) -> None:
        import threading

        from loopforge import lock as lock_module

        heartbeat_seen = threading.Event()
        refresh_calls = 0
        real_refresh = lock_module.refresh_lock

        def counting_refresh(profile, run_id):
            nonlocal refresh_calls
            ok = real_refresh(profile, run_id)
            refresh_calls += 1
            if refresh_calls >= 2:
                heartbeat_seen.set()
            return ok

        def waiting_executor(profile, item, run_id):
            self.assertTrue(heartbeat_seen.wait(1), "executor 运行期间项目锁未续期")
            lock_payload = json.loads(DEMO_LOCK.read_text(encoding="utf-8"))
            self.assertEqual(lock_payload["run_id"], run_id)
            self.assertIn("heartbeat_at", lock_payload)
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "stub executor",
                "last_message_path": "",
                "log_path": "",
                "final_status": "completed",
            }

        with patch("loopforge.lock.LOCK_HEARTBEAT_INTERVAL_SECONDS", 0.01), patch(
            "loopforge.lock.refresh_lock", counting_refresh
        ), patch("loopforge.commands.run_executor", waiting_executor):
            payload = project_run_once(CONFIG, "demo")

        self.assertEqual(payload["status"], "completed")
        self.assertGreaterEqual(refresh_calls, 2)

    def test_project_run_once_clears_previous_executor_error_before_executor(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0].update(
            {
                "status": "claimed",
                "agent_status": "failed",
                "agent_exit_code": 127,
                "blocker_reason": "旧 blocker",
                "last_error": "旧 codex_cli 错误",
                "last_run_summary": "codex_cli 执行失败：找不到 codex 可执行文件",
                "last_message_path": "/tmp/old-last-message.md",
                "log_path": "/tmp/old-events.jsonl",
                "required_action": "旧处理建议",
            }
        )
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        captured = {}

        def fake_executor(profile, item, run_id):
            task_payload = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
            captured["task_on_disk"] = task_payload["items"][0]
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "stub executor",
                "last_message_path": "",
                "log_path": "",
                "final_status": "completed",
            }

        with patch("loopforge.commands.run_executor", fake_executor):
            project_run_once(CONFIG, "demo")

        task_on_disk = captured["task_on_disk"]
        self.assertEqual(task_on_disk["agent_status"], "running")
        for key in [
            "agent_exit_code",
            "blocker_reason",
            "last_error",
            "last_run_summary",
            "last_message_path",
            "log_path",
            "required_action",
        ]:
            self.assertIsNone(task_on_disk.get(key), key)

    def test_project_run_once_handles_item_archived_by_project_script(self) -> None:
        def archiving_executor(profile, item, run_id):
            payload = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
            payload["items"] = [task for task in payload["items"] if task.get("id") != item["id"]]
            DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "项目脚本已归档当前 item",
                "last_message_path": "",
                "log_path": "",
            }

        with patch("loopforge.commands.run_executor", archiving_executor):
            payload = project_run_once(CONFIG, "demo")

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["run"]["task_id"], "demo-task-001")
        self.assertEqual(payload["run"]["task_state"], "completed")
        self.assertTrue(DEMO_HISTORY.exists())
        self.assertIn("demo-task-001", DEMO_HISTORY.read_text(encoding="utf-8"))

    def test_schedule_tick_runs_one_project(self) -> None:
        payload = schedule_tick(CONFIG)
        self.assertEqual(payload["project_id"], "demo")
        self.assertEqual(payload["loop_type"], "dev")
        self.assertEqual(payload["status"], "completed")

    def test_schedule_tick_can_inject_background_dispatch(self) -> None:
        calls = []

        def dispatch(config_path, project_id, *, trigger, loop_type):
            calls.append((config_path, project_id, trigger, loop_type))
            return {"status": "accepted", "summary": "运行已受理", "dispatch_id": "dispatch-1"}

        payload = schedule_tick(CONFIG, run_project=dispatch)

        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(calls, [(CONFIG, "demo", "schedule", "dev")])

    def test_control_actions_write_events(self) -> None:
        pause = project_pause(CONFIG, "fake")
        self.assertEqual(pause["status"], "completed")
        resolve = project_resolve_and_resume(CONFIG, "fake", reason="已人工处理")
        self.assertEqual(resolve["status"], "completed")
        content = FAKE_EVENTS.read_text(encoding="utf-8")
        self.assertIn("pause", content)
        self.assertIn("resolve_blocker", content)

    def test_resolve_and_resume_clears_current_task_blocker(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "prd_blocked"
        payload["items"][0]["blocker_reason"] = "需要确认验收口径"
        payload["items"][0]["active_blocker"] = "missing_acceptance"
        payload["items"][0]["blockers"] = [
            {
                "code": "missing_acceptance",
                "message": "缺少验收口径",
                "resolution": "补齐 requirements",
            }
        ]
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        captured = {}

        def completing_executor(profile, item, run_id):
            captured["status_seen_by_executor"] = item["status"]
            captured["resolved_reason_seen_by_executor"] = item.get("resolved_reason")
            current = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))["items"][0]
            captured["status_on_disk_during_executor"] = current["status"]
            captured["blocker_reason_on_disk"] = current.get("blocker_reason")
            captured["active_blocker_on_disk"] = current.get("active_blocker")
            captured["blockers_on_disk"] = current.get("blockers")
            captured["resolved_reason_on_disk"] = current.get("resolved_reason")
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "stub executor",
                "last_message_path": "",
                "log_path": "",
            }

        with patch("loopforge.commands.run_executor", completing_executor):
            result = project_resolve_and_resume(CONFIG, "demo", reason="已补充 requirements 验收口径")

        self.assertEqual(result["task"]["state"], "prd_ready")
        self.assertEqual(captured["status_seen_by_executor"], "prd_ready")
        self.assertEqual(captured["resolved_reason_seen_by_executor"], "已补充 requirements 验收口径")
        self.assertEqual(captured["status_on_disk_during_executor"], "prd_ready")
        self.assertIsNone(captured["blocker_reason_on_disk"])
        self.assertIsNone(captured["active_blocker_on_disk"])
        self.assertIsNone(captured["blockers_on_disk"])
        self.assertEqual(captured["resolved_reason_on_disk"], "已补充 requirements 验收口径")
        final_payload = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertEqual(final_payload["items"][0]["status"], "prd_ready")
        self.assertNotIn("blocker_reason", final_payload["items"][0])
        self.assertNotIn("active_blocker", final_payload["items"][0])
        self.assertNotIn("blockers", final_payload["items"][0])

    def test_resume_once_clears_structured_blocker_after_session_starts(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0].update(
            {
                "status": "blocked",
                "active_blocker": "dependency_not_ready",
                "blockers": [
                    {
                        "code": "dependency_not_ready",
                        "message": "依赖服务尚未完成",
                        "resolution": "等待依赖项目完成后继续",
                    }
                ],
            }
        )
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        captured = {}

        def continuing_executor(profile, item, run_id):
            current = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))["items"][0]
            captured["blocker_reason_seen_by_executor"] = item.get("blocker_reason")
            captured["active_blocker_on_disk"] = current.get("active_blocker")
            captured["blockers_on_disk"] = current.get("blockers")
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "stub executor",
                "last_message_path": "",
                "log_path": "",
                "final_status": "completed",
            }

        with patch("loopforge.commands.run_executor", continuing_executor):
            result = project_resume_once(CONFIG, "demo")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run"]["trigger"], "resume_once")
        self.assertEqual(captured["blocker_reason_seen_by_executor"], "")
        self.assertIsNone(captured["active_blocker_on_disk"])
        self.assertIsNone(captured["blockers_on_disk"])

    def test_resume_once_passes_instruction_to_only_the_next_run(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "coding"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        captured = {}

        def continuing_executor(profile, item, run_id):
            current = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))["items"][0]
            captured["run_instruction"] = item.get("run_instruction")
            captured["persisted_run_instruction"] = current.get("run_instruction")
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "stub executor",
                "last_message_path": "",
                "log_path": "",
                "final_status": "completed",
            }

        with patch("loopforge.commands.run_executor", continuing_executor):
            result = project_resume_once(CONFIG, "demo", "继续完成 Rust FC，不要重新规划任务")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(captured["run_instruction"], "继续完成 Rust FC，不要重新规划任务")
        self.assertIsNone(captured["persisted_run_instruction"])
        final_payload = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertNotIn("run_instruction", final_payload["items"][0])

    def test_run_once_auto_resumes_blocked_item_when_dependencies_completed(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"] = [
            {
                **payload["items"][0],
                "id": "main-blocked",
                "title": "等待依赖的主任务",
                "status": "blocked",
                "resume_status": "open",
                "active_blocker": "waiting_dependencies",
                "blockers": [
                    {
                        "code": "dependency_not_completed",
                        "id": "waiting_dependencies",
                        "dependencies": ["dep-current", "dep-new-history", "dep-legacy-history"],
                        "message": "等待依赖完成",
                        "resolution": "依赖完成后恢复 open",
                    }
                ],
            },
            {
                **payload["items"][1],
                "id": "dep-current",
                "title": "当前文件内已完成依赖",
                "status": "completed",
            },
        ]
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        history_dir = DEMO_DEV_TASK.parent / "history"
        history_dir.mkdir()
        history_path = history_dir / "dev-task-20260724.json"
        history_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            **_item("dep-new-history", "新目录归档的已完成依赖", "Demo"),
                            "status": "completed",
                            "completed_at": "2026-07-24T00:00:00+00:00",
                            "archived_at": "2026-07-24T00:00:00+00:00",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        legacy_history_path = DEMO_DEV_TASK.parent / "dev-task-history-20260723.json"
        legacy_history_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            **_item("dep-legacy-history", "旧路径归档的已完成依赖", "Demo"),
                            "status": "completed",
                            "completed_at": "2026-07-23T00:00:00+00:00",
                            "archived_at": "2026-07-23T00:00:00+00:00",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        captured = {}

        def completing_executor(profile, item, run_id):
            current = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))["items"][0]
            captured["status_seen_by_executor"] = item["status"]
            captured["blocker_reason_seen_by_executor"] = item.get("blocker_reason")
            captured["status_on_disk_during_executor"] = current["status"]
            captured["active_blocker_on_disk"] = current.get("active_blocker")
            captured["blockers_on_disk"] = current.get("blockers")
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "stub executor",
                "last_message_path": "",
                "log_path": "",
                "final_status": "completed",
            }

        with patch("loopforge.commands.run_executor", completing_executor):
            result = project_run_once(CONFIG, "demo")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["run"]["previous_state"], "open")
        self.assertEqual(captured["status_seen_by_executor"], "claimed")
        self.assertEqual(captured["blocker_reason_seen_by_executor"], "")
        self.assertEqual(captured["status_on_disk_during_executor"], "claimed")
        self.assertIsNone(captured["active_blocker_on_disk"])
        self.assertIsNone(captured["blockers_on_disk"])

    def test_demo_project_advances_to_completed(self) -> None:
        payload = project_run_once(CONFIG, "demo")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["run"]["next_state"], "completed")
        self.assertEqual(payload["run"]["executor"], "fake_codex")

        status = project_status(CONFIG, "demo")
        active_task = status["runner"]["active_task"]
        self.assertEqual(active_task["id"], "demo-task-002")
        self.assertEqual(active_task["state"], "open")

        events = DEMO_EVENTS.read_text(encoding="utf-8")
        self.assertIn("task_transition", events)
        self.assertIn("已完成", events)

    def test_project_run_once_worker_result_moves_to_ready_for_review(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "spec_ready"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        def fake_executor(profile, item, run_id):
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "worker 已实现并验证",
                "last_message_path": "",
                "log_path": "",
                "worker_result": {
                    "status": "completed",
                    "summary": "实现完成，等待验收。",
                    "recommended_status": "ready_for_review",
                    "validation": {"status": "passed", "commands": ["python3 -m unittest"]},
                    "acceptance_results": [
                        {"index": index, "status": "passed", "evidence": ["python3 -m unittest"]}
                        for index in range(3)
                    ],
                },
            }

        with patch("loopforge.commands.run_executor", fake_executor):
            result_payload = project_run_once(CONFIG, "demo")

        self.assertEqual(result_payload["status"], "completed")
        self.assertEqual(result_payload["run"]["next_state"], "ready_for_review")
        self.assertEqual(result_payload["task"]["status"], "ready_for_review")
        self.assertEqual(result_payload["run"]["validation"]["status"], "passed")
        saved = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertEqual(saved["items"][0]["status"], "ready_for_review")
        self.assertNotIn("completed_at", saved["items"][0])

    def test_project_run_once_persists_complete_target_results_from_worker_result(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "spec_ready"
        payload["items"][0]["targets"] = [
            {"id": "admin-ui", "project": "ad-admin-react", "after": [], "scope": "完成管理端页面"}
        ]
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        def fake_executor(profile, item, run_id):
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "worker 已实现并验证 Target",
                "last_message_path": "",
                "log_path": "",
                "worker_result": {
                    "status": "completed",
                    "summary": "Target 完成，等待验收。",
                    "recommended_status": "ready_for_review",
                    "validation": {"status": "passed"},
                    "acceptance_results": [
                        {"index": index, "status": "passed", "evidence": ["Target 测试通过"]}
                        for index in range(3)
                    ],
                    "targets": [
                        {
                            "id": "admin-ui",
                            "status": "completed",
                            "summary": "管理端页面完成",
                            "branch": "loopforge/demo/demo-task-001/admin-ui",
                            "worktree_path": ".worktrees/demo-task-001/ad-admin-react",
                            "validation": {
                                "status": "passed",
                                "commands": ["npm run lint", "npm run build"],
                                "summary": "子项目门禁通过",
                            },
                        }
                    ],
                },
            }

        with patch("loopforge.commands.run_executor", fake_executor):
            result_payload = project_run_once(CONFIG, "demo")

        self.assertEqual(result_payload["task"]["status"], "ready_for_review")
        target = result_payload["task"]["targets"][0]
        self.assertEqual(target["branch"], "loopforge/demo/demo-task-001/admin-ui")
        self.assertEqual(target["worktree_path"], ".worktrees/demo-task-001/ad-admin-react")
        self.assertEqual(target["validation"]["status"], "passed")
        self.assertEqual(target["execution_summary"], "管理端页面完成")
        self.assertNotIn("status", target)

    def test_project_run_once_blocks_ready_for_review_when_target_result_is_incomplete(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "spec_ready"
        payload["items"][0]["targets"] = [
            {"id": "admin-ui", "project": "ad-admin-react", "after": [], "scope": "完成管理端页面"}
        ]
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        def fake_executor(profile, item, run_id):
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "worker 返回不完整 Target 结果",
                "last_message_path": "",
                "log_path": "",
                "worker_result": {
                    "status": "completed",
                    "summary": "错误地请求验收。",
                    "recommended_status": "ready_for_review",
                    "acceptance_results": [
                        {"index": index, "status": "passed", "evidence": ["Target 验收完成"]}
                        for index in range(3)
                    ],
                    "targets": [
                        {
                            "id": "admin-ui",
                            "status": "completed",
                            "branch": "loopforge/demo/demo-task-001/admin-ui",
                            "validation": {"status": "passed", "commands": ["npm run lint"]},
                        }
                    ],
                },
            }

        with patch("loopforge.commands.run_executor", fake_executor):
            result_payload = project_run_once(CONFIG, "demo")

        self.assertEqual(result_payload["status"], "blocked")
        self.assertEqual(result_payload["task"]["status"], "dev_blocked")
        self.assertTrue(any(blocker["type"] == "worker_result_invalid" for blocker in result_payload["task"]["blockers"]))
        self.assertIn("worktree_path", result_payload["task"]["blocker_reason"])

    def test_project_run_once_worker_result_routes_spec_issue_to_spec_blocked(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "coding"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        def fake_executor(profile, item, run_id):
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "worker 发现规格问题",
                "last_message_path": "",
                "log_path": "",
                "worker_result": {
                    "status": "needs_spec",
                    "summary": "接口返回结构未定义。",
                    "recommended_status": "spec_blocked",
                    "required_action": "请先确认接口返回结构。",
                    "blockers": [{"type": "spec_conflict", "summary": "接口返回结构未定义。"}],
                },
            }

        with patch("loopforge.commands.run_executor", fake_executor):
            result_payload = project_run_once(CONFIG, "demo")

        self.assertEqual(result_payload["status"], "blocked")
        self.assertEqual(result_payload["run"]["next_state"], "spec_blocked")
        self.assertEqual(result_payload["task"]["status"], "spec_blocked")
        self.assertTrue(any(blocker["type"] == "spec_conflict" for blocker in result_payload["task"]["blockers"]))
        self.assertEqual(result_payload["run"]["blocker_type"], "spec_blocked")

    def test_project_task_review_accepts_ready_task(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "ready_for_review"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        result_payload = project_task_review(CONFIG, "demo", "demo-task-001", "accept", "验收通过")

        self.assertEqual(result_payload["status"], "completed")
        self.assertEqual(result_payload["task"]["status"], "accepted")
        self.assertEqual(result_payload["transition_event"]["previous_state"], "ready_for_review")
        saved = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertEqual(saved["items"][0]["status"], "accepted")
        self.assertEqual(saved["items"][0]["review_decision"], "accepted")
        self.assertIn("accepted_at", saved["items"][0])

    def test_project_task_review_rejects_code_and_next_prompt_includes_feedback(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "ready_for_review"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        rejected = project_task_review(CONFIG, "demo", "demo-task-001", "reject_code", "按钮文案没有按 requirements 更新")
        preview = project_preview_run(CONFIG, "demo")

        self.assertEqual(rejected["status"], "completed")
        self.assertEqual(rejected["task"]["status"], "coding")
        self.assertEqual(rejected["task"]["review_feedback_type"], "code")
        self.assertIn("最近一次验收驳回", preview["plan"]["prompt"])
        self.assertIn("按钮文案没有按 requirements 更新", preview["plan"]["prompt"])

    def test_project_task_review_rejects_spec_to_spec_blocked(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "ready_for_review"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        result_payload = project_task_review(CONFIG, "demo", "demo-task-001", "reject_spec", "先确认导出字段是否包含 owner")

        self.assertEqual(result_payload["status"], "completed")
        self.assertEqual(result_payload["task"]["status"], "spec_blocked")
        self.assertEqual(result_payload["task"]["blocker_reason"], "先确认导出字段是否包含 owner")
        self.assertTrue(any(blocker["type"] == "review_spec_change" for blocker in result_payload["task"]["blockers"]))
        saved = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertNotIn("active_blocker", saved["items"][0])

    def test_dev_loop_skips_review_and_merge_waiting_tasks(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "ready_for_review"
        payload["items"][1]["status"] = "accepted"
        payload["items"][2]["status"] = "spec_ready"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        captured = {}

        def fake_executor(profile, item, run_id):
            captured["item_id"] = item["id"]
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "worker 已实现并验证",
                "last_message_path": "",
                "log_path": "",
                "worker_result": {
                    "status": "completed",
                    "summary": "第三个任务完成，等待验收。",
                    "recommended_status": "ready_for_review",
                    "acceptance_results": [
                        {"index": index, "status": "passed", "evidence": ["第三个任务验收完成"]}
                        for index in range(3)
                    ],
                },
            }

        with patch("loopforge.commands.run_executor", fake_executor):
            result_payload = project_run_once(CONFIG, "demo")

        self.assertEqual(result_payload["status"], "completed")
        self.assertEqual(captured["item_id"], "demo-task-003")
        saved = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertEqual(saved["items"][0]["status"], "ready_for_review")
        self.assertEqual(saved["items"][1]["status"], "accepted")
        self.assertEqual(saved["items"][2]["status"], "ready_for_review")

    def test_project_run_once_blocks_target_after_cycle_without_worker(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "spec_ready"
        payload["items"][0]["targets"] = [
            {"id": "ad-api-go", "project": "ad-api-go", "after": ["ad-admin-react"]},
            {"id": "ad-admin-react", "project": "ad-admin-react", "after": ["ad-api-go"]},
        ]
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        with patch("loopforge.commands.run_executor") as run_executor:
            result_payload = project_run_once(CONFIG, "demo")

        run_executor.assert_not_called()
        self.assertEqual(result_payload["status"], "blocked")
        self.assertEqual(result_payload["task"]["status"], "dev_blocked")
        self.assertEqual(result_payload["task"]["blockers"][0]["code"], "target_after_cycle")
        self.assertIn("形成环", result_payload["run"]["summary"])
        saved = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertNotIn("active_blocker", saved["items"][0])

    def test_project_task_merge_squashes_feature_branch_then_cleanup_archives_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_review_merge_repo(root)

            merged = project_task_merge(config_path, "merge-demo", "merge-task-001")

            self.assertEqual(merged["status"], "completed")
            self.assertEqual(merged["task"]["status"], "merged")
            self.assertEqual(merged["merge_result"]["status"], "merged")
            self.assertEqual(merged["merge_result"]["merge_method"], "squash")
            self.assertTrue((root / "feature.txt").exists())
            log = subprocess.run(
                ["git", "-C", str(root), "log", "-1", "--pretty=%s"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(log.stdout.strip(), "loopforge: merge 本地合入测试任务")

            cleaned = project_task_cleanup(config_path, "merge-demo", "merge-task-001")

            self.assertEqual(cleaned["status"], "completed")
            self.assertEqual(cleaned["task"]["status"], "completed")
            branch = subprocess.run(
                ["git", "-C", str(root), "show-ref", "--verify", "--quiet", "refs/heads/feature/merge-demo/merge-task-001-local"],
                check=False,
            )
            self.assertNotEqual(branch.returncode, 0)
            active = json.loads((root / "data" / "dev-task.json").read_text(encoding="utf-8"))
            self.assertEqual(active["items"], [])
            histories = list((root / "data" / "history").glob("dev-task-*.json"))
            self.assertTrue(histories)
            self.assertFalse(list((root / "data").glob("dev-task-history-*.json")))

    def test_project_task_merge_and_cleanup_multiple_targets_in_after_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / "ad"
            api_repo = base / "ad-api-go"
            web_repo = base / "ad-admin-react"
            api_branch = "feature/parent-task-001-api"
            web_branch = "feature/parent-task-001-web"
            api_target = self._init_target_merge_repo(api_repo, api_branch, "api.txt", "api change\n")
            web_target = self._init_target_merge_repo(web_repo, web_branch, "web.txt", "web change\n")
            config_path = self._init_parent_multi_target_project(
                parent,
                [
                    {
                        "id": "ad-api-go",
                        "project": "ad-api-go",
                        "path": "../ad-api-go",
                        "branch": api_branch,
                        "target_branch": api_target,
                    },
                    {
                        "id": "ad-admin-react",
                        "project": "ad-admin-react",
                        "path": "../ad-admin-react",
                        "branch": web_branch,
                        "target_branch": web_target,
                        "after": ["ad-api-go"],
                    },
                ],
            )

            merged = project_task_merge(config_path, "ad", "parent-task-001")

            self.assertEqual(merged["status"], "completed")
            self.assertEqual(merged["task"]["status"], "merged")
            self.assertEqual(merged["merge_result"]["ordered_target_ids"], ["ad-api-go", "ad-admin-react"])
            self.assertTrue((api_repo / "api.txt").exists())
            self.assertTrue((web_repo / "web.txt").exists())
            self.assertEqual(merged["task"]["targets"][0]["status"], "merged")
            self.assertEqual(merged["task"]["targets"][1]["merge_result"]["merge_commit"], merged["merge_result"]["targets"][1]["merge_commit"])

            cleaned = project_task_cleanup(config_path, "ad", "parent-task-001")

            self.assertEqual(cleaned["status"], "completed")
            self.assertEqual(cleaned["task"]["status"], "completed")
            self.assertEqual(cleaned["cleanup_result"]["ordered_target_ids"], ["ad-api-go", "ad-admin-react"])
            for repo, branch in [(api_repo, api_branch), (web_repo, web_branch)]:
                branch_ref = subprocess.run(
                    ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                    check=False,
                )
                self.assertNotEqual(branch_ref.returncode, 0)

    def test_project_task_merge_persists_completed_targets_before_later_failure(self) -> None:
        """捕获后续 Target 失败时丢失前序 Target 合入事实、导致重试从头执行的问题。"""

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / "ad"
            api_repo = base / "ad-api-go"
            web_repo = base / "ad-admin-react"
            api_branch = "feature/parent-task-001-api"
            web_branch = "feature/parent-task-001-web"
            api_target = self._init_target_merge_repo(api_repo, api_branch, "api.txt", "api change\n")
            web_target = self._init_target_merge_repo(web_repo, web_branch, "web.txt", "web change\n")
            config_path = self._init_parent_multi_target_project(
                parent,
                [
                    {
                        "id": "ad-api-go",
                        "project": "ad-api-go",
                        "path": "../ad-api-go",
                        "branch": api_branch,
                        "target_branch": api_target,
                    },
                    {
                        "id": "ad-admin-react",
                        "project": "ad-admin-react",
                        "path": "../ad-admin-react",
                        "branch": web_branch,
                        "target_branch": web_target,
                        "after": ["ad-api-go"],
                    },
                ],
            )
            dirty_file = web_repo / "local.txt"
            dirty_file.write_text("阻止第二个 Target 合入\n", encoding="utf-8")

            failed = project_task_merge(config_path, "ad", "parent-task-001")

            self.assertEqual(failed["status"], "failed")
            first_target = failed["task"]["targets"][0]
            self.assertEqual(first_target["status"], "merged")
            self.assertEqual(first_target["merge_result"]["status"], "merged")
            api_head_after_first_attempt = subprocess.run(
                ["git", "-C", str(api_repo), "rev-parse", "--short", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            self.assertEqual(first_target["merge_commit"], api_head_after_first_attempt)

            dirty_file.unlink()
            retried = project_task_merge(config_path, "ad", "parent-task-001")

            self.assertEqual(retried["status"], "completed", retried)
            first_result, second_result = retried["merge_result"]["targets"]
            self.assertEqual(first_result["status"], "already_merged")
            self.assertEqual(first_result["merge_commit"], api_head_after_first_attempt)
            self.assertEqual(second_result["status"], "merged")
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(api_repo), "rev-parse", "--short", "HEAD"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                api_head_after_first_attempt,
            )

    def test_project_task_merge_resumes_after_previous_target_was_squashed(self) -> None:
        """捕获多 Target 重试时把已 squash 的首个 Target 误判为空提交失败。"""

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            parent = base / "ad"
            api_repo = base / "ad-api-go"
            web_repo = base / "ad-admin-react"
            api_branch = "feature/parent-task-001-api"
            web_branch = "feature/parent-task-001-web"
            api_target = self._init_target_merge_repo(api_repo, api_branch, "api.txt", "api change\n")
            web_target = self._init_target_merge_repo(web_repo, web_branch, "web.txt", "web change\n")
            config_path = self._init_parent_multi_target_project(
                parent,
                [
                    {
                        "id": "ad-api-go",
                        "project": "ad-api-go",
                        "path": "../ad-api-go",
                        "branch": api_branch,
                        "target_branch": api_target,
                    },
                    {
                        "id": "ad-admin-react",
                        "project": "ad-admin-react",
                        "path": "../ad-admin-react",
                        "branch": web_branch,
                        "target_branch": web_target,
                        "after": ["ad-api-go"],
                    },
                ],
            )
            subprocess.run(
                ["git", "-C", str(api_repo), "merge", "--squash", api_branch],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", str(api_repo), "commit", "-m", "earlier squash attempt"],
                check=True,
                capture_output=True,
                text=True,
            )
            api_head_before_retry = subprocess.run(
                ["git", "-C", str(api_repo), "rev-parse", "--short", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()

            merged = project_task_merge(config_path, "ad", "parent-task-001")

            self.assertEqual(merged["status"], "completed", merged)
            self.assertEqual(merged["task"]["status"], "merged", merged)
            first, second = merged["merge_result"]["targets"]
            self.assertEqual(first["status"], "already_merged")
            self.assertEqual(first["merge_commit"], api_head_before_retry)
            self.assertEqual(second["status"], "merged")
            self.assertTrue((web_repo / "web.txt").exists())
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(api_repo), "log", "-1", "--pretty=%s"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "earlier squash attempt",
            )

    def test_project_task_merge_restores_clean_target_after_commit_hook_failure(self) -> None:
        """捕获 squash 提交失败后暂存内容残留、导致无法重试的问题。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_review_merge_repo(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["projects"][0]["worktree_managed"] = True
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            hook = root / ".git" / "hooks" / "pre-commit"
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            hook.chmod(0o755)

            failed = project_task_merge(config_path, "merge-demo", "merge-task-001")

            self.assertEqual(failed["status"], "failed")
            self.assertIn("commit 失败", failed["summary"])
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(root), "diff", "--cached", "--name-only"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "",
            )
            self.assertFalse((root / "feature.txt").exists())

            hook.unlink()
            retried = project_task_merge(config_path, "merge-demo", "merge-task-001")

            self.assertEqual(retried["status"], "completed", retried)
            self.assertEqual(retried["task"]["status"], "merged")
            self.assertTrue((root / "feature.txt").exists())

    def test_project_task_merge_reports_true_conflict_files_without_mutating_target(self) -> None:
        """确保真实冲突不会被当成已合入，并直接返回冲突文件。"""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_review_merge_repo(root)
            branch = "feature/merge-demo/merge-task-001-local"
            subprocess.run(["git", "-C", str(root), "switch", branch], check=True, capture_output=True, text=True)
            conflict_file = root / "shared.txt"
            conflict_file.write_text("feature\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "shared.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-m", "feature conflict"],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(["git", "-C", str(root), "switch", "main"], check=True, capture_output=True, text=True)
            conflict_file.write_text("main\n", encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "shared.txt"], check=True)
            subprocess.run(
                ["git", "-C", str(root), "commit", "-m", "main conflict"],
                check=True,
                capture_output=True,
                text=True,
            )

            merged = project_task_merge(config_path, "merge-demo", "merge-task-001")

            self.assertEqual(merged["status"], "failed")
            self.assertIn("shared.txt", merged["summary"], merged)
            self.assertEqual(conflict_file.read_text(encoding="utf-8"), "main\n")
            self.assertEqual(
                subprocess.run(
                    ["git", "-C", str(root), "ls-files", "-u"],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip(),
                "",
            )

    def test_project_task_merge_blocks_dirty_target_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_review_merge_repo(root)
            (root / "README.md").write_text("dirty\n", encoding="utf-8")

            merged = project_task_merge(config_path, "merge-demo", "merge-task-001")

            self.assertEqual(merged["status"], "failed")
            self.assertEqual(merged["task"]["status"], "accepted")
            self.assertEqual(merged["merge_result"]["status"], "failed")
            self.assertIn("工作区不干净", merged["summary"])
            saved = json.loads((root / "data" / "dev-task.json").read_text(encoding="utf-8"))
            self.assertNotIn("active_blocker", saved["items"][0])

    def test_project_task_merge_allows_managed_runtime_history_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_review_merge_repo(root)
            config = json.loads(config_path.read_text(encoding="utf-8"))
            config["projects"][0]["worktree_managed"] = True
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
            history_dir = root / "data" / "history"
            history_dir.mkdir()
            (history_dir / "dev-task-20260826.json").write_text(
                json.dumps({"version": 1, "items": []}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            merged = project_task_merge(config_path, "merge-demo", "merge-task-001")

            self.assertEqual(merged["status"], "completed", merged)
            self.assertEqual(merged["task"]["status"], "merged", merged)

    def test_demo_project_reports_running_during_intermediate_state(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "coding"
        payload["items"][0]["agent_status"] = "running"
        payload["items"][0]["agent_run_id"] = "live-run"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        DEMO_LOCK.parent.mkdir(parents=True, exist_ok=True)
        DEMO_LOCK.write_text(
            json.dumps(
                {
                    "project_id": "demo",
                    "run_id": "live-run",
                    "pid": os.getpid(),
                    "started_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=30)).isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status = project_status(CONFIG, "demo")
        self.assertEqual(status["runner"]["project_status"], "running")
        self.assertEqual(status["runner"]["active_task"]["state"], "coding")
        self.assertEqual(status["runner"]["active_task"]["runtime_status"], "running")
        self.assertEqual(status["runner"]["active_task"]["runtime_status_label"], "实际运行中")
        self.assertTrue(status["runner"]["active_task"]["runtime_health"]["lock_active"])

    def test_project_status_running_takes_precedence_over_blocked_task_state(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "prd_blocked"
        payload["items"][0]["blocker_reason"] = "等待用户确认规格"
        payload["items"][0]["agent_status"] = "running"
        payload["items"][0]["agent_run_id"] = "live-blocked-run"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        DEMO_LOCK.parent.mkdir(parents=True, exist_ok=True)
        DEMO_LOCK.write_text(
            json.dumps(
                {
                    "project_id": "demo",
                    "run_id": "live-blocked-run",
                    "pid": os.getpid(),
                    "started_at": now.isoformat(),
                    "expires_at": (now + timedelta(minutes=30)).isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status = project_status(CONFIG, "demo")

        self.assertEqual(status["runner"]["project_status"], "running")
        self.assertEqual(status["runner"]["active_task"]["state"], "prd_blocked")
        self.assertEqual(status["runner"]["active_task"]["blocker_reason"], "等待用户确认规格")
        self.assertNotIn("required_action", status["runner"])

    def test_demo_project_reports_timeout_for_expired_running_lock(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "coding"
        payload["items"][0]["agent_status"] = "running"
        payload["items"][0]["agent_run_id"] = "stale-run"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        now = datetime.now(timezone.utc).replace(microsecond=0)
        DEMO_LOCK.parent.mkdir(parents=True, exist_ok=True)
        DEMO_LOCK.write_text(
            json.dumps(
                {
                    "project_id": "demo",
                    "run_id": "stale-run",
                    "pid": 999999,
                    "started_at": (now - timedelta(hours=2)).isoformat(),
                    "expires_at": (now - timedelta(minutes=30)).isoformat(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        status = project_status(CONFIG, "demo")

        self.assertEqual(status["runner"]["project_status"], "timeout")
        self.assertEqual(status["runner"]["active_task"]["agent_status"], "timeout")
        self.assertEqual(status["runner"]["active_task"]["runtime_status"], "stale")
        self.assertIn("上次运行 stale-run 未正常收尾", status["runner"]["required_action"])

    def test_project_status_exposes_prd_blocked_reason(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "prd_blocked"
        payload["items"][0]["last_error"] = "缺少跨租户吊销 token 的验收口径"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        status = project_status(CONFIG, "demo")

        self.assertEqual(status["runner"]["project_status"], "blocked")
        self.assertEqual(status["runner"]["active_task"]["state"], "prd_blocked")
        self.assertEqual(status["runner"]["active_task"]["blocker_reason"], "缺少跨租户吊销 token 的验收口径")
        self.assertEqual(status["runner"]["required_action"], "缺少跨租户吊销 token 的验收口径")

    def test_project_status_exposes_active_structured_blocker_reason(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "blocked"
        payload["items"][0]["active_blocker"] = "redis-services-unavailable"
        payload["items"][0]["blockers"] = [
            {
                "id": "redis-services-unavailable",
                "target": "admin-backend",
                "command": "./scripts/test.sh integration",
                "message": "真实 Redis 和 MySQL 不可用",
                "resolution": "提供 TEST_DB_DSN、REDIS_ADDR 和 ADMIN_REDIS_ADDR 后重试",
            }
        ]
        payload["items"][0]["targets"] = [
            {
                "id": "admin-backend",
                "repo": "sample-go-service",
                "worktree": ".worktrees/demo/sample-go-service",
                "branch": "task/demo-admin-backend",
                "status": "blocked",
            }
        ]
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        status = project_status(CONFIG, "demo")

        expected = "真实 Redis 和 MySQL 不可用；提供 TEST_DB_DSN、REDIS_ADDR 和 ADMIN_REDIS_ADDR 后重试"
        diagnostic = status["runner"]["active_task"]["diagnostic"]
        self.assertEqual(status["runner"]["project_status"], "blocked")
        self.assertEqual(status["runner"]["active_task"]["blocker_reason"], expected)
        self.assertEqual(status["runner"]["required_action"], expected)
        self.assertEqual(diagnostic["target_id"], "admin-backend")
        self.assertEqual(diagnostic["repo"], "sample-go-service")
        self.assertEqual(diagnostic["worktree"], ".worktrees/demo/sample-go-service")
        self.assertEqual(diagnostic["branch"], "task/demo-admin-backend")
        self.assertEqual(diagnostic["failed_command"], "./scripts/test.sh integration")

    def test_project_status_matches_structured_blocker_by_code(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "blocked"
        payload["items"][0]["active_blocker"] = "dependency_not_completed"
        payload["items"][0]["blockers"] = [
            {
                "code": "dependency_not_completed",
                "message": "CAP-PLAT-005 必须等待 CAP-PLAT-004 完成 IAM 查询权限和菜单授权合同。",
                "evidence": "CAP-PLAT-004 当前状态为 open。",
            }
        ]
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        status = project_status(CONFIG, "demo")

        reason = "CAP-PLAT-005 必须等待 CAP-PLAT-004 完成 IAM 查询权限和菜单授权合同。"
        self.assertEqual(status["runner"]["active_task"]["blocker_reason"], reason)
        self.assertEqual(status["runner"]["required_action"], reason)
        self.assertEqual(status["runner"]["active_task"]["diagnostic"]["blocker_code"], "dependency_not_completed")

    def test_project_status_prefers_earlier_open_task_over_later_blocked_dependency(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"] = [
            _item("CAP-PLAT-004-ADMIN-IAM-AUTHORIZATION", "实现 IAM 查询权限和菜单授权合同", "Demo"),
            _item("CAP-PLAT-005-ADMIN-AUDIT", "实现平台审计", "Demo"),
        ]
        payload["items"][1]["status"] = "blocked"
        payload["items"][1]["active_blocker"] = "dependency_not_completed"
        payload["items"][1]["blockers"] = [
            {
                "code": "dependency_not_completed",
                "message": "CAP-PLAT-005 必须等待 CAP-PLAT-004 完成 IAM 查询权限和菜单授权合同。",
                "resolution": "先完成 CAP-PLAT-004。",
            }
        ]
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        status = project_status(CONFIG, "demo")
        preview = project_preview_run(CONFIG, "demo")
        dashboard_payload = dashboard(CONFIG)
        demo = next(project for project in dashboard_payload["projects"] if project["project_id"] == "demo")

        self.assertEqual(status["runner"]["project_status"], "idle")
        self.assertEqual(status["runner"]["active_task"]["id"], "CAP-PLAT-004-ADMIN-IAM-AUTHORIZATION")
        self.assertEqual(preview["task"]["id"], "CAP-PLAT-004-ADMIN-IAM-AUTHORIZATION")
        self.assertEqual(demo["status"]["active_task"]["id"], "CAP-PLAT-004-ADMIN-IAM-AUTHORIZATION")
        self.assertIsNone(demo["issue"])
        self.assertFalse(any(issue["project_id"] == "demo" for issue in dashboard_payload["issues"]))

    def test_project_status_does_not_use_codex_summary_as_blocker_reason(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "prd_blocked"
        payload["items"][0]["last_run_summary"] = "codex_cli 执行完成"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        status = project_status(CONFIG, "demo")

        self.assertEqual(status["runner"]["project_status"], "blocked")
        self.assertEqual(status["runner"]["active_task"]["blocker_reason"], "")
        self.assertNotIn("codex_cli 执行完成", status["runner"]["required_action"])
        self.assertIn("worker 没有写明具体原因", status["runner"]["required_action"])

    def test_project_run_once_records_prd_blocked_reason(self) -> None:
        def prd_blocked_executor(profile, item, run_id):
            payload = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
            payload["items"][0]["status"] = "prd_blocked"
            payload["items"][0]["last_error"] = "需要确认平台管理员是否允许跨租户操作"
            DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {
                "status": "completed",
                "exit_code": 0,
                "summary": "规格存在阻塞",
                "last_message_path": "",
                "log_path": "",
            }

        with patch("loopforge.commands.run_executor", prd_blocked_executor):
            payload = project_run_once(CONFIG, "demo")

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["run"]["task_state"], "prd_blocked")
        self.assertEqual(payload["run"]["blocker_type"], "prd_blocked")
        self.assertEqual(payload["run"]["blocker_reason"], "需要确认平台管理员是否允许跨租户操作")
        self.assertEqual(payload["run"]["required_action"], "需要确认平台管理员是否允许跨租户操作")

    def test_project_status_distinguishes_dev_task_read_failure(self) -> None:
        with patch("loopforge.commands.load_dev_tasks", side_effect=PermissionError("denied")):
            status = project_status(CONFIG, "demo")

        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["runner"]["project_status"], "failed")
        self.assertEqual(status["runner"]["summary"], "任务存储读取失败：系统权限不足")
        self.assertIn("完全磁盘访问权限", status["runner"]["required_action"])

    def test_project_tasks_distinguishes_dev_task_read_failure(self) -> None:
        with patch("loopforge.commands.load_dev_tasks", side_effect=PermissionError("denied")):
            payload = project_tasks(CONFIG, "demo")

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["summary"], "任务存储读取失败：系统权限不足")
        self.assertIn("完全磁盘访问权限", payload["required_action"])
        self.assertEqual(payload["tasks"], [])

    def test_dashboard_history_read_failure_does_not_mark_project_misconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            config_path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "demo",
                                "name": "Demo Project",
                                "root_dir": "tests/fixtures/demo_project",
                                "enabled": True,
                                "schedule_enabled": True,
                                "priority": 1,
                                "default_agent": "codex",
                                "executor": "fake_codex",
                                "auto_commit": False,
                                "notification_channel": "none",
                                "state_dir": "tests/fixtures/demo_project/.loopforge",
                                "report_dir": "tests/fixtures/demo_project/.loopforge/reports",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            with patch("loopforge.commands.read_jsonl", side_effect=OSError("denied")):
                payload = dashboard(config_path)

        demo = payload["projects"][0]
        self.assertEqual(demo["project_status"], "idle")
        self.assertEqual(demo["issue"]["kind"], "history_unreadable")
        self.assertEqual(payload["summary_counts"]["misconfigured"], 0)

    def test_dashboard_ignores_failed_run_for_archived_task(self) -> None:
        old_run = {
            "run_id": "old",
            "project_id": "demo",
            "trigger": "manual",
            "status": "failed",
            "started_at": "2026-07-01T00:00:00+00:00",
            "ended_at": "2026-07-01T00:00:01+00:00",
            "duration_seconds": 1,
            "summary": "旧任务失败",
            "task_id": "archived-task",
        }
        DEMO_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        DEMO_HISTORY.write_text(json.dumps(old_run, ensure_ascii=False) + "\n", encoding="utf-8")

        payload = dashboard(CONFIG)
        demo = next(project for project in payload["projects"] if project["project_id"] == "demo")

        self.assertIsNone(demo["issue"])
        self.assertFalse(any(issue["project_id"] == "demo" for issue in payload["issues"]))

    def test_dashboard_ignores_failed_run_after_task_resumes_running(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "coding"
        payload["items"][0]["agent_status"] = "running"
        payload["items"][0]["agent_run_id"] = "resumed-run"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        DEMO_HISTORY.parent.mkdir(parents=True, exist_ok=True)
        DEMO_HISTORY.write_text(
            json.dumps(
                {
                    "run_id": "old-failed",
                    "project_id": "demo",
                    "trigger": "manual",
                    "status": "failed",
                    "started_at": "2026-07-01T00:00:00+00:00",
                    "ended_at": "2026-07-01T00:00:01+00:00",
                    "duration_seconds": 1,
                    "summary": "旧阻塞失败",
                    "required_action": "旧处理建议",
                    "task_id": "demo-task-001",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        with patch("loopforge.commands.read_lock", return_value={"run_id": "resumed-run", "pid": os.getpid(), "started_at": "2026-07-01T00:00:00+00:00", "expires_at": "2999-01-01T00:00:00+00:00"}):
            dashboard_payload = dashboard(CONFIG)
        demo = next(project for project in dashboard_payload["projects"] if project["project_id"] == "demo")

        self.assertEqual(demo["project_status"], "running")
        self.assertIsNone(demo["issue"])
        self.assertFalse(any(issue["project_id"] == "demo" for issue in dashboard_payload["issues"]))

    def test_dashboard_exposes_next_scheduled_at_for_enabled_projects(self) -> None:
        payload = dashboard(CONFIG)
        demo = next(project for project in payload["projects"] if project["project_id"] == "demo")
        sample_project = next(project for project in payload["projects"] if project["project_id"] == "sample-go-service")

        self.assertTrue(demo["next_scheduled_at"])
        self.assertIsNone(sample_project["next_scheduled_at"])

    def test_demo_task_management_adds_manual_item(self) -> None:
        payload = project_task_add(CONFIG, "demo", "补充任务管理入口", "在项目详情中维护 backlog")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["task"]["source"], "manual")

        tasks = project_tasks(CONFIG, "demo")
        self.assertEqual(len(tasks["tasks"]), 4)
        self.assertEqual(tasks["tasks"][-1]["title"], "补充任务管理入口")

        # demo backlog 的 3 个 seed item 各占一个 tick，第四个 tick 推进手工新增的任务。
        for _ in range(4):
            run = schedule_tick(CONFIG)
        self.assertEqual(run["run"]["run"]["task_title"], "补充任务管理入口")

        events = DEMO_EVENTS.read_text(encoding="utf-8")
        self.assertIn("task_item_added", events)

    def test_demo_task_management_ai_creates_structured_item(self) -> None:
        payload = project_task_ai_create(CONFIG, "demo", "支持一句话生成任务 item")
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["task"]["source"], "ai")
        self.assertGreaterEqual(len(payload["task"]["acceptance_criteria"]), 3)

        tasks = project_tasks(CONFIG, "demo")
        self.assertEqual(tasks["tasks"][-1]["title"], "支持一句话生成任务 item")

        events = DEMO_EVENTS.read_text(encoding="utf-8")
        self.assertIn("task_item_ai_created", events)

    def test_demo_completed_tasks_leave_task_management(self) -> None:
        initial = project_tasks(CONFIG, "demo")
        self.assertEqual(len(initial["tasks"]), 3)

        run = project_run_once(CONFIG, "demo")
        self.assertEqual(run["run"]["task_id"], "demo-task-001")

        dev_task_payload = json.loads(DEMO_DEV_TASK.read_text(encoding="utf-8"))
        self.assertNotIn("demo-task-001", [task["id"] for task in dev_task_payload["items"]])

        history_files = list((DEMO_DEV_TASK.parent / "history").glob("dev-task-*.json"))
        self.assertEqual(len(history_files), 1)
        self.assertFalse(list(DEMO_DEV_TASK.parent.glob("dev-task-history-*.json")))
        archived = json.loads(history_files[0].read_text(encoding="utf-8"))
        self.assertEqual(archived["version"], 1)
        self.assertEqual(archived["items"][0]["id"], "demo-task-001")
        self.assertEqual(archived["items"][0]["status"], "completed")
        self.assertIn("archived_at", archived["items"][0])

        tasks = project_tasks(CONFIG, "demo")
        self.assertEqual(len(tasks["tasks"]), 2)
        self.assertNotIn("demo-task-001", [task["id"] for task in tasks["tasks"]])

        history = DEMO_HISTORY.read_text(encoding="utf-8")
        self.assertIn("demo-task-001", history)

    def test_demo_schedule_runs_next_task_until_backlog_empty(self) -> None:
        # 每个 tick 推进一个任务：demo backlog 有 3 个 item，第三个 tick 后清空。
        first = schedule_tick(CONFIG)
        second = schedule_tick(CONFIG)
        third = schedule_tick(CONFIG)

        self.assertEqual(first["project_id"], "demo")
        self.assertEqual(second["project_id"], "demo")
        self.assertEqual(third["project_id"], "demo")
        self.assertEqual(third["run"]["run"]["task_id"], "demo-task-003")

        status = project_status(CONFIG, "demo")
        self.assertEqual(status["runner"]["project_status"], "completed")
        self.assertFalse(status["runner"]["has_open_backlog"])

        next_tick = schedule_tick(CONFIG)
        self.assertEqual(next_tick["project_id"], "fake")

    def test_project_preview_run_has_no_side_effect(self) -> None:
        preview = project_preview_run(CONFIG, "demo")
        self.assertEqual(preview["status"], "completed")
        self.assertEqual(preview["task"]["id"], "demo-task-001")
        self.assertEqual(preview["task"]["planning_level"], "complex")
        self.assertEqual(preview["plan"]["executor"], "fake_codex")
        for path in preview["plan"]["artifacts"].values():
            self.assertFalse(Path(path).exists(), path)
        tasks = project_tasks(CONFIG, "demo")
        self.assertEqual(tasks["tasks"][0]["id"], "demo-task-001")
        self.assertEqual(tasks["tasks"][0]["status"], "open")

    def test_project_preview_run_preserves_lightweight_planning_level(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["planning_level"] = "lightweight"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        preview = project_preview_run(CONFIG, "demo")

        self.assertEqual(preview["task"]["planning_level"], "lightweight")
        self.assertIn("当前 item 规划级别：lightweight", preview["plan"]["prompt"])

    def test_codex_cli_command_uses_current_approval_config(self) -> None:
        with patch(
            "loopforge.commands.load_dev_tasks",
            return_value={"version": 1, "items": [{"id": "task-1", "title": "Task", "status": "open"}]},
        ):
            preview = project_preview_run(CONFIG, "sample-go-service")
        command = preview["plan"]["command"]

        self.assertEqual(preview["status"], "completed")
        self.assertNotIn("--ask-for-approval", command)
        self.assertIn("-c", command)
        self.assertIn('approval_policy="never"', command)
        self.assertIn("--model", command)
        self.assertIn("gpt-5-codex", command)
        self.assertIn('model_reasoning_effort="high"', command)
        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)

    def test_codex_cli_command_uses_project_sandbox_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            root = Path(tmp) / "repo"
            root.mkdir()
            (root / "data").mkdir()
            (root / "data" / "dev-task.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "items": [{"id": "task-1", "title": "Task", "status": "open"}],
                    }
                ),
                encoding="utf-8",
            )
            config_path.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "ui",
                                "name": "UI",
                                "root_dir": str(root),
                                "executor": "codex_cli",
                                "codex_sandbox": "danger-full-access",
                                "notification_channel": "none",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            preview = project_preview_run(config_path, "ui")

        command = preview["plan"]["command"]
        sandbox_index = command.index("--sandbox")
        self.assertEqual(command[sandbox_index + 1], "danger-full-access")

    def test_project_notification_config_persists_webhook(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            global_config_path = Path(tmp) / "global-config.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            with patch.dict(os.environ, {"LOOPFORGE_GLOBAL_CONFIG": str(global_config_path)}):
                payload = project_notification_config(config_path, "demo", "wecom_robot", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")

            self.assertEqual(payload["status"], "completed")
            self.assertTrue(payload["settings"]["notifications"]["wecom"]["enabled"])
            self.assertEqual(payload["settings"]["notifications"]["wecom"]["webhook_url"], "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertNotIn("webhook_url", saved["projects"][0])
            persisted = json.loads(global_config_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["notifications"]["wecom"]["webhook_url"], "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")

    def test_project_notification_config_allows_env_only_wecom(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"LOOPFORGE_GLOBAL_CONFIG": str(Path(tmp) / "global-config.json")}):
                payload = project_notification_config(CONFIG, "demo", "wecom_robot", "")
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(payload["settings"]["notifications"]["wecom"]["enabled"])
        self.assertEqual(payload["settings"]["notifications"]["wecom"]["webhook_env"], "LOOPFORGE_WECOM_WEBHOOK")

    def test_project_runtime_config_persists_schedule_and_auto_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            payload = project_runtime_config(config_path, "demo", False, True, "daily", "gpt-5.6-terra", "high", "danger-full-access")

            self.assertEqual(payload["status"], "completed")
            self.assertFalse(payload["project"]["schedule_enabled"])
            self.assertEqual(payload["project"]["automation_mode"], "off")
            self.assertEqual(payload["project"]["automation_mode_label"], "关闭")
            self.assertEqual(payload["project"]["automation_allowed_loops"], [])
            self.assertEqual(payload["project"]["schedule_frequency"], "daily")
            self.assertTrue(payload["project"]["auto_commit"])
            self.assertEqual(payload["project"]["codex_model"], "gpt-5.6-terra")
            self.assertEqual(payload["project"]["codex_model_options"][0]["slug"], "gpt-5-codex")
            self.assertEqual(payload["project"]["codex_reasoning_effort"], "high")
            self.assertEqual(payload["project"]["codex_sandbox"], "danger-full-access")
            self.assertIsNone(payload["project"]["next_scheduled_at"])
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(saved["projects"][0]["schedule_enabled"])
            self.assertEqual(saved["projects"][0]["automation_mode"], "off")
            self.assertEqual(saved["projects"][0]["schedule_frequency"], "daily")
            self.assertTrue(saved["projects"][0]["auto_commit"])
            self.assertEqual(saved["projects"][0]["codex_model"], "gpt-5.6-terra")
            self.assertEqual(saved["projects"][0]["codex_reasoning_effort"], "high")
            self.assertEqual(saved["projects"][0]["codex_sandbox"], "danger-full-access")

            with patch("loopforge.commands.utc_now", return_value="2026-07-01T10:00:00+00:00"):
                enabled = project_runtime_config(config_path, "demo", True, True, "daily")
            self.assertTrue(enabled["project"]["schedule_enabled"])
            self.assertEqual(enabled["project"]["automation_mode"], "execute")
            self.assertEqual(enabled["project"]["schedule_started_at"], "2026-07-01T10:00:00+00:00")
            self.assertEqual(enabled["project"]["next_scheduled_at"], "2026-07-02T10:00:00+00:00")
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["projects"][0]["schedule_started_at"], "2026-07-01T10:00:00+00:00")
            self.assertEqual(saved["projects"][0]["automation_mode"], "execute")

    def test_project_runtime_config_persists_automation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            payload = project_runtime_config(config_path, "demo", True, False, "hourly", automation_mode="observe")

            self.assertEqual(payload["status"], "completed")
            self.assertTrue(payload["project"]["schedule_enabled"])
            self.assertEqual(payload["project"]["automation_mode"], "observe")
            self.assertEqual(payload["project"]["automation_mode_label"], "观察模式")
            self.assertEqual(payload["project"]["automation_allowed_loops"], ["scan"])
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertTrue(saved["projects"][0]["schedule_enabled"])
            self.assertEqual(saved["projects"][0]["automation_mode"], "observe")

    def test_project_runtime_config_rejects_invalid_schedule_frequency(self) -> None:
        payload = project_runtime_config(CONFIG, "demo", True, False, "monthly")
        self.assertEqual(payload["status"], "failed")
        self.assertIn("非法调度频率", payload["summary"])

    def test_project_runtime_config_rejects_invalid_automation_mode(self) -> None:
        payload = project_runtime_config(CONFIG, "demo", True, False, "hourly", automation_mode="danger")
        self.assertEqual(payload["status"], "failed")
        self.assertIn("非法自动化模式", payload["summary"])

    def test_project_runtime_config_accepts_half_hourly_schedule_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            with patch("loopforge.commands.utc_now", return_value="2026-07-01T10:00:00+00:00"):
                payload = project_runtime_config(config_path, "demo", True, False, "half_hourly")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["project"]["schedule_frequency"], "half_hourly")
            self.assertEqual(payload["project"]["schedule_frequency_label"], "每半小时")
            self.assertEqual(payload["project"]["next_scheduled_at"], "2026-07-01T10:30:00+00:00")
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["projects"][0]["schedule_frequency"], "half_hourly")

    def test_project_runtime_config_rejects_invalid_codex_reasoning_effort(self) -> None:
        payload = project_runtime_config(CONFIG, "demo", True, False, "hourly", "gpt-5.5", "extreme")
        self.assertEqual(payload["status"], "failed")
        self.assertIn("非法 Codex reasoning 级别", payload["summary"])

    def test_project_runtime_config_rejects_invalid_codex_sandbox(self) -> None:
        payload = project_runtime_config(CONFIG, "demo", True, False, "hourly", "gpt-5.5", "xhigh", "browser")
        self.assertEqual(payload["status"], "failed")
        self.assertIn("非法 Codex sandbox", payload["summary"])

    def test_project_runtime_config_switches_provider_and_preserves_hidden_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            raw["projects"][0].update(
                {
                    "executor": "codex_cli",
                    "codex_model": "gpt-5.6-terra",
                    "codex_reasoning_effort": "xhigh",
                    "codex_sandbox": "workspace-write",
                }
            )
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            payload = project_runtime_config(
                config_path,
                "demo",
                True,
                False,
                "hourly",
                worker_provider="claude",
                worker_settings={"model": "opus", "permission_mode": "acceptEdits"},
                worker_network={
                    "mode": "custom",
                    "http_proxy": "http://127.0.0.1:8899",
                    "https_proxy": "http://127.0.0.1:8899",
                    "all_proxy": "socks5://127.0.0.1:8899",
                },
            )

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["project"]["executor"], "claude_cli")
            self.assertEqual(payload["project"]["default_agent"], "claude")
            self.assertEqual(payload["project"]["worker_runtime"]["settings"], {"model": "opus", "permission_mode": "acceptEdits"})
            saved = json.loads(config_path.read_text(encoding="utf-8"))["projects"][0]
            self.assertEqual(saved["claude_model"], "opus")
            self.assertEqual(saved["claude_permission_mode"], "acceptEdits")
            self.assertEqual(saved["codex_model"], "gpt-5.6-terra")
            self.assertEqual(saved["codex_reasoning_effort"], "xhigh")
            self.assertEqual(saved["codex_sandbox"], "workspace-write")
            self.assertEqual(
                saved["worker_network"],
                {
                    "mode": "custom",
                    "http_proxy": "http://127.0.0.1:8899",
                    "https_proxy": "http://127.0.0.1:8899",
                    "all_proxy": "socks5://127.0.0.1:8899",
                },
            )
            self.assertEqual(payload["project"]["worker_runtime"]["network"]["settings"], saved["worker_network"])

    def test_project_runtime_config_rejects_proxy_credentials(self) -> None:
        payload = project_runtime_config(
            CONFIG,
            "demo",
            True,
            False,
            worker_network={
                "mode": "custom",
                "https_proxy": "http://secret:password@127.0.0.1:7897",
            },
        )

        self.assertEqual(payload["status"], "failed")
        self.assertIn("代理地址不能包含用户名或密码", payload["summary"])

    def test_resume_after_provider_switch_uses_new_profile_without_rewriting_old_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            raw["projects"][0].update({"executor": "codex_cli", "codex_model": "gpt-5.6-sol"})
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            task_payload = demo_dev_task_payload()
            task_payload["items"][0].update(
                {
                    "status": "coding",
                    "agent_status": "failed",
                    "agent_run_id": "run-codex",
                    "last_error": "Codex 模型额度耗尽",
                }
            )
            DEMO_DEV_TASK.write_text(json.dumps(task_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            old_run_dir = DEMO_RUNS / "run-codex"
            old_run_dir.mkdir(parents=True)
            old_request = {"worker_provider": "codex", "worker_model": "gpt-5.6-sol"}
            old_request_path = old_run_dir / "request.json"
            old_request_path.write_text(json.dumps(old_request), encoding="utf-8")

            saved = project_runtime_config(
                config_path,
                "demo",
                True,
                False,
                worker_provider="claude",
                worker_settings={"model": "opus", "permission_mode": "bypassPermissions"},
            )
            captured = {}

            def completing_executor(profile, item, run_id):
                captured["executor"] = profile.executor
                captured["model"] = profile.claude_model
                captured["permission_mode"] = profile.claude_permission_mode
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "summary": "Claude 继续完成",
                    "last_message_path": "",
                    "log_path": "",
                    "final_status": "completed",
                }

            with patch("loopforge.commands.run_executor", completing_executor):
                resumed = project_resume_once(config_path, "demo")

            self.assertEqual(saved["project"]["worker_runtime"]["provider"], "claude")
            self.assertEqual(resumed["status"], "completed")
            self.assertEqual(captured, {"executor": "claude_cli", "model": "opus", "permission_mode": "bypassPermissions"})
            self.assertEqual(json.loads(old_request_path.read_text(encoding="utf-8")), old_request)

    def test_project_runtime_config_rejects_unknown_provider_and_invalid_claude_permission(self) -> None:
        unknown = project_runtime_config(
            CONFIG,
            "demo",
            True,
            False,
            worker_provider="other",
            worker_settings={},
        )
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            raw["projects"][0]["executor"] = "codex_cli"
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            invalid_permission = project_runtime_config(
                config_path,
                "demo",
                True,
                False,
                worker_provider="claude",
                worker_settings={"model": "sonnet", "permission_mode": "workspace-write"},
            )

        self.assertEqual(unknown["status"], "failed")
        self.assertIn("非法 Worker Provider", unknown["summary"])
        self.assertEqual(invalid_permission["status"], "failed")
        self.assertIn("非法 Claude 权限模式", invalid_permission["summary"])

    def test_project_runtime_config_keeps_fake_executor_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            payload = project_runtime_config(
                config_path,
                "demo",
                False,
                True,
                worker_provider="claude",
                worker_settings={"model": "sonnet", "permission_mode": "bypassPermissions"},
            )

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["project"]["executor"], "fake_codex")
            self.assertEqual(payload["project"]["worker_runtime"]["provider"], "fake")

    def test_project_run_once_auto_commit_commits_clean_repo_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)

            payload = project_run_once(config_path, "git-demo")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["run"]["commit"]["status"], "committed")
            log = subprocess.run(
                ["git", "-C", str(root), "log", "-1", "--pretty=%s"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertIn("loopforge:", log.stdout)
            tracked_state = subprocess.run(
                ["git", "-C", str(root), "ls-files", ".loopforge"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(tracked_state.stdout.strip(), "")

    def test_project_run_once_auto_commit_refuses_dirty_repo_before_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)
            (root / "README.md").write_text("user change\n", encoding="utf-8")

            payload = project_run_once(config_path, "git-demo")

            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["run"]["commit"]["status"], "dirty_before_run")
            self.assertIn("运行前工作区不干净", payload["summary"])
            self.assertEqual(payload["run"]["diagnostic"]["failure_kind"], "dirty_worktree")
            self.assertEqual(payload["run"]["diagnostic"]["dirty_files"], ["README.md"])
            log = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(log.stdout.strip(), "1")

    def test_project_run_once_auto_commit_continues_after_executor_failure_dev_task_dirty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)
            task_path = root / "data" / "dev-task.json"
            payload = json.loads(task_path.read_text(encoding="utf-8"))
            payload["items"][0].update(
                {
                    "status": "claimed",
                    "agent_status": "failed",
                    "agent_exit_code": 127,
                    "agent_run_id": "failed-run",
                    "last_error": "codex_cli 执行失败：找不到 codex 可执行文件",
                }
            )
            task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            captured = {}

            def completion_executor(profile, item, run_id):
                captured["called"] = True
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "summary": "完成实现",
                    "last_message_path": "",
                    "log_path": "",
                    "final_status": "completed",
                }

            with patch("loopforge.commands.run_executor", completion_executor):
                payload = project_run_once(config_path, "git-demo")

            self.assertTrue(captured.get("called"))
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["run"]["commit"]["status"], "committed")

    def test_project_resume_once_allows_executor_failure_current_task_dirty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)
            task_path = root / "data" / "dev-task.json"
            planning_workspace = ".loopforge/planning/git-task-001"
            payload = json.loads(task_path.read_text(encoding="utf-8"))
            payload["items"][0].update(
                {
                    "status": "coding",
                    "agent_status": "failed",
                    "agent_exit_code": 1,
                    "agent_run_id": "failed-run",
                    "last_error": "Selected model is at capacity. Please try a different model.",
                    "planning_workspace": planning_workspace,
                }
            )
            task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            task_dir = root / planning_workspace
            task_dir.mkdir(parents=True)
            (task_dir / "implement.md").write_text("上一轮 executor 已产生的当前任务产物\n", encoding="utf-8")

            def completion_executor(profile, item, run_id):
                (profile.root_dir / "main.go").write_text("package main\n", encoding="utf-8")
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "summary": "完成实现",
                    "last_message_path": "",
                    "log_path": "",
                    "final_status": "completed",
                }

            with patch("loopforge.commands.run_executor", completion_executor):
                payload = project_resume_once(config_path, "git-demo")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["run"]["trigger"], "resume_once")
            self.assertEqual(payload["run"]["commit"]["status"], "committed")
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_project_run_once_auto_commit_recovers_stale_dev_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)
            task_path = root / "data" / "dev-task.json"
            payload = json.loads(task_path.read_text(encoding="utf-8"))
            payload["items"][0]["status"] = "claimed"
            payload["items"][0]["agent_status"] = "running"
            payload["items"][0]["agent_run_id"] = "stale-run"
            task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            (root / ".loopforge" / "lock.json").write_text(
                json.dumps(
                    {
                        "project_id": "git-demo",
                        "run_id": "stale-run",
                        "pid": 999999,
                        "started_at": (now - timedelta(hours=2)).isoformat(),
                        "expires_at": (now - timedelta(minutes=30)).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            payload = project_run_once(config_path, "git-demo")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["run"]["commit"]["status"], "committed")
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_project_run_once_auto_commit_recovers_stale_task_with_partial_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)
            task_path = root / "data" / "dev-task.json"
            payload = json.loads(task_path.read_text(encoding="utf-8"))
            payload["items"][0]["status"] = "spec_ready"
            payload["items"][0]["agent_status"] = "running"
            payload["items"][0]["agent_run_id"] = "stale-run"
            task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            (root / "PARTIAL.md").write_text("上一轮 agent 已产生的中间产物\n", encoding="utf-8")
            now = datetime.now(timezone.utc).replace(microsecond=0)
            (root / ".loopforge" / "lock.json").write_text(
                json.dumps(
                    {
                        "project_id": "git-demo",
                        "run_id": "stale-run",
                        "pid": 999999,
                        "started_at": (now - timedelta(hours=2)).isoformat(),
                        "expires_at": (now - timedelta(minutes=30)).isoformat(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            payload = project_run_once(config_path, "git-demo")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["run"]["commit"]["status"], "committed")
            self.assertEqual(payload["run"]["previous_state"], "spec_ready")
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_project_run_once_allows_resolved_task_owned_dirty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)
            task_path = root / "data" / "dev-task.json"
            payload = json.loads(task_path.read_text(encoding="utf-8"))
            payload["items"][0]["status"] = "prd_ready"
            payload["items"][0]["planning_workspace"] = ".loopforge/planning/git-task-001"
            payload["items"][0]["resolved_reason"] = "允许先按 mock/fallback 继续实现"
            payload["items"][0]["previous_blocked_status"] = "prd_blocked"
            task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            task_dir = root / ".loopforge" / "planning" / "git-task-001"
            task_dir.mkdir(parents=True)
            (task_dir / "prd.md").write_text("已按人工决策更新任务口径\n", encoding="utf-8")

            payload = project_run_once(config_path, "git-demo")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["run"]["previous_state"], "prd_ready")
            self.assertEqual(payload["run"]["commit"]["status"], "committed")
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_resolve_and_resume_rolls_back_when_unrelated_dirty_files_block_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)
            task_path = root / "data" / "dev-task.json"
            payload = json.loads(task_path.read_text(encoding="utf-8"))
            payload["items"][0]["status"] = "prd_blocked"
            payload["items"][0]["blocker_reason"] = "等待人工确认"
            payload["items"][0]["planning_workspace"] = ".loopforge/planning/git-task-001"
            task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            subprocess.run(["git", "-C", str(root), "add", "data/dev-task.json"], check=True)
            subprocess.run(["git", "-C", str(root), "commit", "-m", "blocked"], check=True, capture_output=True, text=True)
            (root / "README.md").write_text("unrelated dirty change\n", encoding="utf-8")

            payload = project_resolve_and_resume(config_path, "git-demo", reason="已确认可以继续")

            self.assertEqual(payload["status"], "failed")
            self.assertIn("已取消恢复运行", payload["summary"])
            current = json.loads(task_path.read_text(encoding="utf-8"))["items"][0]
            self.assertEqual(current["status"], "prd_blocked")
            self.assertEqual(current["blocker_reason"], "等待人工确认")
            self.assertNotIn("resolved_reason", current)

    def test_project_run_once_does_not_commit_non_terminal_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)

            def planning_only_executor(profile, item, run_id):
                task_path = profile.root_dir / "data" / "dev-task.json"
                payload = json.loads(task_path.read_text(encoding="utf-8"))
                payload["items"][0]["status"] = "spec_ready"
                payload["items"][0]["planning_workspace"] = ".loopforge/planning/git-task-001"
                task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                (profile.root_dir / "PLAN.md").write_text("规划阶段产物\n", encoding="utf-8")
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "summary": "只完成规划",
                    "last_message_path": "",
                    "log_path": "",
                }

            with patch("loopforge.commands.run_executor", planning_only_executor):
                payload = project_run_once(config_path, "git-demo")

            self.assertEqual(payload["status"], "failed")
            self.assertEqual(payload["run"]["blocker_type"], "non_terminal_task")
            self.assertEqual(payload["run"]["commit"]["status"], "skipped_non_terminal")
            self.assertEqual(payload["run"]["task_state"], "spec_ready")
            log = subprocess.run(
                ["git", "-C", str(root), "rev-list", "--count", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(log.stdout.strip(), "1")

    def test_project_run_once_continues_dirty_non_terminal_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            config_path = self._init_auto_commit_repo(root)

            def planning_only_executor(profile, item, run_id):
                task_path = profile.root_dir / "data" / "dev-task.json"
                payload = json.loads(task_path.read_text(encoding="utf-8"))
                payload["items"][0]["status"] = "spec_ready"
                task_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                (profile.root_dir / "PLAN.md").write_text("规划阶段产物\n", encoding="utf-8")
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "summary": "只完成规划",
                    "last_message_path": "",
                    "log_path": "",
                }

            def completion_executor(profile, item, run_id):
                (profile.root_dir / "main.go").write_text("package main\n", encoding="utf-8")
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "summary": "完成实现",
                    "last_message_path": "",
                    "log_path": "",
                    "final_status": "completed",
                }

            with patch("loopforge.commands.run_executor", planning_only_executor):
                first = project_run_once(config_path, "git-demo")
            with patch("loopforge.commands.run_executor", completion_executor):
                second = project_run_once(config_path, "git-demo")

            self.assertEqual(first["status"], "failed")
            self.assertEqual(second["status"], "completed")
            self.assertEqual(second["run"]["commit"]["status"], "committed")
            status = subprocess.run(
                ["git", "-C", str(root), "status", "--porcelain"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(status.stdout.strip(), "")

    def test_project_remove_only_updates_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            payload = project_remove(config_path, "demo")

            self.assertEqual(payload["status"], "completed")
            self.assertFalse(payload["deleted_files"])
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(saved["projects"], [])

    def test_auto_schedule_respects_project_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") in {"demo", "fake"}]
            for item in raw["projects"]:
                item["schedule_frequency"] = "daily" if item["id"] == "demo" else "hourly"
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            DEMO_HISTORY.parent.mkdir(parents=True, exist_ok=True)
            DEMO_HISTORY.write_text(
                json.dumps(
                    {
                        "run_id": "recent",
                        "project_id": "demo",
                        "trigger": "schedule",
                        "status": "completed",
                        "started_at": utc_now(),
                        "ended_at": utc_now(),
                        "summary": "recent demo run",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            payload = schedule_tick(config_path, respect_frequency=True)

            self.assertEqual(payload["project_id"], "fake")

    def test_auto_schedule_waits_until_schedule_started_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            raw["projects"][0]["schedule_frequency"] = "hourly"
            raw["projects"][0]["schedule_started_at"] = "2999-01-01T00:00:00+00:00"
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            not_due = schedule_tick(config_path, respect_frequency=True)
            self.assertEqual(not_due["status"], "skipped_no_task")

            raw["projects"][0]["schedule_started_at"] = "2000-01-01T00:00:00+00:00"
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            due = schedule_tick(config_path, respect_frequency=True)
            self.assertEqual(due["project_id"], "demo")
            self.assertEqual(due["status"], "completed")

    def _init_auto_commit_repo(self, root: Path) -> Path:
        (root / "data").mkdir(parents=True)
        (root / ".loopforge").mkdir()
        (root / ".gitignore").write_text(".loopforge/\n", encoding="utf-8")
        (root / "data" / "dev-task.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [_item("git-task-001", "自动提交测试任务", "Git")],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "loopforge@example.test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "LoopForge Test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", ".gitignore", "data/dev-task.json"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True, text=True)
        config_path = root.parent / "projects.json"
        config_path.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "git-demo",
                            "name": "Git Demo",
                            "root_dir": str(root),
                            "enabled": True,
                            "schedule_enabled": True,
                            "schedule_frequency": "hourly",
                            "priority": 1,
                            "default_agent": "codex",
                            "executor": "fake_codex",
                            "auto_commit": True,
                            "notification_channel": "none",
                            "state_dir": str(root / ".loopforge"),
                            "report_dir": str(root / ".loopforge" / "reports"),
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return config_path

    def _init_review_merge_repo(self, root: Path) -> Path:
        branch = "feature/merge-demo/merge-task-001-local"
        (root / "data").mkdir(parents=True)
        (root / ".loopforge").mkdir()
        (root / ".gitignore").write_text(".loopforge/\n", encoding="utf-8")
        (root / "data" / "dev-task.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [_item("merge-task-001", "本地合入测试任务", "Merge")],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "loopforge@example.test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "LoopForge Test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", ".gitignore", "data/dev-task.json"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True, text=True)
        target = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        subprocess.run(["git", "-C", str(root), "switch", "-c", branch], check=True, capture_output=True, text=True)
        (root / "feature.txt").write_text("feature branch change\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "feature.txt"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "feature work"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "switch", target], check=True, capture_output=True, text=True)

        payload = json.loads((root / "data" / "dev-task.json").read_text(encoding="utf-8"))
        payload["items"][0].update(
            {
                "status": "accepted",
                "branch": branch,
                "target_branch": target,
                "merge_method": "squash",
                "accepted_at": "2026-08-20T00:00:00+00:00",
            }
        )
        (root / "data" / "dev-task.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", "data/dev-task.json"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "accept task"], check=True, capture_output=True, text=True)

        config_path = root.parent / "projects.json"
        config_path.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "merge-demo",
                            "name": "Merge Demo",
                            "root_dir": str(root),
                            "enabled": True,
                            "schedule_enabled": False,
                            "schedule_frequency": "hourly",
                            "priority": 1,
                            "default_agent": "codex",
                            "executor": "fake_codex",
                            "auto_commit": False,
                            "notification_channel": "none",
                            "state_dir": str(root / ".loopforge"),
                            "report_dir": str(root / ".loopforge" / "reports"),
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return config_path

    def _init_target_merge_repo(self, root: Path, branch: str, filename: str, body: str) -> str:
        root.mkdir(parents=True)
        (root / ".gitignore").write_text(".loopforge/\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "loopforge@example.test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "LoopForge Test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", ".gitignore"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True, text=True)
        target = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(["git", "-C", str(root), "switch", "-c", branch], check=True, capture_output=True, text=True)
        (root / filename).write_text(body, encoding="utf-8")
        subprocess.run(["git", "-C", str(root), "add", filename], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", f"feature {filename}"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "switch", target], check=True, capture_output=True, text=True)
        return target

    def _init_parent_multi_target_project(self, root: Path, targets: list[dict]) -> Path:
        (root / "data").mkdir(parents=True)
        (root / ".loopforge").mkdir()
        task = _item("parent-task-001", "父任务多 target", "AD")
        task.update(
            {
                "status": "accepted",
                "targets": targets,
                "merge_method": "squash",
                "accepted_at": "2026-08-20T00:00:00+00:00",
            }
        )
        (root / "data" / "dev-task.json").write_text(
            json.dumps({"version": 1, "items": [task]}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        config_path = root.parent / "projects.json"
        config_path.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "ad",
                            "name": "AD",
                            "root_dir": str(root),
                            "enabled": True,
                            "schedule_enabled": False,
                            "schedule_frequency": "hourly",
                            "priority": 1,
                            "default_agent": "codex",
                            "executor": "fake_codex",
                            "auto_commit": False,
                            "notification_channel": "none",
                            "state_dir": str(root / ".loopforge"),
                            "report_dir": str(root / ".loopforge" / "reports"),
                            "project_group": {
                                "key": "ad",
                                "children": [
                                    {"key": "ad-api-go", "path": "../ad-api-go"},
                                    {"key": "ad-admin-react", "path": "../ad-admin-react"},
                                ],
                            },
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return config_path

    def _init_scan_project(self, root: Path) -> Path:
        (root / "data").mkdir(parents=True)
        (root / ".loopforge").mkdir()
        (root / "data" / "dev-task.json").write_text(json.dumps({"version": 1, "items": []}, ensure_ascii=False, indent=2), encoding="utf-8")
        return root

    def _init_v2_scan_project(self, root: Path) -> Path:
        (root / "data" / "tasks").mkdir(parents=True)
        (root / ".loopforge").mkdir()
        return root

    def _scan_project_config(self, root: Path) -> Path:
        config_path = root.parent / "projects.json"
        config_path.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "scan-demo",
                            "name": "Scan Demo",
                            "root_dir": str(root),
                            "enabled": True,
                            "schedule_enabled": True,
                            "schedule_frequency": "hourly",
                            "priority": 1,
                            "default_agent": "codex",
                            "executor": "fake_codex",
                            "auto_commit": False,
                            "notification_channel": "none",
                            "state_dir": str(root / ".loopforge"),
                            "report_dir": str(root / ".loopforge" / "reports"),
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return config_path

    def _write_confirmed_doc_pair(self, root: Path, key: str, code_ref: str = "app/service.py", test_ref: str = "tests/test_service.py", body: str = "") -> None:
        module_dir = root / "docs" / key
        requirements_path = module_dir / "requirements.md"
        design_path = module_dir / "design.md"
        specs_path = module_dir / "specs.md"
        module_dir.mkdir(parents=True, exist_ok=True)
        readme = root / "docs" / "README.md"
        readme.write_text(f"# 文档索引\n\n- [{key}]({key}/requirements.md)\n", encoding="utf-8")
        requirements_path.write_text(_doc(key.replace("/", "."), "Requirements", "需求说明"), encoding="utf-8")
        design_path.write_text(_doc(key.replace("/", "."), "Design", "流程说明"), encoding="utf-8")
        spec_body = body or "\n".join(
            [
                "## 场景规则",
                "",
                "| When | Then |",
                "| --- | --- |",
                "| 当用户保存规则 | 系统持久化配置 |",
                "",
                "## 代码引用",
                "",
                f"- {code_ref}",
                "",
                "## 验证引用",
                "",
                f"- {test_ref}",
            ]
        )
        specs_path.write_text(_doc(key.replace("/", "."), "Specs", spec_body), encoding="utf-8")

    def _write_plain_doc_trio(self, root: Path, key: str) -> None:
        module_dir = root / "docs" / key
        module_dir.mkdir(parents=True)
        (root / "docs" / "README.md").write_text(f"# 文档索引\n\n- [{key}]({key}/requirements.md)\n", encoding="utf-8")
        (module_dir / "requirements.md").write_text("# Requirements\n\n需求说明。\n", encoding="utf-8")
        (module_dir / "design.md").write_text("# Design\n\n流程说明。\n", encoding="utf-8")
        (module_dir / "specs.md").write_text("# Specs\n\n尚未补充场景规则。\n", encoding="utf-8")

    def _init_git_repo(self, root: Path) -> None:
        subprocess.run(["git", "-C", str(root), "init"], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "loopforge@example.test"], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.name", "LoopForge Test"], check=True)
        subprocess.run(["git", "-C", str(root), "add", "."], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-m", "initial"], check=True, capture_output=True, text=True)

    def test_latest_report_reads_executor_last_message(self) -> None:
        project_run_once(CONFIG, "demo")
        html = read_report_html(CONFIG, "demo", "latest")
        self.assertIn("last-message.md", html)
        self.assertIn("fake codex 完成任务：观察任务完成全流程", html)

    def test_project_import_persists_project_and_reads_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            config_path.write_text(json.dumps({"projects": []}, ensure_ascii=False), encoding="utf-8")

            payload = project_import(config_path, str(IMPORT_PROJECT), name="导入项目")

            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["project"]["name"], "导入项目")
            self.assertEqual(payload["tasks"][0]["title"], "导入后读取任务清单")

            raw = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertFalse(raw["projects"][0]["schedule_enabled"])
            self.assertEqual(raw["projects"][0]["schedule_frequency"], "hourly")
            self.assertEqual(raw["projects"][0]["default_agent"], "codex")
            self.assertEqual(raw["projects"][0]["codex_model"], "gpt-5-codex")
            self.assertEqual(raw["projects"][0]["codex_reasoning_effort"], "high")
            self.assertTrue(raw["projects"][0]["auto_commit"])
            tasks = project_tasks(config_path, payload["project"]["project_id"])
            self.assertEqual(tasks["tasks"][0]["id"], "import-task-001")

    def test_project_import_initializes_missing_dev_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            root = Path(tmp) / "empty_project"
            root.mkdir()
            config_path.write_text(json.dumps({"projects": []}, ensure_ascii=False), encoding="utf-8")

            payload = project_import(config_path, str(root))

            self.assertEqual(payload["status"], "completed")
            self.assertTrue((root / "data" / "tasks" / ".gitkeep").exists())
            self.assertFalse((root / "data" / "dev-task.json").exists())
            self.assertTrue(payload["dev_task_initialized"])

    def test_project_import_rejects_invalid_dev_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            root = Path(tmp) / "broken_project"
            (root / "data").mkdir(parents=True)
            (root / "data" / "dev-task.json").write_text(json.dumps({"version": 1, "items": [{"id": "bad", "title": "Bad", "status": "task_created"}]}), encoding="utf-8")
            config_path.write_text(json.dumps({"projects": []}, ensure_ascii=False), encoding="utf-8")

            payload = project_import(config_path, str(root))

            self.assertEqual(payload["status"], "misconfigured")
            self.assertIn("task_created", payload["summary"])

    def test_cli_project_list_json(self) -> None:
        proc = subprocess.run(
            ["python3", "-m", "loopforge.cli", "--config", str(CONFIG), "--json", "project", "list"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["projects"][0]["project_id"], "demo")

def demo_dev_task_payload() -> dict:
    return {
        "version": 1,
        "description": "Demo Project 外部 backlog。LoopForge 每轮最多处理一个 item。",
        "items": [
            _item("demo-task-001", "观察任务完成全流程", "Demo"),
            _item("demo-task-002", "补充运行报告入口", "Demo"),
            _item("demo-task-003", "验证通知重发链路", "Demo"),
        ],
    }


def fake_dev_task_payload() -> dict:
    return {
        "version": 1,
        "description": "Fake Project 外部 backlog。LoopForge 每轮最多处理一个 item。",
        "items": [_item("fake-task-001", "Fake 项目任务", "Fake")],
    }


def _item(item_id: str, title: str, project_name: str) -> dict:
    return {
        "id": item_id,
        "title": title,
        "description": f"{project_name} backlog item：{title}",
        "status": "open",
        "priority": "P2",
        "source": "seed",
        "agent": "codex",
        "acceptance": ["进入任务队列", "可被调度执行", "运行历史可观察"],
        "created_at": "2026-06-30T00:00:00+00:00",
        "updated_at": "2026-06-30T00:00:00+00:00",
    }


def _doc(doc_id: str, title: str, body: str) -> str:
    return "\n".join(
        [
            "---",
            f"id: {doc_id}",
            "status: confirmed",
            "owner: galaxy",
            "version: 1",
            "updated_at: 2026-08-20",
            "---",
            "",
            f"# {title}",
            "",
            body,
            "",
        ]
    )


if __name__ == "__main__":
    unittest.main()
