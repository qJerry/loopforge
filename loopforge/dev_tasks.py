"""任务运行时门面；优先使用 v2 单任务文件，并保留 legacy 迁移兼容。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import ProjectProfile
from .domain import TASK_STATES, TERMINAL_TASK_STATES, utc_now
from .task_git import (
    publish_main_change,
    publish_task_mutation_from_isolated_main,
    push_managed_feature_revision,
)
from .task_contract import task_uses_document_contract
from .task_publisher import publish_new_task, reserve_task
from .task_store import (
    archive_task_file,
    load_active_tasks,
    load_history_tasks,
    load_task,
)
from .targets import TargetPlanError, target_plan_error_payload, task_target_plan


RUNNING_TASK_STATES = {"claimed", "spec_ready", "prd_ready", "coding"}
TASK_STATE_LABELS = {
    "open": "待领取",
    "claimed": "已领取",
    "spec_ready": "规格就绪",
    "spec_blocked": "规格阻塞",
    "prd_ready": "规格就绪",
    "prd_blocked": "规格阻塞",
    "coding": "实现中",
    "dev_blocked": "开发阻塞",
    "ready_for_review": "待验收",
    "accepted": "已验收",
    "merged": "已合入",
    "blocked": "执行阻塞",
    "completed": "已完成",
    "abandoned": "已放弃",
}
TASK_STATE_ACTIONS = {
    "open": "等待项目脚本领取",
    "claimed": "等待进入开发执行",
    "spec_ready": "进入开发执行",
    "spec_blocked": "等待人工处理规格阻塞",
    "prd_ready": "进入实现和验证",
    "prd_blocked": "等待人工处理规格阻塞",
    "coding": "继续实现、验证和归档",
    "dev_blocked": "等待人工处理开发阻塞",
    "ready_for_review": "等待用户验收",
    "accepted": "等待合入",
    "merged": "等待清理",
    "blocked": "等待人工处理执行阻塞",
    "completed": "任务已完成",
    "abandoned": "任务已放弃",
}
DEFAULT_PRIORITY = "P2"
DEV_TASK_STATES = {"spec_ready", "prd_ready", "coding"}
USER_WAITING_TASK_STATES = {"spec_blocked", "prd_blocked", "dev_blocked", "blocked", "ready_for_review", "accepted", "merged"}
BLOCKER_FIELDS = (
    "blocker_reason",
    "last_error",
    "required_action",
    "active_blocker",
    "blockers",
    "diagnostic",
)
_LEGACY_TASK_FIELD_ORDER = (
    "id",
    "title",
    "description",
    "status",
    "priority",
    "planning_level",
    "source",
    "acceptance",
    "acceptance_refs",
    "agent",
    "created_at",
    "updated_at",
)


def dev_task_path(profile: ProjectProfile) -> Path:
    return profile.root_dir / "data" / "dev-task.json"


def dev_task_history_path(profile: ProjectProfile, at: Optional[datetime] = None) -> Path:
    observed_at = at or datetime.now(timezone.utc)
    return profile.root_dir / "data" / "history" / f"dev-task-{observed_at.strftime('%Y%m%d')}.json"


def default_dev_task_payload(project_name: str) -> Dict[str, Any]:
    return {
        "version": 1,
        "description": f"{project_name} 外部 backlog。LoopForge 每轮最多处理一个 item。",
        "items": [],
    }


def ensure_dev_task_file(root_dir: Path, project_name: str) -> bool:
    legacy = root_dir / "data" / "dev-task.json"
    if legacy.exists():
        validate_payload(read_payload(legacy))
        return False
    path = root_dir / "data" / "tasks" / ".gitkeep"
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    return True


def read_payload(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("data/dev-task.json 根节点必须是 JSON object")
    return payload


def write_payload(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(_ordered_legacy_payload(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _ordered_legacy_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in ("version", "description", "items"):
        if key not in payload:
            continue
        if key == "items" and isinstance(payload[key], list):
            result[key] = [
                _ordered_legacy_task(item) if isinstance(item, dict) else item
                for item in payload[key]
            ]
        else:
            result[key] = payload[key]
    for key in sorted(set(payload) - set(result)):
        result[key] = payload[key]
    return result


def _ordered_legacy_task(task: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key in _LEGACY_TASK_FIELD_ORDER:
        if key in task:
            result[key] = task[key]
    for key in sorted(set(task) - set(result)):
        result[key] = task[key]
    return result


def load_dev_tasks(profile: ProjectProfile) -> Dict[str, Any]:
    if _uses_v2_storage(profile):
        return {"version": 2, "items": load_active_tasks(profile)}
    path = dev_task_path(profile)
    if not path.exists():
        raise ValueError("缺少 data/dev-task.json")
    payload = read_payload(path)
    validate_payload(payload)
    return payload


def save_dev_tasks(profile: ProjectProfile, payload: Dict[str, Any]) -> None:
    if payload.get("version") == 2 or _uses_v2_storage(profile):
        _save_v2_payload(profile, payload)
        return
    validate_payload(payload)
    write_payload(dev_task_path(profile), payload)


def load_dev_task_history(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"version": 1, "items": []}
    payload = read_payload(path)
    validate_history_payload(payload)
    return payload


def validate_history_payload(payload: Dict[str, Any]) -> None:
    if payload.get("version") != 1:
        raise ValueError("dev-task-history version 必须是 1")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("dev-task-history items 必须是数组")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"history items[{index}] 必须是 object")
        _required_text(item, "id", index)
        _required_text(item, "title", index)
        status = _required_text(item, "status", index)
        if status not in TERMINAL_TASK_STATES:
            raise ValueError(f"history 不支持非终态 item status：{status}")


def archive_terminal_item(profile: ProjectProfile, payload: Dict[str, Any], item: Dict[str, Any]) -> Dict[str, Any]:
    status = str(item.get("status") or "")
    if status not in TERMINAL_TASK_STATES:
        return item

    archived_item = dict(item)
    now = utc_now()
    archived_item.setdefault("completed_at", now if status == "completed" else None)
    archived_item["archived_at"] = now

    items = payload.get("items", [])
    payload["items"] = [
        current
        for current in items
        if not (isinstance(current, dict) and current.get("id") == archived_item.get("id"))
    ]

    history_path = dev_task_history_path(profile)
    history = load_dev_task_history(history_path)
    history_items = history.setdefault("items", [])
    existing_index = next(
        (
            index
            for index, current in enumerate(history_items)
            if isinstance(current, dict) and current.get("id") == archived_item.get("id")
        ),
        None,
    )
    if existing_index is None:
        history_items.append(archived_item)
    else:
        history_items[existing_index] = archived_item

    validate_history_payload(history)
    write_payload(history_path, history)
    save_dev_tasks(profile, payload)
    return archived_item


def validate_payload(payload: Dict[str, Any]) -> None:
    if payload.get("version") != 1:
        raise ValueError("data/dev-task.json version 必须是 1")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("data/dev-task.json items 必须是数组")
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"items[{index}] 必须是 object")
        item_id = _required_text(item, "id", index)
        _required_text(item, "title", index)
        status = _required_text(item, "status", index)
        if item_id in seen:
            raise ValueError(f"重复的 item id：{item_id}")
        if status not in TASK_STATES:
            raise ValueError(f"不支持的 item status：{status}")
        seen.add(item_id)


def _required_text(item: Dict[str, Any], key: str, index: int) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"items[{index}].{key} 必须是非空字符串")
    return value.strip()


def status_label(status: Any) -> str:
    return TASK_STATE_LABELS.get(str(status), str(status or "无"))


def blocker_reason(item: Dict[str, Any]) -> str:
    for key in ["blocker_reason", "last_error", "required_action"]:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    structured = _structured_blocker_reason(item)
    if structured:
        return structured
    return ""


def clear_blocker_fields(item: Dict[str, Any]) -> None:
    for key in BLOCKER_FIELDS:
        item.pop(key, None)


def refresh_satisfied_blocker(profile: ProjectProfile, payload: Dict[str, Any], item: Dict[str, Any]) -> bool:
    status = str(item.get("status") or "")
    if status not in {"blocked", "prd_blocked", "spec_blocked", "dev_blocked"}:
        return False

    active_blocker = _active_blocker(item)
    dependency_ids = _blocker_dependency_ids(active_blocker)
    if not dependency_ids:
        return False

    completed_ids = _completed_item_ids(profile, payload)
    if any(dependency_id not in completed_ids for dependency_id in dependency_ids):
        return False

    item["status"] = _resume_status_for_satisfied_blocker(item, status)
    item["updated_at"] = utc_now()
    clear_blocker_fields(item)
    save_dev_tasks(profile, payload)
    return True


def _blocker_dependency_ids(active_blocker: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(active_blocker, dict):
        return []
    dependencies = active_blocker.get("dependencies")
    if not isinstance(dependencies, list):
        return []
    return [str(dependency).strip() for dependency in dependencies if str(dependency).strip()]


def _completed_item_ids(profile: ProjectProfile, payload: Dict[str, Any]) -> set[str]:
    completed: set[str] = set()
    for item in payload.get("items", []):
        if isinstance(item, dict) and item.get("status") == "completed":
            item_id = str(item.get("id") or "").strip()
            if item_id:
                completed.add(item_id)

    if payload.get("version") == 2 or _uses_v2_storage(profile):
        history = load_history_tasks(profile, limit=10000)
        for item in history["items"]:
            if item.get("status") == "completed":
                completed.add(str(item["id"]))
        return completed

    data_dir = dev_task_path(profile).parent
    history_paths = [
        *sorted((data_dir / "history").glob("dev-task-*.json")),
        *sorted(data_dir.glob("dev-task-history-*.json")),
    ]
    for history_path in history_paths:
        history = load_dev_task_history(history_path)
        for item in history.get("items", []):
            if isinstance(item, dict) and item.get("status") == "completed":
                item_id = str(item.get("id") or "").strip()
                if item_id:
                    completed.add(item_id)
    return completed


def _resume_status_for_satisfied_blocker(item: Dict[str, Any], previous_status: str) -> str:
    resume_status = str(item.get("resume_status") or "").strip()
    if resume_status in TASK_STATES and resume_status not in TERMINAL_TASK_STATES | {"blocked", "prd_blocked", "spec_blocked", "dev_blocked"}:
        return resume_status
    if previous_status in {"prd_blocked", "spec_blocked"}:
        return "spec_ready" if previous_status == "spec_blocked" else "prd_ready"
    if previous_status == "dev_blocked":
        return "coding"
    return "coding"


def _structured_blocker_reason(item: Dict[str, Any]) -> str:
    blockers = item.get("blockers")
    if not isinstance(blockers, list):
        return ""
    active_blocker = str(item.get("active_blocker") or "").strip()
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        blocker_id = str(blocker.get("id") or blocker.get("code") or "").strip()
        if active_blocker and blocker_id != active_blocker:
            continue
        message = str(blocker.get("message") or "").strip()
        resolution = str(blocker.get("resolution") or "").strip()
        if message and resolution:
            return f"{message}；{resolution}"
        if message:
            return message
        if resolution:
            return resolution
    return ""


def diagnostic_for_item(
    item: Dict[str, Any],
    profile: ProjectProfile,
    *,
    kind: str = "",
    summary: str = "",
    required_action: str = "",
    git_status: Optional[Dict[str, Any]] = None,
    artifacts: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Return a stable troubleshooting projection for UI, history and artifacts."""
    active_blocker = _active_blocker(item)
    target = _diagnostic_target(item, active_blocker)
    failed_command = str((active_blocker or {}).get("command") or "").strip()
    reason = blocker_reason(item)
    next_action = (
        str(required_action or "").strip()
        or str((active_blocker or {}).get("resolution") or "").strip()
        or reason
        or TASK_STATE_ACTIONS.get(str(item.get("status") or ""), "")
    )
    dirty_files: List[str] = []
    if isinstance(git_status, dict):
        dirty_files = [
            _git_status_path(line)
            for line in str(git_status.get("porcelain") or "").splitlines()
            if line.strip()
        ]
    diagnostic = {
        "project_id": profile.project_id,
        "project_name": profile.name,
        "root_dir": str(profile.root_dir),
        "item_id": str(item.get("id") or ""),
        "item_title": str(item.get("title") or ""),
        "item_status": str(item.get("status") or ""),
        "target_id": str((target or {}).get("id") or (active_blocker or {}).get("target") or ""),
        "repo": str((target or {}).get("repo") or ""),
        "worktree": str((target or {}).get("worktree") or (target or {}).get("worktree_path") or ""),
        "branch": str((target or {}).get("branch") or ""),
        "blocker_code": str((active_blocker or {}).get("code") or item.get("active_blocker") or ""),
        "failure_kind": kind or _diagnostic_kind(item, active_blocker, target, git_status),
        "summary": summary or reason or str(item.get("last_run_summary") or ""),
        "reason": reason,
        "failed_command": failed_command,
        "next_action": next_action,
        "can_resume": str(item.get("status") or "") not in TERMINAL_TASK_STATES,
        "dirty_files": dirty_files,
        "artifacts": artifacts or {},
    }
    meaningful_kind = diagnostic["failure_kind"] in {
        "dirty_worktree",
        "ready_for_merge_pending_merge",
        "non_terminal_task",
        "verification_failed",
        "dependency_not_completed",
        "failed",
        "timeout",
        "misconfigured",
        "prd_blocked",
        "blocked",
    }
    diagnostic["has_signal"] = meaningful_kind or any(
        bool(diagnostic.get(key))
        for key in [
            "target_id",
            "repo",
            "worktree",
            "branch",
            "blocker_code",
            "failed_command",
            "reason",
        ]
    ) or bool(dirty_files)
    return diagnostic


def _active_blocker(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    blockers = item.get("blockers")
    if not isinstance(blockers, list):
        return None
    active_blocker = str(item.get("active_blocker") or "").strip()
    fallback: Optional[Dict[str, Any]] = None
    for blocker in blockers:
        if not isinstance(blocker, dict):
            continue
        if fallback is None:
            fallback = blocker
        blocker_id = str(blocker.get("id") or blocker.get("code") or "").strip()
        if active_blocker and blocker_id == active_blocker:
            return blocker
    return fallback if not active_blocker else None


def _diagnostic_target(item: Dict[str, Any], active_blocker: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    targets = item.get("targets")
    if not isinstance(targets, list):
        return None
    blocker_target = str((active_blocker or {}).get("target") or "").strip()
    if blocker_target:
        for target in targets:
            if isinstance(target, dict) and str(target.get("id") or "") == blocker_target:
                return target
    for wanted in ["blocked", "coding", "prd_ready", "claimed", "pending"]:
        for target in targets:
            if isinstance(target, dict) and str(target.get("status") or "") == wanted:
                return target
    for target in targets:
        if isinstance(target, dict) and str(target.get("status") or "") == "ready_for_merge":
            return target
    return None


def _diagnostic_kind(
    item: Dict[str, Any],
    active_blocker: Optional[Dict[str, Any]],
    target: Optional[Dict[str, Any]],
    git_status: Optional[Dict[str, Any]],
) -> str:
    if isinstance(git_status, dict) and git_status.get("status"):
        return str(git_status.get("status"))
    if active_blocker and active_blocker.get("code"):
        return str(active_blocker.get("code"))
    status = str(item.get("status") or "")
    targets = item.get("targets")
    if (
        status not in TERMINAL_TASK_STATES
        and isinstance(targets, list)
        and targets
        and all(isinstance(target, dict) and target.get("status") == "ready_for_merge" for target in targets)
    ):
        return "ready_for_merge_pending_merge"
    if target and target.get("status"):
        return str(target.get("status"))
    return status


def _git_status_path(raw_line: str) -> str:
    line = raw_line.rstrip()
    path = line[3:] if len(line) > 3 and line[2] == " " else line[2:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip()


def normalize_item(item: Dict[str, Any], profile: ProjectProfile) -> Dict[str, Any]:
    status = str(item.get("status") or "open")
    planning_level = str(item.get("planning_level") or "complex")
    if planning_level not in {"complex", "lightweight"}:
        planning_level = "complex"
    docs = _normalized_docs_for_item(item)
    blockers = item.get("blockers") if isinstance(item.get("blockers"), list) else []
    agent = str(item.get("agent") or profile.default_agent or "codex")
    git_contract = item.get("git") if isinstance(item.get("git"), dict) else {}
    target_plan_payload: Dict[str, Any] = {"status": "empty", "ordered_target_ids": [], "targets": []}
    try:
        plan = task_target_plan(profile, item)
        target_plan_payload = plan.to_payload()
    except TargetPlanError as exc:
        target_plan_payload = target_plan_error_payload(exc)

    normalized = {
        "id": str(item.get("id") or ""),
        "schema_version": item.get("schema_version"),
        "version": item.get("version"),
        "title": str(item.get("title") or ""),
        "description": str(item.get("description") or ""),
        "priority": str(item.get("priority") or DEFAULT_PRIORITY),
        "planning_level": planning_level,
        "status": status,
        "state": status,
        "state_label": status_label(status),
        "source": str(item.get("source") or "manual"),
        "agent": agent,
        "agent_status": item.get("agent_status"),
        "agent_executor": item.get("agent_executor") or profile.executor,
        "agent_run_id": item.get("agent_run_id"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "completed_at": item.get("completed_at"),
        "review_ready_at": item.get("review_ready_at"),
        "reviewed_at": item.get("reviewed_at"),
        "review_decision": item.get("review_decision"),
        "review_feedback": item.get("review_feedback"),
        "review_feedback_type": item.get("review_feedback_type"),
        "review_required": item.get("review_required") is True,
        "acceptance_supplements": item.get("acceptance_supplements") if isinstance(item.get("acceptance_supplements"), list) else [],
        "documentation_result": item.get("documentation_result") if isinstance(item.get("documentation_result"), dict) else {},
        "documentation_reviewed_at": item.get("documentation_reviewed_at"),
        "accepted_at": item.get("accepted_at"),
        "merged_at": item.get("merged_at"),
        "abandoned_at": item.get("abandoned_at"),
        "docs": docs,
        "blockers": [blocker for blocker in blockers if isinstance(blocker, dict)],
        "targets": item.get("targets") if isinstance(item.get("targets"), list) else [],
        "target_plan": target_plan_payload,
        "git": git_contract,
        "branch": item.get("branch") or item.get("feature_branch") or git_contract.get("feature_branch"),
        "feature_branch": item.get("feature_branch") or item.get("branch") or git_contract.get("feature_branch"),
        "branch_revision": item.get("branch_revision") or git_contract.get("branch_revision"),
        "worktree_path": item.get("worktree_path") or item.get("worktree"),
        "execution_root": item.get("execution_root"),
        "target_branch": item.get("target_branch") or git_contract.get("target_branch"),
        "merge_method": item.get("merge_method"),
        "merge_result": item.get("merge_result") if isinstance(item.get("merge_result"), dict) else None,
        "cleanup_result": item.get("cleanup_result") if isinstance(item.get("cleanup_result"), dict) else None,
        "cleanup_pending": item.get("cleanup_pending"),
        "abandon_requested_at": item.get("abandon_requested_at"),
        "abandoned_reason": item.get("abandoned_reason"),
        "discard_confirmed": item.get("discard_confirmed") is True,
        "last_error": item.get("last_error"),
        "required_action": item.get("required_action"),
        "blocker_reason": blocker_reason(item),
        "resolved_reason": item.get("resolved_reason"),
        "resolved_at": item.get("resolved_at"),
        "previous_blocked_status": item.get("previous_blocked_status"),
        "can_continue": status not in TERMINAL_TASK_STATES,
        "next_action": TASK_STATE_ACTIONS.get(status, "等待下一轮处理"),
        "timeline": timeline(status),
    }
    diagnostic = diagnostic_for_item(item, profile)
    if diagnostic.get("has_signal"):
        normalized["diagnostic"] = diagnostic
    if not _uses_v2_storage(profile) or not task_uses_document_contract(item):
        acceptance = item.get("acceptance") if isinstance(item.get("acceptance"), list) else []
        normalized["acceptance"] = [str(value) for value in acceptance]
        normalized["acceptance_criteria"] = [str(value) for value in acceptance]
        normalized["acceptance_refs"] = item.get("acceptance_refs") if isinstance(item.get("acceptance_refs"), list) else []
    return normalized


def _normalized_docs_for_item(item: Dict[str, Any]) -> Dict[str, Any]:
    raw_docs = item.get("docs")
    if not isinstance(raw_docs, dict):
        return {"module": "", "requirements": [], "design": [], "specs": []}
    requirements = _path_list(raw_docs.get("requirements")) or _path_list(raw_docs.get("prd"))
    design = _path_list(raw_docs.get("design"))
    specs = _path_list(raw_docs.get("specs")) or _path_list(raw_docs.get("spec"))
    module = str(raw_docs.get("module") or "").strip()
    if not module:
        module = _module_from_doc_paths(requirements + design + specs)
    normalized = dict(raw_docs)
    normalized.update(
        {
            "module": module,
            "requirements": requirements,
            "design": design,
            "specs": specs,
        }
    )
    return normalized


def _path_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _module_from_doc_paths(paths: List[str]) -> str:
    for value in paths:
        path = Path(value)
        if len(path.parts) < 3 or path.parts[0] != "docs":
            continue
        if path.name in {"requirements.md", "design.md", "specs.md"}:
            return Path(*path.parts[1:-1]).as_posix()
        if len(path.parts) >= 4 and path.parts[1] in {"prd", "design", "spec"}:
            legacy_module = Path(*path.parts[2:]).with_suffix("")
            return legacy_module.as_posix()
    return ""


def timeline(current_status: str) -> List[Dict[str, Any]]:
    order = ["open", "claimed", "prd_ready", "coding", "completed"]
    if current_status in {"prd_blocked", "spec_blocked", "blocked", "dev_blocked", "abandoned"}:
        order = ["open", "claimed", current_status]
    elif current_status in {"spec_ready", "ready_for_review", "accepted", "merged"}:
        order = ["open", "spec_ready", "coding", "ready_for_review", "accepted", "merged", "completed"]
    current_index = order.index(current_status) if current_status in order else 0
    rows: List[Dict[str, Any]] = []
    for index, status in enumerate(order):
        phase = "done" if index < current_index else "current" if index == current_index else "waiting"
        rows.append(
            {
                "state": status,
                "label": status_label(status),
                "phase": phase,
                "action": TASK_STATE_ACTIONS.get(status, ""),
                "order": index + 1,
            }
        )
    return rows


def active_items(payload: Dict[str, Any], profile: ProjectProfile) -> List[Dict[str, Any]]:
    return [
        normalize_item(item, profile)
        for item in payload.get("items", [])
        if isinstance(item, dict) and item.get("status") not in TERMINAL_TASK_STATES
    ]


def choose_item(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    items = [
        item
        for item in payload.get("items", [])
        if isinstance(item, dict) and not item.get("cleanup_pending")
    ]
    for item in items:
        status = item.get("status")
        if item.get("agent_status") == "running" and status not in TERMINAL_TASK_STATES:
            return item
    for item in items:
        if item.get("status") not in TERMINAL_TASK_STATES:
            return item
    return None


def choose_item_for_loop(payload: Dict[str, Any], loop_type: str) -> Optional[Dict[str, Any]]:
    if loop_type == "dev":
        return _choose_item_in_states(payload, DEV_TASK_STATES)
    return choose_item(payload)


def _choose_item_in_states(payload: Dict[str, Any], allowed_states: set[str]) -> Optional[Dict[str, Any]]:
    items = [
        item
        for item in payload.get("items", [])
        if isinstance(item, dict) and not item.get("cleanup_pending")
    ]
    for item in items:
        status = str(item.get("status") or "")
        if item.get("agent_status") == "running" and status in allowed_states:
            return item
    for item in items:
        if str(item.get("status") or "") in allowed_states:
            return item
    return None


def summary(payload: Dict[str, Any], profile: ProjectProfile) -> Dict[str, Any]:
    items = [item for item in payload.get("items", []) if isinstance(item, dict)]
    active = active_items(payload, profile)
    chosen = choose_item(payload)
    normalized_chosen = normalize_item(chosen, profile) if chosen else None
    blocked = normalized_chosen and normalized_chosen["status"] in {"prd_blocked", "spec_blocked", "blocked", "dev_blocked"}
    running = normalized_chosen and normalized_chosen.get("agent_status") == "running"
    runnable_loops: List[str] = []
    if choose_item_for_loop(payload, "dev") is not None:
        runnable_loops.append("dev")
    project_status = "completed"
    if running:
        project_status = "running"
    elif blocked:
        project_status = "blocked"
    elif active:
        project_status = "idle"
    result = {
        "project_status": project_status,
        "active_task": normalized_chosen,
        "has_open_backlog": bool(active),
        "runnable_loops": runnable_loops,
        "backlog": {
            "total": len(items),
            "active": len(active),
            "completed": sum(1 for item in items if item.get("status") in TERMINAL_TASK_STATES),
        },
        "capabilities": {
            "task_add": True,
            "task_ai_create": True,
            "task_reorder": payload.get("version") != 2,
        },
    }
    if blocked and not running and normalized_chosen:
        reason = normalized_chosen.get("blocker_reason") or "任务已进入阻塞状态，但 worker 没有写明具体原因；请查看 last-message.md 或项目文档。"
        result["summary"] = f"任务处于{status_label(normalized_chosen['status'])}"
        result["required_action"] = reason
    return result


def add_task(
    profile: ProjectProfile,
    title: str,
    description: str = "",
    source: str = "manual",
    acceptance: Optional[List[str]] = None,
    targets: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    acceptance_items = acceptance or ["任务 item 已进入 backlog", "下一轮调度可领取该任务", "处理结果会写入运行历史"]
    target_items = targets if isinstance(targets, list) else []
    if _uses_v2_storage(profile):
        reservation = reserve_task(profile, {"title": title, "source": source, "targets": target_items})
        published = publish_new_task(
            profile,
            reservation.reservation_id,
            {
                "title": title,
                "description": description or "由 LoopForge 创建的任务",
                "source": source,
                "planning_level": "complex",
                "acceptance": acceptance_items,
                "acceptance_refs": [],
                "docs": {},
                "targets": target_items,
            },
        )
        return normalize_item(published["task"], profile)
    payload = load_dev_tasks(profile)
    now = utc_now()
    task = {
        "id": f"task-{uuid.uuid4().hex[:10]}",
        "title": title,
        "description": description,
        "status": "open",
        "priority": DEFAULT_PRIORITY,
        "agent": profile.default_agent,
        "source": source,
        "acceptance": acceptance_items,
        "targets": target_items,
        "created_at": now,
        "updated_at": now,
    }
    payload.setdefault("items", []).append(task)
    save_dev_tasks(profile, payload)
    return normalize_item(task, profile)


def add_structured_task(profile: ProjectProfile, fields: Dict[str, Any]) -> Dict[str, Any]:
    """写入已经过严格入口校验的结构化 LoopForge 任务。"""

    if _uses_v2_storage(profile):
        reservation = reserve_task(
            profile,
            {
                "title": str(fields.get("title") or ""),
                "source": str(fields.get("source") or "loopforge-grill-me"),
                "targets": fields.get("targets") if isinstance(fields.get("targets"), list) else [],
            },
        )
        published = publish_new_task(profile, reservation.reservation_id, fields)
        return normalize_item(published["task"], profile)
    payload = load_dev_tasks(profile)
    now = utc_now()
    task = dict(fields)
    task.pop("intake", None)
    task.pop("documentation_mode", None)
    task.update(
        {
            "id": f"task-{uuid.uuid4().hex[:10]}",
            "status": "open",
            "priority": DEFAULT_PRIORITY,
            "agent": profile.default_agent,
            "source": "loopforge-grill-me",
            "created_at": now,
            "updated_at": now,
        }
    )
    payload.setdefault("items", []).append(task)
    save_dev_tasks(profile, payload)
    return normalize_item(task, profile)


def ai_create_task(profile: ProjectProfile, prompt: str) -> Dict[str, Any]:
    title = prompt if len(prompt) <= 36 else prompt[:36].rstrip() + "..."
    acceptance = [
        "需求已转为可调度 item",
        "执行后可在运行历史中看到结果",
        "完成后不再出现在任务管理非终态列表",
    ]
    return add_task(profile, title, f"基于一句话需求生成：{prompt}", "ai", acceptance)


def resolve_blocked_item(profile: ProjectProfile, reason: str = "") -> Optional[Dict[str, Any]]:
    payload = load_dev_tasks(profile)
    item = choose_item(payload)
    if not item or item.get("status") not in {"prd_blocked", "spec_blocked", "blocked", "dev_blocked"}:
        return None

    previous_status = str(item.get("status") or "")
    if previous_status == "spec_blocked":
        next_status = "spec_ready"
    elif previous_status == "prd_blocked":
        next_status = "prd_ready"
    else:
        next_status = "coding"
    clean_reason = reason.strip() or "人工确认阻塞已处理"
    clear_blocker_fields(item)
    item["status"] = next_status
    item["resolved_reason"] = clean_reason
    item["resolved_at"] = utc_now()
    item["previous_blocked_status"] = previous_status
    item["updated_at"] = utc_now()
    save_dev_tasks(profile, payload)
    return normalize_item(item, profile)


def update_item(
    profile: ProjectProfile,
    item_id: str,
    remove_fields: Optional[set[str]] = None,
    **fields: Any,
) -> Dict[str, Any]:
    removals = {str(field) for field in (remove_fields or set())}
    if _uses_v2_storage(profile):
        before = load_task(profile, item_id)
        persisted_fields = _v2_persisted_fields(before, fields)
        _refresh_single_repo_feature_revision(profile, before, persisted_fields)
        next_status = str(persisted_fields.get("status") or before["status"])
        next_git = persisted_fields.get("git")
        if isinstance(next_git, dict) and next_git.get("branch_revision") != before.get("git", {}).get("branch_revision"):
            push_managed_feature_revision(profile, next_git, str(next_git.get("branch_revision") or ""))
        if next_status in TERMINAL_TASK_STATES:
            change = archive_task_file(
                profile,
                item_id,
                expected_version=int(before["version"]),
                allowed_states={str(before["status"])},
                terminal_status=next_status,
                fields={key: value for key, value in persisted_fields.items() if key != "status"},
                remove_fields=removals,
            )
            published = publish_main_change(profile, change, f"loopforge: 更新任务 {item_id} 为 {next_status}")
        else:
            published, change = publish_task_mutation_from_isolated_main(
                profile,
                item_id,
                expected_version=int(before["version"]),
                allowed_states={str(before["status"])},
                fields=persisted_fields,
                message=f"loopforge: 更新任务 {item_id} 为 {next_status}",
                remove_fields=removals,
            )
        normalized = normalize_item(change.after or before, profile)
        if published.status == "sync_blocked":
            normalized["sync_blocked"] = published.blocker
        return normalized
    payload = load_dev_tasks(profile)
    for item in payload.get("items", []):
        if isinstance(item, dict) and item.get("id") == item_id:
            for field in removals:
                item.pop(field, None)
            item.update(fields)
            item["updated_at"] = utc_now()
            if item.get("status") in TERMINAL_TASK_STATES:
                archived_item = archive_terminal_item(profile, payload, item)
                return normalize_item(archived_item, profile)
            save_dev_tasks(profile, payload)
            return normalize_item(item, profile)
    raise KeyError(f"未知任务 item：{item_id}")


def uses_v2_storage(profile: ProjectProfile) -> bool:
    return not dev_task_path(profile).exists() and (profile.root_dir / "data" / "tasks").is_dir()


def _uses_v2_storage(profile: ProjectProfile) -> bool:
    return uses_v2_storage(profile)


def _v2_persisted_fields(before: Dict[str, Any], fields: Dict[str, Any]) -> Dict[str, Any]:
    persisted = dict(fields)
    for transient in ("worktree", "worktree_path", "execution_root", "ownership_marker", "target_plan"):
        persisted.pop(transient, None)
    git = dict(before.get("git") or {})
    git_changed = False
    for legacy_key, git_key in (
        ("branch", "feature_branch"),
        ("feature_branch", "feature_branch"),
        ("target_branch", "target_branch"),
        ("branch_revision", "branch_revision"),
        ("base_revision", "base_revision"),
    ):
        if legacy_key in persisted:
            git[git_key] = persisted.pop(legacy_key)
            git_changed = True
    if git_changed:
        persisted["git"] = git
    return persisted


def _refresh_single_repo_feature_revision(
    profile: ProjectProfile,
    before: Dict[str, Any],
    persisted_fields: Dict[str, Any],
) -> None:
    if before.get("targets"):
        return
    git = dict(persisted_fields.get("git") or before.get("git") or {})
    branch = str(git.get("feature_branch") or "")
    if not branch:
        return
    result = subprocess.run(
        ["git", "-C", str(profile.root_dir), "rev-parse", "--verify", f"refs/heads/{branch}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return
    revision = result.stdout.strip()
    if revision == git.get("branch_revision"):
        return
    binding = {**git, "repo_path": str(profile.root_dir)}
    push_managed_feature_revision(profile, binding, revision)
    git["branch_revision"] = revision
    persisted_fields["git"] = git


def _save_v2_payload(profile: ProjectProfile, payload: Dict[str, Any]) -> None:
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("v2 task payload items 必须是数组")
    current = {str(item["id"]): item for item in load_active_tasks(profile)}
    desired = {str(item.get("id") or ""): item for item in items if isinstance(item, dict)}
    if set(current) != set(desired):
        raise ValueError("v2 save 不允许隐式新增或删除任务，请使用 publisher 或 archive")
    for task_id, before in current.items():
        after = desired[task_id]
        comparison_before = {key: value for key, value in before.items() if key not in {"updated_at"}}
        comparison_after = {key: value for key, value in after.items() if key not in {"updated_at"}}
        if comparison_before == comparison_after:
            continue
        fields = {
            key: copy_value
            for key, copy_value in after.items()
            if key not in {"schema_version", "id", "version", "created_at", "updated_at"}
            and before.get(key) != copy_value
        }
        remove_fields = {
            key
            for key in before
            if key not in after and key not in {"schema_version", "id", "version", "created_at", "updated_at"}
        }
        updated = update_item(profile, task_id, remove_fields=remove_fields, **fields)
        after.clear()
        after.update(load_task(profile, task_id) if updated.get("status") not in TERMINAL_TASK_STATES else updated)
