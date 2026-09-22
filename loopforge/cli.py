"""LoopForge CLI。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from . import __version__
from .commands import (
    list_projects,
    validate_config,
    notify_placeholder,
    project_doctor,
    project_latest_report,
    project_pause,
    project_preview_run,
    project_remove,
    project_resolve_and_resume,
    project_resume_once,
    project_run_once,
    project_status,
    project_task_reserve,
    project_task_reservation_cancel,
    project_task_reservation_migrate_branches,
    schedule_tick,
)
from .onboarding import onboarding_apply, onboarding_preview
from .run_dispatch import execute_dispatch_request
from .server import ConsoleServer


def _config_path(value: Optional[str]) -> Optional[Path]:
    return Path(value).expanduser() if value else None


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _project_group_children(values: list[str] | None) -> list[Dict[str, str]] | None:
    if values is None:
        return None
    children: list[Dict[str, str]] = []
    for value in values:
        key, separator, path = str(value).partition("=")
        if not separator or not key.strip() or not path.strip():
            raise ValueError("--child 必须使用 key=/absolute/or/relative/path 格式")
        children.append({"key": key.strip(), "path": path.strip()})
    return children


def _print(payload: Dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return
    status = payload.get("status_label") or payload.get("status")
    summary = payload.get("summary") or ""
    print(f"{status}: {summary}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="loopforge")
    parser.add_argument("--version", action="version", version=f"loopforge {__version__}")
    parser.add_argument("--config", help="projects 配置路径")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status")
    sub.add_parser("schedule-tick")
    worker = sub.add_parser("run-worker", help=argparse.SUPPRESS)
    worker.add_argument("--request", required=True)
    console = sub.add_parser("console")
    console.add_argument("--host", default="127.0.0.1")
    console.add_argument("--port", type=int, default=8765)
    console.add_argument("--token", default=None)
    console.add_argument("--auto-schedule", action="store_true", default=_env_bool("LOOPFORGE_AUTO_SCHEDULE", False))
    console.add_argument("--schedule-interval", type=int, default=int(os.environ.get("LOOPFORGE_SCHEDULE_INTERVAL", "60")))
    config = sub.add_parser("config")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("validate")

    project = sub.add_parser("project")
    project_sub = project.add_subparsers(dest="project_command", required=True)
    project_sub.add_parser("list")
    onboard = project_sub.add_parser("onboard")
    onboard.add_argument("--root", required=True)
    onboard.add_argument("--type", required=True, dest="project_type")
    onboard.add_argument("--project-id", default="")
    onboard.add_argument("--name", default="")
    onboard.add_argument("--owner", required=True)
    onboard.add_argument("--planning-adapter", default="builtin")
    onboard.add_argument("--build-target", default="", help="Flutter 构建目标；无法唯一探测时必须显式提供")
    onboard.add_argument("--child", action="append", default=None, help="项目组子仓，格式 key=path，可重复")
    onboard_mode = onboard.add_mutually_exclusive_group()
    onboard_mode.add_argument("--dry-run", action="store_true")
    onboard_mode.add_argument("--apply", action="store_true")
    onboard.add_argument("--plan-hash", default="")
    reserve = project_sub.add_parser("task-reserve")
    reserve.add_argument("project_id")
    reserve.add_argument("--request", required=True, help="UTF-8 JSON 请求文件")
    reservation_cancel = project_sub.add_parser("task-reservation-cancel")
    reservation_cancel.add_argument("project_id")
    reservation_cancel.add_argument("reservation_id")
    reservation_cancel.add_argument("--discard-changes", action="store_true")
    reservation_migrate = project_sub.add_parser("task-reservation-migrate-branches")
    reservation_migrate.add_argument("project_id")
    reservation_migrate.add_argument("reservation_id")
    for name in [
        "status",
        "doctor",
        "run-once",
        "preview-run",
        "remove",
        "pause",
        "resume-once",
        "resolve-and-resume",
        "notify-test",
        "notify-resend",
        "latest-report",
    ]:
        command = project_sub.add_parser(name)
        command.add_argument("project_id")
        if name == "resolve-and-resume":
            command.add_argument("--reason", default="")
    return parser


def dispatch(args: argparse.Namespace) -> Dict[str, Any]:
    config_path = _config_path(args.config)
    if args.command == "run-worker":
        return execute_dispatch_request(Path(args.request))
    if args.command == "status":
        return list_projects(config_path)
    if args.command == "schedule-tick":
        return schedule_tick(config_path)
    if args.command == "console":
        ConsoleServer(
            config_path,
            host=args.host,
            port=args.port,
            token=args.token,
            auto_schedule=args.auto_schedule,
            schedule_interval_seconds=args.schedule_interval,
        ).serve_forever()
        return {"status": "completed", "summary": "控台已停止"}
    if args.command == "config" and args.config_command == "validate":
        return validate_config(config_path)
    if args.command == "project":
        cmd = args.project_command
        if cmd == "list":
            return list_projects(config_path)
        if cmd == "onboard":
            project_group_children = _project_group_children(args.child)
            if args.apply:
                return onboarding_apply(
                    config_path,
                    args.root,
                    args.project_type,
                    project_id=args.project_id,
                    name=args.name,
                    planning_adapter=args.planning_adapter,
                    owner=args.owner,
                    build_target=args.build_target,
                    project_group_children=project_group_children,
                    plan_hash=args.plan_hash,
                )
            return onboarding_preview(
                args.root,
                args.project_type,
                project_id=args.project_id,
                name=args.name,
                planning_adapter=args.planning_adapter,
                owner=args.owner,
                build_target=args.build_target,
                project_group_children=project_group_children,
            )
        if cmd == "status":
            return project_status(config_path, args.project_id)
        if cmd == "task-reserve":
            request_path = Path(args.request).expanduser()
            with request_path.open("r", encoding="utf-8") as handle:
                request = json.load(handle)
            return project_task_reserve(config_path, args.project_id, request)
        if cmd == "task-reservation-cancel":
            return project_task_reservation_cancel(
                config_path,
                args.project_id,
                args.reservation_id,
                discard_changes=args.discard_changes,
            )
        if cmd == "task-reservation-migrate-branches":
            return project_task_reservation_migrate_branches(
                config_path,
                args.project_id,
                args.reservation_id,
            )
        if cmd == "doctor":
            return project_doctor(config_path, args.project_id)
        if cmd == "run-once":
            return project_run_once(config_path, args.project_id)
        if cmd == "preview-run":
            return project_preview_run(config_path, args.project_id)
        if cmd == "remove":
            return project_remove(config_path, args.project_id)
        if cmd == "pause":
            return project_pause(config_path, args.project_id)
        if cmd == "resume-once":
            return project_resume_once(config_path, args.project_id)
        if cmd == "resolve-and-resume":
            return project_resolve_and_resume(config_path, args.project_id, args.reason)
        if cmd == "notify-test":
            return notify_placeholder(config_path, args.project_id)
        if cmd == "notify-resend":
            return notify_placeholder(config_path, args.project_id, manual_resend=True)
        if cmd == "latest-report":
            return project_latest_report(config_path, args.project_id)
    raise SystemExit(f"未知命令：{args.command}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw_argv
    if json_requested:
        raw_argv = [part for part in raw_argv if part != "--json"]
    args = parser.parse_args(raw_argv)
    args.json = bool(args.json or json_requested)
    try:
        payload = dispatch(args)
    except Exception as exc:
        payload = {"status": "failed", "status_label": "失败", "summary": str(exc)}
    _print(payload, args.json)
    return 0 if payload.get("status") not in {"failed"} else 1


if __name__ == "__main__":
    sys.exit(main())
