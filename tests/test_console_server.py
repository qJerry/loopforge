from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from loopforge.server import ConsoleServer


CONFIG = Path("tests/fixtures/projects.json")
DEMO_STATE = Path("tests/fixtures/demo_project/.loopforge/demo_state.json")
DEMO_EVENTS = Path("tests/fixtures/demo_project/.loopforge/events.jsonl")
DEMO_HISTORY = Path("tests/fixtures/demo_project/.loopforge/index.jsonl")
DEMO_LOCK = Path("tests/fixtures/demo_project/.loopforge/lock.json")
DEMO_CLUES = Path("tests/fixtures/demo_project/.loopforge/clues")
DEMO_CANCEL_REQUESTS = Path("tests/fixtures/demo_project/.loopforge/cancel-requests")
DEMO_ACTIVE_DISPATCH = Path("tests/fixtures/demo_project/.loopforge/active-dispatch.json")
DEMO_DISPATCHES = Path("tests/fixtures/demo_project/.loopforge/dispatches")
DEMO_DEV_TASK = Path("tests/fixtures/demo_project/data/dev-task.json")
DEMO_DOCS = Path("tests/fixtures/demo_project/docs")
IMPORT_PROJECT = Path("tests/fixtures/import_project")


class ConsoleServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ConsoleServer(CONFIG, port=0, token="test-token")
        handler = cls.server._handler()
        from http.server import ThreadingHTTPServer

        try:
            cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        except PermissionError as exc:
            raise unittest.SkipTest(f"当前沙箱禁止绑定本地端口：{exc}")
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=2)

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def setUp(self) -> None:
        for path in [DEMO_STATE, DEMO_EVENTS, DEMO_HISTORY, DEMO_LOCK, DEMO_ACTIVE_DISPATCH]:
            path.unlink(missing_ok=True)
        for path in [DEMO_CLUES, DEMO_CANCEL_REQUESTS]:
            if path.exists():
                for child in path.glob("*.json"):
                    child.unlink(missing_ok=True)
        shutil.rmtree(DEMO_DOCS, ignore_errors=True)
        shutil.rmtree(DEMO_DISPATCHES, ignore_errors=True)
        DEMO_DEV_TASK.write_text(json.dumps(demo_dev_task_payload(), ensure_ascii=False, indent=2), encoding="utf-8")

    def tearDown(self) -> None:
        for path in [DEMO_STATE, DEMO_EVENTS, DEMO_HISTORY, DEMO_LOCK, DEMO_ACTIVE_DISPATCH]:
            path.unlink(missing_ok=True)
        for path in [DEMO_CLUES, DEMO_CANCEL_REQUESTS]:
            if path.exists():
                for child in path.glob("*.json"):
                    child.unlink(missing_ok=True)
        shutil.rmtree(DEMO_DOCS, ignore_errors=True)
        shutil.rmtree(DEMO_DISPATCHES, ignore_errors=True)

    def request_json(self, path: str, method: str = "GET", body: dict | None = None) -> dict:
        _, payload = self.request_json_with_status(path, method=method, body=body)
        return payload

    def request_json_with_status(self, path: str, method: str = "GET", body: dict | None = None) -> tuple[int, dict]:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(
            self.url(path),
            data=data,
            method=method,
            headers={"X-LoopForge-Token": "test-token", "Content-Type": "application/json"},
        )
        with urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))

    def test_api_requires_token(self) -> None:
        with self.assertRaises(HTTPError) as ctx:
            urlopen(self.url("/api/v1/projects"), timeout=3)
        self.assertEqual(ctx.exception.code, 401)

    def test_projects_api(self) -> None:
        payload = self.request_json("/api/v1/projects")
        self.assertEqual(payload["status"], "completed")
        self.assertTrue(any(project["project_id"] == "demo" for project in payload["projects"]))

    def test_worker_health_api_is_authenticated_and_delegates_to_detector(self) -> None:
        expected = {
            "status": "completed",
            "providers": {
                "codex": {"status": "ready", "ready": True},
                "claude": {"status": "ready", "ready": True},
            },
        }
        with patch("loopforge.server.detect_worker_health", return_value=expected) as detect:
            payload = self.request_json("/api/v1/worker-health")

        self.assertEqual(payload, expected)
        detect.assert_called_once_with()

    def test_worker_health_api_uses_requested_project_network_config(self) -> None:
        expected = {"status": "completed", "providers": {}}
        with patch("loopforge.server.detect_worker_health", return_value=expected) as detect:
            payload = self.request_json("/api/v1/worker-health?project_id=demo")

        self.assertEqual(payload, expected)
        profile = detect.call_args.kwargs["profile"]
        self.assertEqual(profile.project_id, "demo")


    def test_schedule_tick_api_returns_accepted_dispatch(self) -> None:
        accepted = {"status": "accepted", "dispatch_id": "dispatch-schedule", "pid": 123, "summary": "运行已受理"}
        with patch("loopforge.server.schedule_dispatch_tick", return_value=accepted) as dispatch:
            status_code, payload = self.request_json_with_status(
                "/api/v1/schedule-tick",
                method="POST",
                body={},
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload, accepted)
        dispatch.assert_called_once_with(CONFIG)

    def test_resume_once_api_passes_this_run_instruction(self) -> None:
        accepted = {"status": "accepted", "dispatch_id": "dispatch-resume", "pid": 123, "summary": "运行已受理"}
        with patch("loopforge.server.dispatch_project_action", return_value=accepted) as dispatch:
            status_code, payload = self.request_json_with_status(
                "/api/v1/projects/demo/resume-once",
                method="POST",
                body={"reason": "继续当前 Rust 实现"},
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload, accepted)
        dispatch.assert_called_once_with(CONFIG, "demo", "resume_once", reason="继续当前 Rust 实现")


    def test_task_token_usage_api_forwards_filter_and_safe_limit(self) -> None:
        expected = {"status": "completed", "summary": "ok", "items": [], "issues": []}
        with patch("loopforge.server.task_token_usage", return_value=expected) as usage_mock:
            payload = self.request_json("/api/v1/token-usage/tasks?project_id=demo&limit=500")

        self.assertEqual(payload, expected)
        usage_mock.assert_called_once_with(CONFIG, project_id="demo", limit=100)

        with patch("loopforge.server.task_token_usage", return_value=expected) as usage_mock:
            self.request_json("/api/v1/token-usage/tasks?limit=invalid")
        usage_mock.assert_called_once_with(CONFIG, project_id="", limit=20)

    def test_onboarding_preview_api_is_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "go-service"
            root.mkdir()
            (root / "go.mod").write_text("module example.com/service\n", encoding="utf-8")

            payload = self.request_json(
                "/api/v1/projects/onboarding/preview",
                method="POST",
                body={
                    "root_dir": str(root),
                    "project_type": "go-backend",
                    "project_id": "go-service",
                    "owner": "galaxy",
                },
            )

            self.assertEqual(payload["status"], "completed", payload)
            self.assertEqual(payload["owner"], "galaxy")
            self.assertEqual(payload["planning_adapter"], "builtin")
            self.assertTrue(payload["plan_hash"])
            self.assertFalse((root / "AGENTS.md").exists())

    def test_onboarding_preview_api_forwards_selected_project_group_children(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "group"
            selected = root / "selected"
            omitted = root / "omitted"
            for child in (selected, omitted):
                (child / ".git").mkdir(parents=True)

            payload = self.request_json(
                "/api/v1/projects/onboarding/preview",
                method="POST",
                body={
                    "root_dir": str(root),
                    "project_type": "project-group",
                    "project_id": "group",
                    "owner": "galaxy",
                    "project_group_children": [{"key": "selected", "path": str(selected)}],
                },
            )

            children = payload["profile"]["project_group"]["children"]
            self.assertEqual([child["key"] for child in children], ["selected"])

    def test_onboarding_preview_api_forwards_flutter_build_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "flutter-app"
            root.mkdir()
            (root / "pubspec.yaml").write_text(
                "name: demo\ndependencies:\n  flutter:\n    sdk: flutter\n",
                encoding="utf-8",
            )

            payload = self.request_json(
                "/api/v1/projects/onboarding/preview",
                method="POST",
                body={
                    "root_dir": str(root),
                    "project_type": "flutter-app",
                    "project_id": "flutter-app",
                    "owner": "galaxy",
                    "build_target": "web",
                },
            )

            self.assertEqual(payload["status"], "completed", payload)
            self.assertEqual(payload["build_target"], "web")
            build = next(entry for entry in payload["files"] if entry["path"] == "scripts/build.sh")
            self.assertIn('BUILD_TARGET="${1:-web}"', build["content"])

    def test_project_tasks_api(self) -> None:
        tasks = self.request_json("/api/v1/projects/demo/tasks")
        self.assertEqual(tasks["status"], "completed")
        self.assertEqual(len(tasks["tasks"]), 3)

        created = self.request_json("/api/v1/projects/demo/tasks", method="POST", body={"title": "后台新增任务", "description": "HTTP API 创建"})
        self.assertEqual(created["status"], "completed")
        self.assertEqual(created["task"]["title"], "后台新增任务")

        ai_created = self.request_json("/api/v1/projects/demo/tasks/ai-create", method="POST", body={"prompt": "一句话创建后台任务"})
        self.assertEqual(ai_created["status"], "completed")
        self.assertEqual(ai_created["task"]["source"], "ai")

        history = self.request_json("/api/v1/projects/demo/tasks/history?limit=20")
        self.assertEqual(history["status"], "completed")
        self.assertEqual(history["limit"], 20)
        with self.assertRaises(HTTPError) as raised:
            self.request_json("/api/v1/projects/demo/tasks/reorder", method="POST", body={"ordered_ids": []})
        self.assertEqual(raised.exception.code, 404)


    def test_project_task_review_api(self) -> None:
        payload = demo_dev_task_payload()
        payload["items"][0]["status"] = "ready_for_review"
        DEMO_DEV_TASK.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

        accepted = self.request_json(
            "/api/v1/projects/demo/tasks/demo-task-001/review",
            method="POST",
            body={"decision": "accept", "feedback": "验收通过"},
        )

        self.assertEqual(accepted["status"], "completed")
        self.assertEqual(accepted["task"]["status"], "accepted")

    def test_resource_cleanup_apis_forward_explicit_discard_confirmation(self) -> None:
        with patch("loopforge.server.project_task_reservation_cancel") as cancel_reservation:
            cancel_reservation.return_value = {"status": "completed", "summary": "预留已取消"}
            payload = self.request_json(
                "/api/v1/projects/demo/tasks/reservations/reservation-1234567890/cancel",
                method="POST",
                body={"discard_changes": True},
            )
            self.assertEqual(payload["status"], "completed")
            cancel_reservation.assert_called_once_with(
                CONFIG,
                "demo",
                "reservation-1234567890",
                discard_changes=True,
            )

        with patch("loopforge.server.project_task_cleanup") as cleanup:
            cleanup.return_value = {"status": "completed", "summary": "清理完成"}
            self.request_json(
                "/api/v1/projects/demo/tasks/demo-task-001/cleanup",
                method="POST",
                body={"discard_changes": True},
            )
            cleanup.assert_called_once_with(CONFIG, "demo", "demo-task-001", discard_changes=True)

        with patch("loopforge.server.project_task_abandon") as abandon:
            abandon.return_value = {"status": "completed", "summary": "已放弃"}
            self.request_json(
                "/api/v1/projects/demo/tasks/demo-task-001/abandon",
                method="POST",
                body={"reason": "取消实施", "discard_changes": True},
            )
            abandon.assert_called_once_with(
                CONFIG,
                "demo",
                "demo-task-001",
                "取消实施",
                discard_changes=True,
            )

    def test_project_preview_run_api(self) -> None:
        payload = self.request_json("/api/v1/projects/demo/preview-run", method="POST", body={})
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["task"]["id"], "demo-task-001")
        self.assertEqual(payload["plan"]["executor"], "fake_codex")

    def test_loop_type_run_apis(self) -> None:
        accepted = {"status": "accepted", "dispatch_id": "dispatch-loop", "pid": 123, "summary": "运行已受理"}
        with patch("loopforge.server.dispatch_project_action", return_value=accepted) as dispatch:
            status_code, payload = self.request_json_with_status(
                "/api/v1/projects/demo/run-once",
                method="POST",
                body={"loop_type": "scan"},
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload, accepted)
        dispatch.assert_called_once_with(CONFIG, "demo", "run_once", loop_type="scan")

    def test_start_run_api_calibrates_and_develops_in_one_request(self) -> None:
        accepted = {"status": "accepted", "dispatch_id": "dispatch-start", "pid": 123, "summary": "运行已受理"}
        with patch("loopforge.server.dispatch_project_action", return_value=accepted) as dispatch:
            status_code, payload = self.request_json_with_status(
                "/api/v1/projects/demo/start-run",
                method="POST",
                body={},
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload, accepted)
        dispatch.assert_called_once_with(CONFIG, "demo", "start_run")

    def test_resolve_and_resume_api_returns_accepted_dispatch(self) -> None:
        accepted = {"status": "accepted", "dispatch_id": "dispatch-resolve", "pid": 123, "summary": "运行已受理"}
        with patch("loopforge.server.dispatch_project_action", return_value=accepted) as dispatch:
            status_code, payload = self.request_json_with_status(
                "/api/v1/projects/demo/resolve-and-resume",
                method="POST",
                body={"reason": "人工问题已处理"},
            )

        self.assertEqual(status_code, 202)
        self.assertEqual(payload, accepted)
        dispatch.assert_called_once_with(CONFIG, "demo", "resolve_and_resume", reason="人工问题已处理")

    def test_project_clues_api(self) -> None:
        created = self.request_json(
            "/api/v1/projects/demo/clues",
            method="POST",
            body={
                "type": "code_without_docs",
                "severity": "medium",
                "subject": {"kind": "code", "id": "route:POST /api/demo"},
                "summary": "新增 API 没有对应模块三件套",
                "evidence": ["routes/demo.py"],
            },
        )
        self.assertEqual(created["status"], "completed")
        listed = self.request_json("/api/v1/projects/demo/clues")
        self.assertEqual(listed["status"], "completed")
        self.assertEqual(listed["counts"]["open"], 1)

        decided = self.request_json(
            "/api/v1/projects/demo/clues/decide",
            method="POST",
            body={"clue_id": created["clue"]["id"], "decision": "create_task", "reason": "确认需要补规格"},
        )
        self.assertEqual(decided["status"], "completed")
        self.assertEqual(decided["mapped_status"], "spec_blocked")

    def test_cancel_run_api(self) -> None:
        skipped = self.request_json("/api/v1/projects/demo/cancel-run", method="POST", body={})
        self.assertEqual(skipped["status"], "skipped_no_running")

        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).replace(microsecond=0).isoformat()
        DEMO_LOCK.parent.mkdir(parents=True, exist_ok=True)
        DEMO_LOCK.write_text(
            json.dumps({"run_id": "active-run", "pid": os.getpid(), "started_at": future, "expires_at": future, "loop_type": "dev"}),
            encoding="utf-8",
        )
        requested = self.request_json("/api/v1/projects/demo/cancel-run", method="POST", body={"run_id": "active-run"})
        self.assertEqual(requested["status"], "cancel_requested")
        self.assertTrue((DEMO_CANCEL_REQUESTS / "active-run.json").exists())

    def test_project_notification_config_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            global_config_path = Path(tmp) / "global-config.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            raw["projects"][0]["executor"] = "codex_cli"
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            with patch.dict(os.environ, {"LOOPFORGE_GLOBAL_CONFIG": str(global_config_path)}):
                server = ConsoleServer(config_path, port=0, token="config-token")
                handler = server._handler()
                from http.server import ThreadingHTTPServer

                try:
                    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
                except PermissionError as exc:
                    raise unittest.SkipTest(f"当前沙箱禁止绑定本地端口：{exc}")
                port = httpd.server_address[1]
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    get_req = Request(
                        f"http://127.0.0.1:{port}/api/v1/settings",
                        method="GET",
                        headers={"X-LoopForge-Token": "config-token", "Content-Type": "application/json"},
                    )
                    with urlopen(get_req, timeout=3) as response:
                        settings_payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(settings_payload["status"], "completed")
                    self.assertEqual(settings_payload["settings"]["scan"]["daily_minutes"], 15)
                    self.assertFalse(settings_payload["settings"]["exists"])

                    data = json.dumps(
                        {
                            "notification_channel": "wecom_robot",
                            "webhook_url": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test",
                        }
                    ).encode("utf-8")
                    req = Request(
                        f"http://127.0.0.1:{port}/api/v1/projects/demo/notification-config",
                        data=data,
                        method="POST",
                        headers={"X-LoopForge-Token": "config-token", "Content-Type": "application/json"},
                    )
                    with urlopen(req, timeout=3) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(payload["status"], "completed")
                    self.assertTrue(payload["settings"]["notifications"]["wecom"]["enabled"])
                    self.assertEqual(payload["settings"]["notifications"]["wecom"]["webhook_url"], "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")
                    saved = json.loads(config_path.read_text(encoding="utf-8"))
                    self.assertNotIn("webhook_url", saved["projects"][0])
                    persisted = json.loads(global_config_path.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["notifications"]["wecom"]["webhook_url"], "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test")

                    global_data = json.dumps(
                        {
                            "enabled": False,
                            "webhook_env": "LOOPFORGE_WECOM_WEBHOOK",
                            "webhook_url": "",
                        }
                    ).encode("utf-8")
                    global_req = Request(
                        f"http://127.0.0.1:{port}/api/v1/settings/notification-config",
                        data=global_data,
                        method="POST",
                        headers={"X-LoopForge-Token": "config-token", "Content-Type": "application/json"},
                    )
                    with urlopen(global_req, timeout=3) as response:
                        global_payload = json.loads(response.read().decode("utf-8"))
                    self.assertEqual(global_payload["status"], "completed")
                    self.assertFalse(global_payload["settings"]["notifications"]["wecom"]["enabled"])
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=2)

    def test_project_runtime_config_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            server = ConsoleServer(config_path, port=0, token="runtime-token")
            handler = server._handler()
            from http.server import ThreadingHTTPServer

            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            except PermissionError as exc:
                raise unittest.SkipTest(f"当前沙箱禁止绑定本地端口：{exc}")
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                data = json.dumps(
                    {
                        "schedule_enabled": False,
                        "schedule_frequency": "weekly",
                        "auto_commit": True,
                        "codex_model": "gpt-5.6-terra",
                        "codex_reasoning_effort": "high",
                        "codex_sandbox": "danger-full-access",
                    }
                ).encode("utf-8")
                req = Request(
                    f"http://127.0.0.1:{port}/api/v1/projects/demo/runtime-config",
                    data=data,
                    method="POST",
                    headers={"X-LoopForge-Token": "runtime-token", "Content-Type": "application/json"},
                )
                with urlopen(req, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["status"], "completed")
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertFalse(saved["projects"][0]["schedule_enabled"])
                self.assertEqual(saved["projects"][0]["automation_mode"], "off")
                self.assertEqual(saved["projects"][0]["schedule_frequency"], "weekly")
                self.assertTrue(saved["projects"][0]["auto_commit"])
                self.assertEqual(saved["projects"][0]["codex_model"], "gpt-5.6-terra")
                self.assertEqual(saved["projects"][0]["codex_reasoning_effort"], "high")
                self.assertEqual(saved["projects"][0]["codex_sandbox"], "danger-full-access")

                claude_data = json.dumps(
                    {
                        "automation_mode": "execute",
                        "schedule_enabled": True,
                        "schedule_frequency": "hourly",
                        "auto_commit": False,
                        "worker_provider": "claude",
                        "worker_settings": {"model": "opus", "permission_mode": "acceptEdits"},
                        "worker_network": {
                            "mode": "custom",
                            "http_proxy": "http://127.0.0.1:8899",
                            "https_proxy": "http://127.0.0.1:8899",
                            "all_proxy": "socks5://127.0.0.1:8899",
                        },
                    }
                ).encode("utf-8")
                claude_req = Request(
                    f"http://127.0.0.1:{port}/api/v1/projects/demo/runtime-config",
                    data=claude_data,
                    method="POST",
                    headers={"X-LoopForge-Token": "runtime-token", "Content-Type": "application/json"},
                )
                with urlopen(claude_req, timeout=3) as response:
                    claude_payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(claude_payload["status"], "completed")
                self.assertEqual(claude_payload["project"]["worker_runtime"]["provider"], "claude")
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["projects"][0]["executor"], "claude_cli")
                self.assertEqual(saved["projects"][0]["claude_model"], "opus")
                self.assertEqual(saved["projects"][0]["claude_permission_mode"], "acceptEdits")
                self.assertEqual(saved["projects"][0]["codex_model"], "gpt-5.6-terra")
                self.assertEqual(saved["projects"][0]["worker_network"]["https_proxy"], "http://127.0.0.1:8899")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_project_remove_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            raw = json.loads(CONFIG.read_text(encoding="utf-8"))
            raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
            config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            server = ConsoleServer(config_path, port=0, token="remove-token")
            handler = server._handler()
            from http.server import ThreadingHTTPServer

            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            except PermissionError as exc:
                raise unittest.SkipTest(f"当前沙箱禁止绑定本地端口：{exc}")
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                req = Request(
                    f"http://127.0.0.1:{port}/api/v1/projects/demo/remove",
                    data=b"{}",
                    method="POST",
                    headers={"X-LoopForge-Token": "remove-token", "Content-Type": "application/json"},
                )
                with urlopen(req, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["status"], "completed")
                self.assertFalse(payload["deleted_files"])
                saved = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(saved["projects"], [])
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_project_import_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "projects.json"
            config_path.write_text(json.dumps({"projects": []}, ensure_ascii=False), encoding="utf-8")
            server = ConsoleServer(config_path, port=0, token="import-token")
            handler = server._handler()
            from http.server import ThreadingHTTPServer

            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            except PermissionError as exc:
                raise unittest.SkipTest(f"当前沙箱禁止绑定本地端口：{exc}")
            port = httpd.server_address[1]
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                data = json.dumps({"root_dir": str(IMPORT_PROJECT), "name": "导入项目"}).encode("utf-8")
                req = Request(
                    f"http://127.0.0.1:{port}/api/v1/projects/import",
                    data=data,
                    method="POST",
                    headers={"X-LoopForge-Token": "import-token", "Content-Type": "application/json"},
                )
                with urlopen(req, timeout=3) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                self.assertEqual(payload["status"], "completed")
                self.assertEqual(payload["tasks"][0]["title"], "导入后读取任务清单")

                raw = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(raw["projects"][0]["name"], "导入项目")
                self.assertFalse(raw["projects"][0]["schedule_enabled"])
                self.assertEqual(raw["projects"][0]["schedule_frequency"], "hourly")
                self.assertEqual(raw["projects"][0]["default_agent"], "codex")
            finally:
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=2)

    def test_index_injects_token(self) -> None:
        with urlopen(self.url("/"), timeout=3) as response:
            body = response.read().decode("utf-8")
        self.assertIn("test-token", body)
        self.assertIn("LoopForge 控制台", body)


def demo_dev_task_payload() -> dict:
    return {
        "version": 1,
        "description": "Demo Project 外部 backlog。LoopForge 每轮最多处理一个 item。",
        "items": [
            _item("demo-task-001", "观察任务完成全流程"),
            _item("demo-task-002", "补充运行报告入口"),
            _item("demo-task-003", "验证通知重发链路"),
        ],
    }


def _item(item_id: str, title: str) -> dict:
    return {
        "id": item_id,
        "title": title,
        "description": f"Demo backlog item：{title}",
        "status": "open",
        "priority": "P2",
        "source": "seed",
        "agent": "codex",
        "acceptance": ["进入任务队列", "可被调度执行", "运行历史可观察"],
        "created_at": "2026-06-30T00:00:00+00:00",
        "updated_at": "2026-06-30T00:00:00+00:00",
    }


if __name__ == "__main__":
    unittest.main()
