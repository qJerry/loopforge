"""serial 调度选择。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import ProjectProfile
from .domain import AUTOMATION_MODE_ALLOWED_LOOPS, SCHEDULE_FREQUENCIES

DEV_STATES = {"open", "claimed", "spec_ready", "prd_ready", "coding"}
USER_WAITING_STATES = {
    "spec_blocked",
    "prd_blocked",
    "dev_blocked",
    "blocked",
    "ready_for_review",
    "accepted",
    "merged",
}
LOOP_PRIORITY = {"dev": 0, "scan": 1}


def _last_run_at(status_payload: Dict[str, Any]) -> str:
    last_run = status_payload.get("last_run") or {}
    if isinstance(last_run, dict):
        return str(last_run.get("ended_at") or last_run.get("started_at") or "")
    return ""


def _schedule_base_at(profile: ProjectProfile, status_payload: Dict[str, Any]) -> Optional[datetime]:
    candidates = [
        value
        for value in [
            _parse_time(_last_run_at(status_payload)),
            _parse_time(profile.schedule_started_at or ""),
        ]
        if value is not None
    ]
    return max(candidates) if candidates else None


def _parse_time(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def is_due(profile: ProjectProfile, status_payload: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    base_at = _schedule_base_at(profile, status_payload)
    if base_at is None:
        return True
    interval = SCHEDULE_FREQUENCIES.get(profile.schedule_frequency, SCHEDULE_FREQUENCIES["hourly"])
    current = now or datetime.now(timezone.utc)
    return (current - base_at).total_seconds() >= interval


def next_due_at(profile: ProjectProfile, status_payload: Dict[str, Any], now: Optional[datetime] = None) -> Optional[str]:
    if not profile.enabled or not profile.schedule_enabled or not profile.is_valid:
        return None
    current = now or datetime.now(timezone.utc)
    base_at = _schedule_base_at(profile, status_payload)
    if base_at is None:
        return current.isoformat()
    interval = SCHEDULE_FREQUENCIES.get(profile.schedule_frequency, SCHEDULE_FREQUENCIES["hourly"])
    return (base_at + timedelta(seconds=interval)).isoformat()


def choose_project(candidates: Iterable[Tuple[ProjectProfile, Dict[str, Any]]], respect_frequency: bool = False) -> Optional[ProjectProfile]:
    runnable: List[Tuple[int, int, str, ProjectProfile]] = []
    for profile, status in candidates:
        if not profile.enabled or not profile.schedule_enabled or not profile.is_valid:
            continue
        if respect_frequency and not is_due(profile, status):
            continue
        if status.get("project_status") in {"paused", "blocked", "misconfigured", "running"}:
            continue
        active_task = status.get("active_task") or {}
        has_active = bool(active_task) and bool(active_task.get("can_continue", True))
        has_backlog = bool(status.get("has_open_backlog"))
        if not has_active and not has_backlog:
            continue
        priority_bucket = 0 if has_active else 1
        runnable.append((priority_bucket, profile.priority, _last_run_at(status), profile))
    if not runnable:
        return None
    runnable.sort(key=lambda item: (item[0], item[1], item[2]))
    return runnable[0][3]


def choose_project_loop(
    candidates: Iterable[Tuple[ProjectProfile, Dict[str, Any]]],
    respect_frequency: bool = False,
) -> Optional[Tuple[ProjectProfile, str]]:
    runnable: List[Tuple[int, int, str, ProjectProfile, str]] = []
    for profile, status in candidates:
        if not profile.enabled or not profile.schedule_enabled or not profile.is_valid:
            continue
        if respect_frequency and not is_due(profile, status):
            continue
        loop_type = loop_type_for_project(profile, status)
        if not loop_type:
            continue
        runnable.append((LOOP_PRIORITY[loop_type], profile.priority, _last_run_at(status), profile, loop_type))
    if not runnable:
        return None
    runnable.sort(key=lambda item: (item[0], item[1], item[2]))
    _, _, _, profile, loop_type = runnable[0]
    return profile, loop_type


def loop_type_for_project(profile: ProjectProfile, status: Dict[str, Any]) -> Optional[str]:
    allowed = AUTOMATION_MODE_ALLOWED_LOOPS.get(profile.automation_mode, set())
    if not allowed:
        return None
    for loop_type in loop_types_for_status(status):
        if loop_type in allowed:
            return loop_type
    return None


def loop_type_for_status(status: Dict[str, Any]) -> Optional[str]:
    loop_types = loop_types_for_status(status)
    return loop_types[0] if loop_types else None


def loop_types_for_status(status: Dict[str, Any]) -> List[str]:
    project_status = str(status.get("project_status") or status.get("status") or "")
    declared = status.get("runnable_loops") if isinstance(status.get("runnable_loops"), list) else []
    declared = [loop_type for loop_type in ("dev",) if loop_type in declared]
    if project_status in {"paused", "misconfigured", "running", "failed"}:
        return []
    if declared:
        return declared + ["scan"]
    if project_status == "blocked":
        return []

    active_task = status.get("active_task") if isinstance(status.get("active_task"), dict) else None
    if active_task and active_task.get("can_continue", True):
        task_state = str(active_task.get("status") or active_task.get("state") or "")
        if task_state in DEV_STATES:
            return ["dev", "scan"]
        if task_state in USER_WAITING_STATES:
            return []

    if status.get("has_open_backlog"):
        return ["dev", "scan"]
    return ["scan"]
