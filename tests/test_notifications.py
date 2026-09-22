from __future__ import annotations

import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from loopforge.commands import notify_placeholder, project_run_once
from loopforge.config import ProjectProfile
from loopforge.notifications import send_run_notification, send_test_notification


CONFIG = Path("tests/fixtures/projects.json")
DEMO_HISTORY = Path("tests/fixtures/demo_project/.loopforge/index.jsonl")
DEMO_EVENTS = Path("tests/fixtures/demo_project/.loopforge/events.jsonl")
DEMO_LOCK = Path("tests/fixtures/demo_project/.loopforge/lock.json")
DEMO_DEV_TASK = Path("tests/fixtures/demo_project/data/dev-task.json")


@contextmanager
def capture_webhook(response_body: dict, status_code: int = 200) -> Iterator[tuple[str, list[dict]]]:
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode("utf-8")
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            payload = json.dumps(response_body, ensure_ascii=False).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError as exc:
        raise unittest.SkipTest(f"当前沙箱禁止绑定本地端口：{exc}")
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/webhook", requests
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_profile(webhook_url: str, channel: str = "wecom_robot") -> ProjectProfile:
    return ProjectProfile(
        project_id="demo",
        name="Demo Project",
        root_dir=Path("tests/fixtures/demo_project"),
        notification_channel=channel,
        webhook_url=webhook_url if channel == "wecom_robot" else None,
    )


class NotificationTests(unittest.TestCase):
    def setUp(self) -> None:
        for path in [DEMO_HISTORY, DEMO_EVENTS, DEMO_LOCK]:
            path.unlink(missing_ok=True)
        DEMO_DEV_TASK.write_text(json.dumps(demo_dev_task_payload(), ensure_ascii=False, indent=2), encoding="utf-8")

    def test_send_test_notification_posts_wecom_markdown(self) -> None:
        with capture_webhook({"errcode": 0, "errmsg": "ok"}) as (url, requests):
            payload = send_test_notification(test_profile(url))

        self.assertEqual(payload["status"], "sent")
        self.assertEqual(len(requests), 1)
        body = json.loads(requests[0]["body"])
        self.assertEqual(body["msgtype"], "markdown")
        self.assertIn("LoopForge 通知测试", body["markdown"]["content"])
        self.assertIn("Demo Project", body["markdown"]["content"])

    def test_send_test_notification_reports_wecom_error(self) -> None:
        with capture_webhook({"errcode": 40001, "errmsg": "invalid key"}) as (url, requests):
            payload = send_test_notification(test_profile(url))

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(len(requests), 1)
        self.assertIn("invalid key", payload["error"])

    def test_send_run_notification_skips_none_channel(self) -> None:
        payload = send_run_notification(
            test_profile("", channel="none"),
            {"run_id": "r1", "status": "completed", "summary": "完成"},
        )

        self.assertEqual(payload["status"], "skipped")
        self.assertEqual(payload["channel"], "none")

    def test_send_run_notification_skips_non_notifiable_status(self) -> None:
        payload = send_run_notification(
            test_profile("", channel="none"),
            {"run_id": "r1", "status": "skipped_no_task", "summary": "无任务"},
        )

        self.assertEqual(payload["status"], "skipped")
        self.assertIn("不触发通知", payload["reason"])

    def test_global_wecom_sends_even_when_project_channel_none(self) -> None:
        with capture_webhook({"errcode": 0, "errmsg": "ok"}) as (url, requests):
            profile = test_profile("", channel="none")
            profile.global_config = {
                "notifications": {
                    "wecom": {
                        "enabled": True,
                        "webhook_url": url,
                        "webhook_env": "LOOPFORGE_WECOM_WEBHOOK",
                    }
                }
            }
            payload = send_run_notification(
                profile,
                {"run_id": "r1", "status": "completed", "loop_type": "dev", "summary": "完成"},
            )

        self.assertEqual(payload["status"], "sent")
        self.assertEqual(len(requests), 1)
        self.assertEqual(payload["source"], "global:webhook_url")

    def test_scan_no_op_skips_even_when_global_wecom_enabled(self) -> None:
        with capture_webhook({"errcode": 0, "errmsg": "ok"}) as (url, requests):
            profile = test_profile("", channel="none")
            profile.global_config = {
                "notifications": {
                    "wecom": {
                        "enabled": True,
                        "webhook_url": url,
                        "webhook_env": "LOOPFORGE_WECOM_WEBHOOK",
                    }
                }
            }
            payload = send_run_notification(
                profile,
                {"run_id": "r1", "status": "completed", "loop_type": "scan", "outcome": "no_op", "summary": "无变化"},
            )

        self.assertEqual(payload["status"], "skipped")
        self.assertEqual(len(requests), 0)

    def test_notify_placeholder_sends_real_message(self) -> None:
        with capture_webhook({"errcode": 0, "errmsg": "ok"}) as (url, requests):
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "projects.json"
                raw = json.loads(CONFIG.read_text(encoding="utf-8"))
                raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
                raw["projects"][0]["notification_channel"] = "wecom_robot"
                raw["projects"][0]["webhook_url"] = url
                config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

                payload = notify_placeholder(config_path, "demo")

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["notification"]["status"], "sent")
        self.assertEqual(len(requests), 1)

    def test_project_run_once_records_notification_result(self) -> None:
        with capture_webhook({"errcode": 0, "errmsg": "ok"}) as (url, requests):
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "projects.json"
                raw = json.loads(CONFIG.read_text(encoding="utf-8"))
                raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
                raw["projects"][0]["notification_channel"] = "wecom_robot"
                raw["projects"][0]["webhook_url"] = url
                config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

                payload = project_run_once(config_path, "demo")

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["run"]["notification"]["status"], "sent")
        self.assertEqual(len(requests), 1)

    def test_project_run_once_uses_latest_notification_config_after_executor(self) -> None:
        with capture_webhook({"errcode": 0, "errmsg": "ok"}) as (url, requests):
            with tempfile.TemporaryDirectory() as tmp:
                config_path = Path(tmp) / "projects.json"
                raw = json.loads(CONFIG.read_text(encoding="utf-8"))
                raw["projects"] = [item for item in raw["projects"] if item.get("id") == "demo"]
                raw["projects"][0]["notification_channel"] = "none"
                raw["projects"][0].pop("webhook_url", None)
                config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

                def complete_after_config_update(*_args: object) -> dict:
                    updated = json.loads(config_path.read_text(encoding="utf-8"))
                    updated["projects"][0]["notification_channel"] = "wecom_robot"
                    updated["projects"][0]["webhook_url"] = url
                    config_path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
                    return {
                        "status": "completed",
                        "exit_code": 0,
                        "summary": "codex_cli 执行完成",
                        "final_status": "completed",
                    }

                with patch("loopforge.commands.run_executor", side_effect=complete_after_config_update):
                    payload = project_run_once(config_path, "demo")

        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["run"]["notification"]["status"], "sent")
        self.assertEqual(payload["run"]["notification"]["channel"], "wecom_robot")
        self.assertEqual(len(requests), 1)


def demo_dev_task_payload() -> dict:
    return {
        "version": 1,
        "description": "Demo Project 外部 backlog。LoopForge 每轮最多处理一个 item。",
        "items": [
            demo_item("demo-task-001", "观察任务完成全流程"),
            demo_item("demo-task-002", "补充运行报告入口"),
            demo_item("demo-task-003", "验证通知重发链路"),
        ],
    }


def demo_item(item_id: str, title: str) -> dict:
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
