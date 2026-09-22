"""本地控制台 HTTP 服务。"""

from __future__ import annotations

import json
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .auto_scheduler import AutoScheduler
from .commands import (
    dashboard,
    global_notification_config,
    global_settings,
    notify_placeholder,
    project_cancel_run,
    project_clue_add,
    project_clue_decide,
    project_clues,
    project_detail,
    project_doctor,
    project_events,
    project_import,
    project_latest_report,
    project_notification_config,
    project_pause,
    project_preview_run,
    project_remove,
    project_runtime_config,
    project_runs,
    project_task_add,
    project_task_abandon,
    project_task_ai_create,
    project_task_cleanup,
    project_task_merge,
    project_task_review,
    project_task_history,
    project_task_reserve,
    project_task_reservation_cancel,
    project_tasks,
    read_report_html,
    report_metadata,
    task_token_usage,
)
from .onboarding import onboarding_apply, onboarding_preview
from .config import load_projects
from .run_dispatch import dispatch_project_action, schedule_dispatch_tick
from .worker_health import detect_worker_health


STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "console"


class ConsoleServer:
    def __init__(
        self,
        config_path: Optional[Path],
        host: str = "127.0.0.1",
        port: int = 8765,
        token: Optional[str] = None,
        auto_schedule: bool = False,
        schedule_interval_seconds: int = 60,
    ) -> None:
        self.config_path = config_path
        self.host = host
        self.port = port
        self.token = token or secrets.token_urlsafe(24)
        self.auto_schedule = auto_schedule
        self.schedule_interval_seconds = max(1, int(schedule_interval_seconds))
        self.scheduler: Optional[AutoScheduler] = None

    def serve_forever(self) -> None:
        handler = self._handler()
        httpd = ThreadingHTTPServer((self.host, self.port), handler)
        print(f"LoopForge 控台已启动：http://{self.host}:{self.port}/?token={self.token}", flush=True)
        if self.auto_schedule:
            self.scheduler = AutoScheduler(self.config_path, self.schedule_interval_seconds)
            self.scheduler.start()
            print(f"自动调度已启动：每 {self.schedule_interval_seconds} 秒执行一次 schedule-tick。", flush=True)
        print("按 Ctrl+C 停止。", flush=True)
        try:
            httpd.serve_forever()
        finally:
            if self.scheduler:
                self.scheduler.stop()

    def _handler(self):
        server_state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                try:
                    if parsed.path.startswith("/api/v1/"):
                        if not self._authorized(parsed.query):
                            self._json({"status": "failed", "summary": "未授权"}, HTTPStatus.UNAUTHORIZED)
                            return
                        self._handle_api(method, parsed.path, parsed.query)
                        return
                    if parsed.path.startswith("/reports/"):
                        if not self._authorized(parsed.query):
                            self._html("未授权", HTTPStatus.UNAUTHORIZED)
                            return
                        self._handle_report(parsed.path)
                        return
                    self._serve_static(parsed.path)
                except Exception as exc:
                    if parsed.path.startswith("/api/"):
                        self._json({"status": "failed", "summary": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                    else:
                        self._html(f"<h1>服务错误</h1><p>{exc}</p>", HTTPStatus.INTERNAL_SERVER_ERROR)

            def _authorized(self, query: str) -> bool:
                header = self.headers.get("Authorization", "")
                token_header = self.headers.get("X-LoopForge-Token", "")
                query_token = parse_qs(query).get("token", [""])[0]
                return header == f"Bearer {server_state.token}" or token_header == server_state.token or query_token == server_state.token

            def _read_json_body(self) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or "0")
                if length <= 0:
                    return {}
                raw = self.rfile.read(length).decode("utf-8")
                return json.loads(raw) if raw else {}

            def _handle_api(self, method: str, path: str, query: str) -> None:
                parts = [unquote(part) for part in path.strip("/").split("/")]
                # parts: api, v1, ...
                if method == "GET" and parts == ["api", "v1", "projects"]:
                    self._json(dashboard(server_state.config_path))
                    return
                if method == "POST" and parts == ["api", "v1", "projects", "import"]:
                    body = self._read_json_body()
                    root_dir = str(body.get("root_dir") or body.get("path") or "")
                    self._json(project_import(server_state.config_path, root_dir, str(body.get("project_id") or ""), str(body.get("name") or "")))
                    return
                if method == "POST" and parts == ["api", "v1", "projects", "onboarding", "preview"]:
                    body = self._read_json_body()
                    self._json(
                        onboarding_preview(
                            str(body.get("root_dir") or body.get("path") or ""),
                            str(body.get("project_type") or body.get("type") or ""),
                            project_id=str(body.get("project_id") or ""),
                            name=str(body.get("name") or ""),
                            planning_adapter=str(body.get("planning_adapter") or ""),
                            owner=str(body.get("owner") or ""),
                            build_target=str(body.get("build_target") or ""),
                            project_group_children=body.get("project_group_children"),
                        )
                    )
                    return
                if method == "POST" and parts == ["api", "v1", "projects", "onboarding", "apply"]:
                    body = self._read_json_body()
                    self._json(
                        onboarding_apply(
                            server_state.config_path,
                            str(body.get("root_dir") or body.get("path") or ""),
                            str(body.get("project_type") or body.get("type") or ""),
                            project_id=str(body.get("project_id") or ""),
                            name=str(body.get("name") or ""),
                            planning_adapter=str(body.get("planning_adapter") or ""),
                            owner=str(body.get("owner") or ""),
                            build_target=str(body.get("build_target") or ""),
                            project_group_children=body.get("project_group_children"),
                            plan_hash=str(body.get("plan_hash") or ""),
                        )
                    )
                    return
                if method == "POST" and parts == ["api", "v1", "schedule-tick"]:
                    payload = schedule_dispatch_tick(server_state.config_path)
                    self._json(payload, HTTPStatus.ACCEPTED if payload.get("status") == "accepted" else HTTPStatus.OK)
                    return
                if method == "GET" and parts == ["api", "v1", "scheduler"]:
                    scheduler = server_state.scheduler
                    self._json(
                        {
                            "status": "completed",
                            "summary": "自动调度状态已读取",
                            "scheduler": scheduler.snapshot()
                            if scheduler
                            else {
                                "enabled": False,
                                "interval_seconds": server_state.schedule_interval_seconds,
                                "tick_count": 0,
                                "last_started_at": None,
                                "last_finished_at": None,
                                "last_result": None,
                                "last_error": None,
                            },
                        }
                    )
                    return
                if method == "GET" and parts == ["api", "v1", "settings"]:
                    self._json(global_settings(server_state.config_path))
                    return
                if method == "GET" and parts == ["api", "v1", "worker-health"]:
                    project_id = str(parse_qs(query).get("project_id", [""])[0] or "").strip()
                    if not project_id:
                        self._json(detect_worker_health())
                        return
                    profile = next(
                        (item for item in load_projects(server_state.config_path) if item.project_id == project_id),
                        None,
                    )
                    if profile is None:
                        self._json({"status": "failed", "summary": f"未知项目：{project_id}"}, HTTPStatus.NOT_FOUND)
                        return
                    self._json(detect_worker_health(profile=profile))
                    return
                if method == "POST" and parts == ["api", "v1", "settings", "notification-config"]:
                    body = self._read_json_body()
                    self._json(
                        global_notification_config(
                            server_state.config_path,
                            body.get("enabled") is True,
                            str(body.get("webhook_env") or ""),
                            str(body.get("webhook_url") or ""),
                        )
                    )
                    return
                if method == "GET" and parts == ["api", "v1", "token-usage", "tasks"]:
                    query_values = parse_qs(query)
                    project_filter = str(query_values.get("project_id", [""])[0] or "").strip()
                    try:
                        requested_limit = int(query_values.get("limit", ["20"])[0])
                    except (TypeError, ValueError):
                        requested_limit = 20
                    self._json(
                        task_token_usage(
                            server_state.config_path,
                            project_id=project_filter,
                            limit=max(1, min(100, requested_limit)),
                        )
                    )
                    return
                if len(parts) < 4 or parts[:3] != ["api", "v1", "projects"]:
                    self._json({"status": "failed", "summary": "未知 API"}, HTTPStatus.NOT_FOUND)
                    return
                project_id = parts[3]
                action = parts[4] if len(parts) > 4 else ""
                body = self._read_json_body() if method == "POST" else {}

                if method == "GET" and not action:
                    self._json(project_detail(server_state.config_path, project_id))
                elif method == "GET" and action == "doctor":
                    self._json(project_doctor(server_state.config_path, project_id))
                elif method == "GET" and action == "latest-report":
                    self._json(report_metadata(server_state.config_path, project_id))
                elif method == "GET" and action == "runs":
                    self._json(project_runs(server_state.config_path, project_id))
                elif method == "GET" and action == "events":
                    self._json(project_events(server_state.config_path, project_id))
                elif method == "GET" and action == "tasks" and len(parts) == 5:
                    self._json(project_tasks(server_state.config_path, project_id))
                elif method == "GET" and action == "tasks" and len(parts) == 6 and parts[5] == "history":
                    raw_limit = parse_qs(query).get("limit", ["20"])[0]
                    self._json(project_task_history(server_state.config_path, project_id, max(1, min(20, int(raw_limit)))))
                elif method == "POST" and action == "tasks" and len(parts) == 5:
                    self._json(
                        project_task_add(
                            server_state.config_path,
                            project_id,
                            str(body.get("title") or ""),
                            str(body.get("description") or ""),
                            body.get("acceptance"),
                            body.get("targets"),
                        )
                    )
                elif method == "POST" and action == "tasks" and len(parts) == 6 and parts[5] == "reservations":
                    self._json(project_task_reserve(server_state.config_path, project_id, body))
                elif (
                    method == "POST"
                    and action == "tasks"
                    and len(parts) == 8
                    and parts[5] == "reservations"
                    and parts[7] == "cancel"
                ):
                    self._json(
                        project_task_reservation_cancel(
                            server_state.config_path,
                            project_id,
                            parts[6],
                            discard_changes=body.get("discard_changes") is True,
                        )
                    )
                elif method == "POST" and action == "tasks" and len(parts) == 6 and parts[5] == "ai-create":
                    self._json(project_task_ai_create(server_state.config_path, project_id, str(body.get("prompt") or "")))
                elif method == "POST" and action == "tasks" and len(parts) == 7 and parts[6] == "review":
                    self._json(
                        project_task_review(
                            server_state.config_path,
                            project_id,
                            parts[5],
                            str(body.get("decision") or ""),
                            str(body.get("feedback") or body.get("reason") or ""),
                        )
                    )
                elif method == "POST" and action == "tasks" and len(parts) == 7 and parts[6] == "merge":
                    self._json(
                        project_task_merge(
                            server_state.config_path,
                            project_id,
                            parts[5],
                            str(body.get("target_branch") or ""),
                            str(body.get("merge_method") or ""),
                        )
                    )
                elif method == "POST" and action == "tasks" and len(parts) == 7 and parts[6] == "cleanup":
                    self._json(
                        project_task_cleanup(
                            server_state.config_path,
                            project_id,
                            parts[5],
                            discard_changes=body.get("discard_changes") is True,
                        )
                    )
                elif method == "POST" and action == "tasks" and len(parts) == 7 and parts[6] == "abandon":
                    self._json(
                        project_task_abandon(
                            server_state.config_path,
                            project_id,
                            parts[5],
                            str(body.get("reason") or ""),
                            discard_changes=body.get("discard_changes") is True,
                        )
                    )
                elif method == "GET" and action == "clues" and len(parts) == 5:
                    self._json(project_clues(server_state.config_path, project_id))
                elif method == "POST" and action == "clues" and len(parts) == 5:
                    self._json(project_clue_add(server_state.config_path, project_id, body))
                elif method == "POST" and action == "clues" and len(parts) == 6 and parts[5] == "decide":
                    self._json(
                        project_clue_decide(
                            server_state.config_path,
                            project_id,
                            str(body.get("clue_id") or ""),
                            str(body.get("decision") or ""),
                            str(body.get("reason") or ""),
                            str(body.get("task_title") or ""),
                            str(body.get("task_description") or ""),
                        )
                    )
                elif method == "POST" and action == "run-once":
                    payload = dispatch_project_action(
                        server_state.config_path,
                        project_id,
                        "run_once",
                        loop_type=str(body.get("loop_type") or "dev"),
                    )
                    self._json(payload, HTTPStatus.ACCEPTED if payload.get("status") == "accepted" else HTTPStatus.OK)
                elif method == "POST" and action == "start-run":
                    payload = dispatch_project_action(server_state.config_path, project_id, "start_run")
                    self._json(payload, HTTPStatus.ACCEPTED if payload.get("status") == "accepted" else HTTPStatus.OK)
                elif method == "POST" and action == "preview-run":
                    self._json(project_preview_run(server_state.config_path, project_id, loop_type=str(body.get("loop_type") or "dev")))
                elif method == "POST" and action == "cancel-run":
                    self._json(project_cancel_run(server_state.config_path, project_id, str(body.get("run_id") or ""), str(body.get("reason") or "")))
                elif method == "POST" and action == "notification-config":
                    self._json(
                        project_notification_config(
                            server_state.config_path,
                            project_id,
                            str(body.get("notification_channel") or ""),
                            str(body.get("webhook_url") or ""),
                        )
                    )
                elif method == "POST" and action == "runtime-config":
                    self._json(
                        project_runtime_config(
                            server_state.config_path,
                            project_id,
                            body.get("schedule_enabled") is True,
                            body.get("auto_commit") is True,
                            str(body.get("schedule_frequency") or "hourly"),
                            str(body.get("codex_model") or ""),
                            str(body.get("codex_reasoning_effort") or ""),
                            str(body.get("codex_sandbox") or ""),
                            str(body.get("automation_mode") or ""),
                            worker_provider=str(body.get("worker_provider") or ""),
                            worker_settings=body.get("worker_settings") if isinstance(body.get("worker_settings"), dict) else None,
                            worker_network=body.get("worker_network") if isinstance(body.get("worker_network"), dict) else None,
                        )
                    )
                elif method == "POST" and action == "remove":
                    self._json(project_remove(server_state.config_path, project_id))
                elif method == "POST" and action == "pause":
                    self._json(project_pause(server_state.config_path, project_id))
                elif method == "POST" and action == "resume-once":
                    payload = dispatch_project_action(
                        server_state.config_path,
                        project_id,
                        "resume_once",
                        reason=str(body.get("reason") or ""),
                    )
                    self._json(payload, HTTPStatus.ACCEPTED if payload.get("status") == "accepted" else HTTPStatus.OK)
                elif method == "POST" and action == "resolve-and-resume":
                    payload = dispatch_project_action(
                        server_state.config_path,
                        project_id,
                        "resolve_and_resume",
                        reason=str(body.get("reason") or ""),
                    )
                    self._json(payload, HTTPStatus.ACCEPTED if payload.get("status") == "accepted" else HTTPStatus.OK)
                elif method == "POST" and action == "notify-test":
                    self._json(notify_placeholder(server_state.config_path, project_id))
                elif method == "POST" and action == "notify-resend":
                    self._json(notify_placeholder(server_state.config_path, project_id, manual_resend=True))
                else:
                    self._json({"status": "failed", "summary": "未知 API"}, HTTPStatus.NOT_FOUND)

            def _handle_report(self, path: str) -> None:
                parts = [unquote(part) for part in path.strip("/").split("/")]
                if len(parts) != 3:
                    self._html("报告路径无效", HTTPStatus.NOT_FOUND)
                    return
                _, project_id, run_id = parts
                html_body = read_report_html(server_state.config_path, project_id, run_id)
                self._html(html_body)

            def _serve_static(self, path: str) -> None:
                if path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                    return
                if path in {"", "/"}:
                    self._serve_index()
                    return
                safe_name = path.lstrip("/")
                target = (STATIC_DIR / safe_name).resolve()
                if STATIC_DIR.resolve() not in target.parents or not target.exists():
                    self._html("页面不存在", HTTPStatus.NOT_FOUND)
                    return
                content_type = "text/css" if target.suffix == ".css" else "application/javascript" if target.suffix == ".js" else "text/plain"
                data = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", f"{content_type}; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _serve_index(self) -> None:
                index_path = STATIC_DIR / "index.html"
                body = index_path.read_text(encoding="utf-8").replace("__LOOPFORGE_TOKEN__", server_state.token)
                self._html(body)

            def _json(self, payload: Dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
                data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
                data = body.encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        return Handler
