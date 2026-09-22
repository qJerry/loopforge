"""项目 runner 调用和 JSON contract 校验。"""

from __future__ import annotations

import json
import subprocess
from typing import Any, Dict, Iterable, List, Tuple

from .config import ProjectProfile
from .domain import BLOCKER_TYPES, PROJECT_STATUSES, RUN_STATUSES, utc_now


CORE_FIELDS = {"schema_version", "project_id", "command", "status", "summary"}
COMMAND_FIELDS = {
    "status": {"project_status", "active_task", "last_run", "latest_report"},
    "tasks": {"tasks", "capabilities"},
    "task-add": {"task"},
    "task-ai-create": {"task"},
    "task-reorder": {"tasks"},
    "run-once": {
        "run_id",
        "trigger",
        "ended_at",
        "duration_seconds",
        "task_id",
        "required_action",
        "blocker_type",
        "blocker_fingerprint",
        "report_path",
        "notification_intent",
    },
    "latest-report": {"report_path", "exists"},
}


def validate_runner_json(command: str, payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
    errors: List[str] = []
    missing = sorted(field for field in CORE_FIELDS if field not in payload)
    if missing:
        errors.append("缺少核心字段：" + ", ".join(missing))
    if "started_at" not in payload and "observed_at" not in payload:
        errors.append("缺少 started_at 或 observed_at")
    if payload.get("command") != command:
        errors.append(f"command 不匹配：期望 {command}")

    status = payload.get("status")
    allowed_statuses = RUN_STATUSES | PROJECT_STATUSES
    if status not in allowed_statuses:
        errors.append(f"非法 status：{status}")

    project_status = payload.get("project_status")
    if project_status is not None and project_status not in PROJECT_STATUSES:
        errors.append(f"非法 project_status：{project_status}")

    blocker_type = payload.get("blocker_type")
    if blocker_type is not None and blocker_type not in BLOCKER_TYPES:
        errors.append(f"非法 blocker_type：{blocker_type}")

    return not errors, errors


def misconfigured_response(project_id: str, command: str, errors: Iterable[str]) -> Dict[str, Any]:
    return {
        "schema_version": "1",
        "project_id": project_id,
        "command": command,
        "status": "misconfigured",
        "observed_at": utc_now(),
        "summary": "runner contract 校验失败",
        "required_action": "修复 runner JSON 输出：" + "；".join(errors),
        "errors": list(errors),
    }


def call_runner(profile: ProjectProfile, command: str, payload: Dict[str, Any] | None = None) -> Dict[str, Any]:
    stdin = json.dumps(payload, ensure_ascii=False) if payload is not None else None
    process = subprocess.run(
        [*profile.runner_command, command],
        cwd=str(profile.root_dir),
        check=False,
        capture_output=True,
        input=stdin,
        text=True,
    )
    if process.returncode != 0:
        return misconfigured_response(
            profile.project_id,
            command,
            [f"runner 退出码非 0：{process.returncode}", process.stderr.strip()],
        )
    try:
        payload = json.loads(process.stdout)
    except json.JSONDecodeError as exc:
        return misconfigured_response(profile.project_id, command, [f"JSON 解析失败：{exc}"])
    if not isinstance(payload, dict):
        return misconfigured_response(profile.project_id, command, ["runner 输出必须是 JSON object"])
    valid, errors = validate_runner_json(command, payload)
    if not valid:
        return misconfigured_response(profile.project_id, command, errors)
    return payload
