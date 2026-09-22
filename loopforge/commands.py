"""CLI 用例层。"""

from __future__ import annotations

import copy
import getpass
import hashlib
import html
import re
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .cancel import write_cancel_request
from .clues import clue_counts, create_or_update_clue, decide_clue, list_clues
from .config import (
    CLAUDE_PERMISSION_MODES,
    CODEX_REASONING_EFFORTS,
    CODEX_SANDBOXES,
    DEFAULT_CLAUDE_MODEL,
    DEFAULT_CLAUDE_PERMISSION_MODE,
    DEFAULT_CODEX_MODEL,
    DEFAULT_CODEX_REASONING_EFFORT,
    DEFAULT_CODEX_SANDBOX,
    WORKER_PROVIDER_EXECUTORS,
    ProjectProfile,
    append_project_config,
    load_projects,
    normalize_worker_network,
    profile_to_dict,
    remove_project_config,
    update_project_config,
)
from .dev_tasks import (
    TASK_STATE_LABELS,
    USER_WAITING_TASK_STATES,
    active_items,
    add_structured_task,
    add_task,
    ai_create_task,
    choose_item,
    choose_item_for_loop,
    clear_blocker_fields,
    diagnostic_for_item,
    ensure_dev_task_file,
    load_dev_tasks,
    normalize_item,
    refresh_satisfied_blocker,
    resolve_blocked_item,
    save_dev_tasks,
    summary as dev_task_summary,
    update_item,
)
from .document_phases import DocumentPhaseError, ordered_document_phases
from .domain import AUTOMATION_MODES, TASK_STATES, result, utc_now
from .domain import NOTIFICATION_CHANNELS
from .executor import merge_result_artifact, preview as executor_preview, run_executor
from .global_config import read_global_config, update_wecom_config
from .history import append_jsonl, count_consecutive_timeouts, read_jsonl
from .lock import is_lock_active, lock_heartbeat, lock_record, project_lock, read_lock, stale_lock_reason
from .notifications import send_run_notification, send_test_notification
from .onboarding import onboarding_doctor
from .planning import ensure_planning_workspace, planning_gate
from .project_paths import ProjectPathError, resolve_project_path
from .run_dispatch import active_dispatch_status
from .scan import collect_scan_signals, scan_state_path
from .scheduler import choose_project_loop, next_due_at
from .task_actions import abandon_task, cleanup_task, merge_task, review_task
from .targets import TargetPlanError, task_target_plan
from .task_git import clear_sync_blocker, TaskGitError, publish_task_mutation_from_isolated_main, read_sync_blocker
from .task_contract import task_uses_document_contract
from .task_publisher import (
    TaskPublisherError,
    cancel_reservation,
    load_published_reservation_for_task,
    load_reservation,
    migrate_reservation_feature_branches,
    publish_new_task,
    reserve_task,
)
from .task_store import TaskStoreError, load_history_tasks, load_task
from .token_usage import build_task_usage, task_associated_project_ids
from .worker_result import load_worker_result, merge_target_results, worker_result_decision
from .worker_adapters import request_metadata
from .worktrees import WorktreePreparationError, prepare_task_worktrees


RUN_START_CLEARED_FIELDS = (
    "agent_exit_code",
    "last_run_summary",
    "last_message_path",
    "log_path",
)
LOOP_TYPES = {"scan", "dev"}


def _run_id() -> str:
    return uuid.uuid4().hex


def _normalize_loop_type(value: Any) -> str:
    loop_type = str(value or "dev").strip() or "dev"
    if loop_type not in LOOP_TYPES:
        raise ValueError(f"非法 loop_type：{loop_type}")
    return loop_type


def _choose_dev_item_with_legacy_fallback(payload: Dict[str, Any], *, allow_blocked: bool = False) -> Optional[Dict[str, Any]]:
    item = choose_item_for_loop(payload, "dev")
    if item is not None:
        return item
    legacy_item = choose_item(payload)
    if not legacy_item:
        return None
    legacy_status = str(legacy_item.get("status") or "")
    if legacy_status == "blocked":
        return legacy_item
    if legacy_status in USER_WAITING_TASK_STATES:
        return None
    if allow_blocked:
        return legacy_item
    return legacy_item


def _find_project(profiles: List[ProjectProfile], project_id: str) -> ProjectProfile:
    for profile in profiles:
        if profile.project_id == project_id:
            return profile
    raise KeyError(f"未知项目：{project_id}")


def _event_id() -> str:
    return uuid.uuid4().hex


def _write_event(profile: ProjectProfile, event_type: str, summary: str, **extra: Any) -> Dict[str, Any]:
    record = {
        "event_id": _event_id(),
        "project_id": profile.project_id,
        "event_type": event_type,
        "created_at": utc_now(),
        "actor": "loopforge",
        "summary": summary,
    }
    record.update(extra)
    append_jsonl(profile.events_path, record)
    return record


def _write_run(profile: ProjectProfile, record: Dict[str, Any]) -> Dict[str, Any]:
    append_jsonl(profile.history_path, record)
    return record


def _fresh_notification_profile(config_path: Optional[Path], profile: ProjectProfile) -> ProjectProfile:
    try:
        return _find_project(load_projects(config_path), profile.project_id)
    except Exception:
        return profile


def _send_run_notification(config_path: Optional[Path], profile: ProjectProfile, record: Dict[str, Any]) -> Dict[str, Any]:
    return send_run_notification(_fresh_notification_profile(config_path, profile), record)


def _scan_budget_alert_after(profile: ProjectProfile) -> int:
    scan_config = profile.global_config.get("scan") if isinstance(profile.global_config.get("scan"), dict) else {}
    try:
        value = int(scan_config.get("budget_exceeded_alert_after") or 2)
    except (TypeError, ValueError):
        return 2
    return value if value > 0 else 2


def _consecutive_scan_budget_exceeded(records: List[Dict[str, Any]]) -> int:
    count = 0
    for record in reversed(records):
        if record.get("loop_type") != "scan":
            continue
        if record.get("outcome") == "scan_budget_exceeded":
            count += 1
            continue
        break
    return count


def _task_state_label(state: Any) -> str:
    return TASK_STATE_LABELS.get(str(state), str(state))


def _target_plan_blocker(error: TargetPlanError) -> Dict[str, Any]:
    return {
        "id": error.code,
        "code": error.code,
        "type": error.code,
        "message": error.summary,
        "summary": error.summary,
        "resolution": "请修正父 task 的 targets 配置或 project_group.children 后，再重新运行开发执行。",
        "detail": error.detail,
    }


def _clean_acceptance_list(value: Any) -> List[str]:
    """把验收标准归一为非空字符串列表；非法输入按空处理。"""

    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if isinstance(item, str) and str(item).strip()]


def _clean_target_list(profile: ProjectProfile, value: Any) -> List[Dict[str, Any]]:
    """用 targets.py 归一显式 targets；无 targets 时返回空列表走单仓默认路径。"""

    if not isinstance(value, list) or not value:
        return []
    plan = task_target_plan(profile, {"targets": value})
    return [target.to_payload() for target in plan.targets]


def list_projects(config_path: Optional[Path]) -> Dict[str, Any]:
    try:
        profiles = load_projects(config_path)
    except FileNotFoundError:
        profiles = []
    return result("completed", "已读取项目配置", projects=[profile_to_dict(p) for p in profiles])


def validate_config(config_path: Optional[Path]) -> Dict[str, Any]:
    profiles = load_projects(config_path)
    invalid = [profile_to_dict(profile) for profile in profiles if not profile.is_valid]
    if invalid:
        return result(
            "misconfigured",
            f"配置存在 {len(invalid)} 个错误项目",
            valid=False,
            projects=[profile_to_dict(profile) for profile in profiles],
            invalid_projects=invalid,
        )
    return result("completed", "配置校验通过", valid=True, projects=[profile_to_dict(profile) for profile in profiles])


def project_doctor(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    return onboarding_doctor(profile)


def project_import(config_path: Optional[Path], root_dir: str, project_id: str = "", name: str = "") -> Dict[str, Any]:
    root_value = root_dir.strip()
    if not root_value:
        return result("failed", "项目路径不能为空")

    root = Path(root_value).expanduser()
    if not root.exists() or not root.is_dir():
        return result("failed", f"项目目录不存在：{root}")

    resolved_root = root.resolve()
    project_name = name.strip() or resolved_root.name
    try:
        dev_task_initialized = ensure_dev_task_file(resolved_root, project_name)
    except Exception as exc:
        return result("misconfigured", f"data/dev-task.json 格式错误：{exc}")

    try:
        profiles = load_projects(config_path)
    except FileNotFoundError:
        profiles = []
    clean_id = _slug(project_id or resolved_root.name)
    existing_ids = {profile.project_id for profile in profiles}
    if clean_id in existing_ids:
        suffix = 2
        candidate = f"{clean_id}-{suffix}"
        while candidate in existing_ids:
            suffix += 1
            candidate = f"{clean_id}-{suffix}"
        clean_id = candidate

    for profile in profiles:
        try:
            if profile.root_dir.resolve() == resolved_root:
                return result("failed", f"项目已导入：{profile.name}", project=profile_to_dict(profile))
        except OSError:
            continue

    entry = {
        "id": clean_id,
        "name": project_name,
        "root_dir": str(resolved_root),
        "enabled": True,
        "schedule_enabled": False,
        "schedule_frequency": "hourly",
        "automation_mode": "off",
        "priority": 100,
        "default_agent": "codex",
        "executor": "codex_cli",
        "codex_model": DEFAULT_CODEX_MODEL,
        "codex_reasoning_effort": DEFAULT_CODEX_REASONING_EFFORT,
        "codex_sandbox": DEFAULT_CODEX_SANDBOX,
        "auto_commit": True,
        "notification_channel": "none",
        "state_dir": str(resolved_root / ".loopforge"),
        "report_dir": str(resolved_root / ".loopforge" / "reports"),
    }
    append_project_config(entry, config_path)

    imported = _find_project(load_projects(config_path), clean_id)
    task_payload = project_tasks(config_path, clean_id)
    event = _write_event(imported, "project_imported", f"导入项目：{imported.name}", dev_task_initialized=dev_task_initialized)
    return result(
        "completed",
        f"已导入项目：{imported.name}",
        project=profile_to_dict(imported),
        tasks=task_payload.get("tasks") or [],
        task_result=task_payload,
        event=event,
        dev_task_initialized=dev_task_initialized,
    )


def project_remove(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    removed = remove_project_config(project_id, config_path)
    return result(
        "completed",
        f"已从 LoopForge 移除项目：{profile.name}",
        project=profile_to_dict(profile),
        removed_project=removed,
        deleted_files=False,
    )


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-_")
    return cleaned or "project"


def _project_payload(profile: ProjectProfile, status_payload: Optional[Dict[str, Any]] = None, latest_run: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = profile_to_dict(profile)
    schedule_status = dict(status_payload or {})
    schedule_status["last_run"] = latest_run
    payload["next_scheduled_at"] = next_due_at(profile, schedule_status)
    return payload


def _latest_run(profile: ProjectProfile) -> Optional[Dict[str, Any]]:
    try:
        runs = read_jsonl(profile.history_path, limit=1)
    except OSError:
        return None
    return runs[-1] if runs else None


def dashboard(config_path: Optional[Path]) -> Dict[str, Any]:
    profiles = load_projects(config_path)
    projects: List[Dict[str, Any]] = []
    issues: List[Dict[str, Any]] = []
    counts = {"blocked": 0, "timeout": 0, "running": 0, "idle": 0, "completed": 0, "misconfigured": 0}
    for profile in profiles:
        history_error = ""
        try:
            runs = read_jsonl(profile.history_path, limit=8)
        except OSError as exc:
            runs = []
            history_error = f"运行历史读取失败：{exc}"
        try:
            events = read_jsonl(profile.events_path, limit=8)
        except OSError as exc:
            events = []
            history_error = history_error or f"事件历史读取失败：{exc}"
        try:
            clues = list_clues(profile, limit=20)
        except OSError as exc:
            clues = []
            history_error = history_error or f"线索读取失败：{exc}"
        status_payload = _profile_status_payload(profile)
        project_status = status_payload.get("project_status") or status_payload.get("status") or "misconfigured"
        counts[project_status if project_status in counts else "idle"] = counts.get(project_status, 0) + 1
        latest_run = runs[-1] if runs else None
        project = _project_payload(profile, status_payload, latest_run)
        issue = _classify_issue(profile, status_payload, latest_run)
        if history_error:
            issue = {
                "project_id": profile.project_id,
                "name": profile.name,
                "kind": "history_unreadable",
                "summary": "运行历史读取失败",
                "required_action": history_error,
            }
        if issue:
            issues.append(issue)
        project.update(
            {
                "project_status": project_status,
                "project_status_label": _project_label(project_status),
                "status": status_payload,
                "runs": runs,
                "events": events,
                "clues": clues,
                "clue_counts": clue_counts(clues),
                "latest_run": latest_run,
                "issue": issue,
            }
        )
        projects.append(project)
    return result(
        "completed",
        "控制面状态已刷新",
        summary_counts=counts,
        generated_at=utc_now(),
        issues=issues,
        projects=projects,
    )


def _project_label(status: str) -> str:
    from .domain import PROJECT_STATUS_LABELS

    return PROJECT_STATUS_LABELS.get(status, status)


def _profile_status_payload(profile: ProjectProfile) -> Dict[str, Any]:
    if not profile.is_valid:
        return {
            "status": "misconfigured",
            "project_status": "misconfigured",
            "summary": "项目配置错误",
            "required_action": "；".join(profile.errors),
        }
    try:
        payload = load_dev_tasks(profile)
        task_summary = dev_task_summary(payload, profile)
    except Exception as exc:
        return _dev_task_error_payload(exc)
    dispatch = active_dispatch_status(profile)
    active_task = task_summary.get("active_task")
    sync_blocker = read_sync_blocker(profile)
    if isinstance(active_task, dict):
        active_task = {**active_task, **_task_runtime_marker(profile, active_task)}
        if sync_blocker and sync_blocker.get("task_id") == active_task.get("id"):
            active_task["sync_blocked"] = sync_blocker
        task_summary = {**task_summary, "active_task": active_task}
    if sync_blocker:
        recovery = active_task.get("result_recovery") if isinstance(active_task, dict) else None
        task_summary = {
            **task_summary,
            "project_status": "blocked",
            "sync_blocked": sync_blocker,
            "required_action": (
                "已有完成的执行结果，请点击“恢复结果”；不要重新运行任务。"
                if isinstance(recovery, dict) and recovery.get("auto_recoverable")
                else "任务 Git 发布发生冲突，请人工处理后清理 sync blocker。"
            ),
            "summary": (
                "执行结果待恢复"
                if isinstance(recovery, dict) and recovery.get("auto_recoverable")
                else "任务 Git 同步阻塞"
            ),
        }
    stale = None if dispatch["active"] else _stale_running_context(profile, task_summary.get("active_task"))
    if stale:
        active_task = dict(task_summary.get("active_task") or {})
        active_task.update(
            {
                "agent_status": "timeout",
                "next_action": "上次运行未正常收尾，可点击运行一轮恢复该任务。",
                "stale_run": stale,
            }
        )
        task_summary = {
            **task_summary,
            "project_status": "timeout",
            "active_task": active_task,
            "required_action": stale["required_action"],
            "summary": "检测到上次运行未正常收尾",
        }
    if dispatch["runtime_status"] != "none":
        task_summary = {**task_summary, "dispatch": dispatch}
    if dispatch["active"]:
        dispatch_summary = (
            "独立运行进程正在启动"
            if dispatch["runtime_status"] == "launching"
            else "独立运行进程正在后台执行"
        )
        task_summary = {
            **task_summary,
            "project_status": "running",
            "summary": dispatch_summary,
        }
    project_status = task_summary["project_status"]
    return {
        "schema_version": "1",
        "project_id": profile.project_id,
        "command": "status",
        "status": "completed" if project_status != "misconfigured" else "misconfigured",
        "project_status": project_status,
        "observed_at": utc_now(),
        "summary": "dev-task 状态已读取",
        **task_summary,
    }


def _dev_task_error_payload(exc: Exception) -> Dict[str, Any]:
    if isinstance(exc, PermissionError):
        return {
            "status": "failed",
            "project_status": "failed",
            "summary": "任务存储读取失败：系统权限不足",
            "required_action": (
                f"{exc}。请在 macOS 系统设置 -> 隐私与安全性 -> 完全磁盘访问权限中，"
                "给启动 LoopForge 的应用或终端授权；如果通过 Codex 启动，请给 Codex 授权，"
                "如果通过 Terminal/iTerm 启动，请给对应终端授权，然后重启 LoopForge 后台。"
            ),
            "active_task": None,
            "has_open_backlog": False,
        }
    if isinstance(exc, OSError):
        return {
            "status": "failed",
            "project_status": "failed",
            "summary": "任务存储读取失败",
            "required_action": str(exc),
            "active_task": None,
            "has_open_backlog": False,
        }
    return {
        "status": "misconfigured",
        "project_status": "misconfigured",
        "summary": "任务存储格式错误",
        "required_action": str(exc),
        "active_task": None,
        "has_open_backlog": False,
    }


def _stale_running_task_from_disk(profile: ProjectProfile) -> Optional[Dict[str, Any]]:
    try:
        payload = load_dev_tasks(profile)
        item = choose_item(payload)
    except Exception:
        return None
    if not item:
        return None
    return _stale_running_context(profile, normalize_item(item, profile))


def _task_runtime_marker(profile: ProjectProfile, task: Any) -> Dict[str, Any]:
    health: Dict[str, Any] = {
        "agent_status": task.get("agent_status") if isinstance(task, dict) else None,
        "agent_run_id": task.get("agent_run_id") if isinstance(task, dict) else None,
        "lock_present": False,
        "lock_invalid": False,
        "lock_active": False,
        "lock_run_id": None,
        "lock_pid": None,
        "lock_started_at": None,
        "lock_expires_at": None,
        "matched_agent_run_id": False,
        "stale_reason": "",
    }
    marker = {
        "runtime_status": "not_running",
        "runtime_status_label": "未运行",
        "runtime_health": health,
    }
    if not isinstance(task, dict):
        return marker
    if task.get("agent_status") != "running":
        return marker
    run_id = str(task.get("agent_run_id") or "")
    if not run_id:
        return {
            **marker,
            "runtime_status": "unknown",
            "runtime_status_label": "运行态缺少 run_id",
            "runtime_health": {**health, "stale_reason": "任务标记为运行中，但缺少 agent_run_id"},
        }
    lock = read_lock(profile.lock_path)
    if lock:
        health.update(
            {
                "lock_present": True,
                "lock_invalid": bool(lock.get("invalid")),
                "lock_run_id": lock.get("run_id"),
                "lock_pid": lock.get("pid"),
                "lock_started_at": lock.get("started_at"),
                "lock_expires_at": lock.get("expires_at"),
                "matched_agent_run_id": lock.get("run_id") == run_id,
            }
        )
    if not lock:
        return {
            **marker,
            "runtime_status": "stale",
            "runtime_status_label": "运行态陈旧",
            "runtime_health": {**health, "stale_reason": "运行锁不存在"},
        }
    if lock.get("invalid"):
        return {
            **marker,
            "runtime_status": "stale",
            "runtime_status_label": "运行态陈旧",
            "runtime_health": {**health, "stale_reason": "运行锁文件无效"},
        }
    if lock.get("run_id") != run_id:
        reason = stale_lock_reason(lock)
        status = "stale" if reason else "unknown"
        label = "运行态陈旧" if reason else "运行锁不匹配"
        return {
            **marker,
            "runtime_status": status,
            "runtime_status_label": label,
            "runtime_health": {
                **health,
                "stale_reason": f"运行锁不匹配：任务 run_id={run_id}，锁 run_id={lock.get('run_id')}",
            },
        }
    reason = stale_lock_reason(lock)
    if reason:
        return {
            **marker,
            "runtime_status": "stale",
            "runtime_status_label": "运行态陈旧",
            "runtime_health": {**health, "stale_reason": reason},
        }
    return {
        **marker,
        "runtime_status": "running",
        "runtime_status_label": "实际运行中",
        "runtime_health": {**health, "lock_active": True},
    }


def _stale_running_context(profile: ProjectProfile, task: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(task, dict) or task.get("agent_status") != "running":
        return None
    run_id = str(task.get("agent_run_id") or "")
    if not run_id:
        return None
    marker = _task_runtime_marker(profile, task)
    if marker.get("runtime_status") != "stale":
        return None
    health = marker.get("runtime_health") or {}
    reason = str(health.get("stale_reason") or "运行态陈旧")
    lock = read_lock(profile.lock_path) or {}
    if lock.get("run_id") not in {None, run_id} and not stale_lock_reason(lock):
        return None
    if not reason:
        return None
    started_at = lock.get("started_at")
    expires_at = lock.get("expires_at")
    return {
        "run_id": run_id,
        "task_id": task.get("id"),
        "reason": reason,
        "started_at": started_at,
        "expires_at": expires_at,
        "pid": lock.get("pid"),
        "required_action": (
            f"上次运行 {run_id} 未正常收尾：{reason}。"
            "LoopForge 已将它视为陈旧运行态；点击运行一轮会继续接管该任务。"
        ),
    }


def _matching_stale_task(stale: Optional[Dict[str, Any]], item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not stale:
        return None
    if stale.get("task_id") != item.get("id"):
        return None
    if stale.get("run_id") != item.get("agent_run_id"):
        return None
    return stale


def _can_continue_dirty_non_terminal(
    latest_run: Optional[Dict[str, Any]],
    item: Dict[str, Any],
    git_status: Dict[str, Any],
) -> bool:
    if git_status.get("status") != "dirty_before_run":
        return False
    if not latest_run or latest_run.get("blocker_type") not in {"non_terminal_task", "prd_blocked", "blocked"}:
        return False
    if latest_run.get("task_id") != item.get("id"):
        return False
    return str(item.get("status") or "") not in {"open", "completed", "abandoned"}


def _can_continue_dirty_resolved_task(
    profile: ProjectProfile,
    item: Dict[str, Any],
    git_status: Dict[str, Any],
) -> bool:
    if git_status.get("status") != "dirty_before_run":
        return False
    if not str(item.get("resolved_reason") or "").strip():
        return False
    if str(item.get("status") or "") in {"open", "completed", "abandoned"}:
        return False
    return _is_current_task_dirty(profile, item, str(git_status.get("porcelain") or ""))


def _can_continue_dirty_executor_failure(
    profile: ProjectProfile,
    item: Dict[str, Any],
    git_status: Dict[str, Any],
) -> bool:
    if git_status.get("status") != "dirty_before_run":
        return False
    if str(item.get("status") or "") in {"open", "completed", "abandoned"}:
        return False
    if str(item.get("agent_status") or "") != "failed":
        return False
    if not bool(item.get("last_error") or item.get("last_run_summary") or item.get("agent_exit_code")):
        return False
    porcelain = str(git_status.get("porcelain") or "")
    return _is_only_dev_task_dirty(profile, porcelain) or _is_current_task_dirty(profile, item, porcelain)


def _classify_issue(profile: ProjectProfile, status_payload: Dict[str, Any], latest_run: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    project_status = status_payload.get("project_status") or status_payload.get("status")
    run_status = latest_run.get("status") if latest_run else None
    active_task = status_payload.get("active_task") if isinstance(status_payload.get("active_task"), dict) else None
    current_status_issue = project_status in {"blocked", "misconfigured", "timeout", "failed"}
    run_status_issue = False
    if run_status in {"timeout_blocked", "failed", "misconfigured"} and active_task and latest_run:
        latest_task_id = latest_run.get("task_id")
        run_status_issue = (
            bool(latest_task_id)
            and latest_task_id == active_task.get("id")
            and active_task.get("agent_status") != "running"
            and project_status not in {"completed", "running"}
        )
    if current_status_issue or run_status_issue:
        summary = (latest_run or {}).get("summary") if run_status_issue else None
        diagnostic = None
        if active_task and isinstance(active_task.get("diagnostic"), dict):
            diagnostic = active_task["diagnostic"]
        elif latest_run and isinstance(latest_run.get("diagnostic"), dict):
            diagnostic = latest_run["diagnostic"]
        return {
            "project_id": profile.project_id,
            "name": profile.name,
            "kind": run_status if run_status_issue else project_status,
            "summary": summary or status_payload.get("summary") or (latest_run or {}).get("summary") or "需要人工处理",
            "required_action": status_payload.get("required_action") or (latest_run or {}).get("required_action") or "",
            "diagnostic": diagnostic,
        }
    notification = (latest_run or {}).get("notification") or {}
    if notification.get("status") == "failed":
        return {
            "project_id": profile.project_id,
            "name": profile.name,
            "kind": "notification_failed",
            "summary": "通知发送失败",
            "required_action": notification.get("error") or "",
        }
    return None


def _project_scan_once(config_path: Optional[Path], project_id: str, trigger: str = "manual") -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    run_id = _run_id()
    if not profile.is_valid:
        now = utc_now()
        record = {
            "run_id": run_id,
            "project_id": profile.project_id,
            "trigger": trigger,
            "loop_type": "scan",
            "status": "misconfigured",
            "started_at": now,
            "ended_at": now,
            "duration_seconds": 0,
            "summary": "项目配置错误，未执行项目巡检。",
            "required_action": "；".join(profile.errors),
            "notification": {"status": "skipped", "reason": "项目配置错误，未触发通知"},
        }
        return result("misconfigured", record["summary"], run=_write_run(profile, record))

    with project_lock(profile, run_id, "scan") as conflict:
        if conflict is not None:
            record = lock_record(profile, run_id, trigger, "scan")
            record["notification"] = {"status": "skipped", "reason": "已有运行中实例，未触发通知"}
            return result("skipped_already_running", record["summary"], run=_write_run(profile, record), lock=conflict)
        started_at = utc_now()
        created_ids: List[str] = []
        updated_ids: List[str] = []
        scan_result = collect_scan_signals(profile, run_id)
        for signal in scan_result.signals:
            clue, created = create_or_update_clue(profile, signal)
            target = created_ids if created else updated_ids
            target.append(str(clue.get("id") or ""))
            _write_event(
                profile,
                "clue_created" if created else "clue_seen",
                clue.get("summary") or "项目巡检线索已更新",
                clue_id=clue.get("id"),
                clue_type=clue.get("type"),
                dedupe_key=clue.get("dedupe_key"),
            )
        ended_at = utc_now()
        clue_ids = [item for item in created_ids + updated_ids if item]
        if scan_result.scan_status == "incomplete":
            outcome = "scan_budget_exceeded"
        else:
            outcome = "clue_created" if created_ids else "task_updated" if updated_ids else "no_op"
        summary = f"项目巡检完成，发现 {len(created_ids)} 条新线索，更新 {len(updated_ids)} 条线索。"
        if scan_result.scan_status == "incomplete":
            summary = (
                f"项目巡检已保存游标，本轮扫描 {scan_result.files_scanned}/"
                f"{scan_result.files_total} 个文件，下一轮继续。"
            )
        if not clue_ids:
            summary = "项目巡检完成，未发现新的文档/规格缺口。"
        if scan_result.scan_status == "incomplete" and not clue_ids:
            summary = (
                f"项目巡检已保存游标，本轮扫描 {scan_result.files_scanned}/"
                f"{scan_result.files_total} 个文件，暂未发现新线索。"
            )
        previous_scan_budget_count = _consecutive_scan_budget_exceeded(read_jsonl(profile.history_path, limit=20))
        scan_budget_count = previous_scan_budget_count + 1 if outcome == "scan_budget_exceeded" else 0
        record = {
            "run_id": run_id,
            "project_id": profile.project_id,
            "trigger": trigger,
            "loop_type": "scan",
            "status": "completed",
            "outcome": outcome,
            "scan_scope": scan_result.scope,
            "scan_status": scan_result.scan_status,
            "scan_base_ref": scan_result.base_ref,
            "scan_head_ref": scan_result.head_ref,
            "scan_budget_seconds": scan_result.budget_seconds,
            "scan_file_budget": scan_result.file_budget,
            "scan_state_path": str(scan_state_path(profile)),
            "files_scanned": scan_result.files_scanned,
            "files_total": scan_result.files_total,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": 0,
            "summary": summary,
            "clue_ids": clue_ids,
            "clues_created": len(created_ids),
            "clues_updated": len(updated_ids),
            "scan_budget_exceeded_count": scan_budget_count,
        }
        if outcome == "scan_budget_exceeded" and scan_budget_count >= _scan_budget_alert_after(profile):
            record["notification_event"] = "scan_budget_exceeded_repeated"
            record["required_action"] = "项目巡检连续超出预算，请提高扫描预算或缩小巡检范围。"
            record["notification"] = _send_run_notification(config_path, profile, record)
        else:
            record["notification"] = {"status": "skipped", "reason": "项目巡检未达到通知门槛"}
        written = _write_run(profile, record)
        event = _write_event(profile, "scan_completed", summary, run_id=run_id, outcome=outcome, clue_ids=clue_ids)
        clues = list_clues(profile, limit=50)
        return result("completed", summary, run=written, event=event, clues=clues, clue_counts=clue_counts(clues))


def _blocker_summary(blockers: List[Dict[str, Any]]) -> str:
    summaries = [str(blocker.get("summary") or "").strip() for blocker in blockers if isinstance(blocker, dict)]
    summaries = [summary for summary in summaries if summary]
    return "；".join(summaries) or "requirements/design/specs 未满足可开发门禁。"


def project_status(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    payload = _profile_status_payload(profile)
    return result(payload.get("status", "completed"), payload.get("summary", ""), project=profile_to_dict(profile), runner=payload)


def project_run_once(
    config_path: Optional[Path],
    project_id: str,
    trigger: str = "manual",
    loop_type: str = "dev",
    run_instruction: str = "",
) -> Dict[str, Any]:
    try:
        clean_loop_type = _normalize_loop_type(loop_type)
    except ValueError as exc:
        return result("failed", str(exc))
    if clean_loop_type == "scan":
        return _project_scan_once(config_path, project_id, trigger)
    profile = _find_project(load_projects(config_path), project_id)
    run_id = _run_id()
    stale_before_lock = _stale_running_task_from_disk(profile)
    latest_before = _latest_run(profile)
    if not profile.is_valid:
        now = utc_now()
        record = {
            "run_id": run_id,
            "project_id": profile.project_id,
            "trigger": trigger,
            "loop_type": "dev",
            "status": "misconfigured",
            "started_at": now,
            "ended_at": now,
            "duration_seconds": 0,
            "summary": "项目配置错误，未调用 executor。",
            "required_action": "；".join(profile.errors),
            "notification": {"status": "skipped", "reason": "项目配置错误，未触发通知"},
        }
        return result("misconfigured", record["summary"], run=_write_run(profile, record))

    with project_lock(profile, run_id, "dev") as conflict:
        if conflict is not None:
            record = lock_record(profile, run_id, trigger, "dev")
            record["notification"] = {"status": "skipped", "reason": "已有运行中实例，未触发通知"}
            return result("skipped_already_running", record["summary"], run=_write_run(profile, record), lock=conflict)
        try:
            payload = load_dev_tasks(profile)
            item = _choose_dev_item_with_legacy_fallback(payload, allow_blocked=trigger in {"resume_once", "resolve_and_resume"})
        except Exception as exc:
            now = utc_now()
            error_payload = _dev_task_error_payload(exc)
            record = {
                "run_id": run_id,
                "project_id": profile.project_id,
                "trigger": trigger,
                "loop_type": "dev",
                "status": error_payload["status"],
                "started_at": now,
                "ended_at": now,
                "duration_seconds": 0,
                "summary": error_payload["summary"],
                "required_action": error_payload["required_action"],
            }
            record["notification"] = _send_run_notification(config_path, profile, record)
            return result(record["status"], record["summary"], run=_write_run(profile, record))
        if item is None:
            now = utc_now()
            record = {
                "run_id": run_id,
                "project_id": profile.project_id,
                "trigger": trigger,
                "loop_type": "dev",
                "status": "skipped_no_task",
                "started_at": now,
                "ended_at": now,
                "duration_seconds": 0,
                "summary": "没有可运行的 dev-task item",
            }
            record["notification"] = _send_run_notification(config_path, profile, record)
            return result("skipped_no_task", record["summary"], run=_write_run(profile, record))

        blocker_refreshed = refresh_satisfied_blocker(profile, payload, item)
        if blocker_refreshed:
            _write_event(
                profile,
                "blocker_auto_resolved",
                "结构化阻塞条件已满足，自动恢复任务。",
                task_id=item.get("id"),
                task_title=item.get("title"),
                task_state=item.get("status"),
            )
        if str(item.get("status") or "") in USER_WAITING_TASK_STATES and not (trigger in {"resume_once", "resolve_and_resume"}):
            now = utc_now()
            record = {
                "run_id": run_id,
                "project_id": profile.project_id,
                "trigger": trigger,
                "loop_type": "dev",
                "status": "skipped_no_task",
                "started_at": now,
                "ended_at": now,
                "duration_seconds": 0,
                "summary": "当前任务等待人工动作，开发执行未启动。",
                "task_id": item.get("id"),
                "task_title": item.get("title"),
                "task_state": item.get("status"),
                "notification": {"status": "skipped", "reason": "等待人工动作，未触发通知"},
            }
            return result("skipped_no_task", record["summary"], run=_write_run(profile, record))

        try:
            task_target_plan(profile, item, require_repo_paths=profile.worktree_managed)
        except TargetPlanError as exc:
            previous_state = str(item.get("status") or "")
            now = utc_now()
            blocker = _target_plan_blocker(exc)
            final_task = update_item(
                profile,
                str(item.get("id") or ""),
                status="dev_blocked",
                blockers=[blocker],
                blocker_reason=exc.summary,
                required_action=blocker["resolution"],
            )
            diagnostic = diagnostic_for_item(
                final_task,
                profile,
                kind=exc.code,
                summary=exc.summary,
                required_action=blocker["resolution"],
            )
            record = {
                "run_id": run_id,
                "project_id": profile.project_id,
                "trigger": trigger,
                "loop_type": "dev",
                "status": "blocked",
                "started_at": now,
                "ended_at": now,
                "duration_seconds": 0,
                "summary": exc.summary,
                "required_action": blocker["resolution"],
                "task_id": final_task.get("id"),
                "task_title": final_task.get("title"),
                "task_state": final_task.get("status"),
                "previous_state": previous_state,
                "next_state": final_task.get("status"),
                "blocker_type": "dev_blocked",
                "diagnostic": diagnostic,
            }
            record["notification"] = _send_run_notification(config_path, profile, record)
            written = _write_run(profile, record)
            transition_event = _write_transition_event(profile, record)
            return result("blocked", record["summary"], run=written, event=transition_event, task=final_task)

        stale_task = _matching_stale_task(stale_before_lock, item)
        git_before = _git_status(profile) if profile.auto_commit else {"status": "disabled"}
        if profile.auto_commit and git_before["status"] != "clean":
            if stale_task and git_before["status"] == "dirty_before_run":
                git_before = {
                    "status": "clean",
                    "summary": "检测到同一任务的陈旧运行态，允许接管上一轮遗留改动。",
                    "porcelain": git_before.get("porcelain", ""),
                }
            elif _can_continue_dirty_executor_failure(profile, item, git_before):
                git_before = {"status": "clean", "summary": "继续接管上一轮 executor 失败留下的 dev-task 状态。"}
            elif _can_continue_dirty_non_terminal(latest_before, item, git_before):
                git_before = {"status": "clean", "summary": "继续上一次未完成任务留下的工作区改动。"}
            elif _can_continue_dirty_resolved_task(profile, item, git_before):
                git_before = {"status": "clean", "summary": "继续处理当前任务阻塞恢复留下的工作区改动。"}
            else:
                now = utc_now()
                diagnostic = diagnostic_for_item(
                    item,
                    profile,
                    kind="dirty_worktree",
                    summary="自动 commit 已启用，但运行前工作区不干净，已取消本轮执行。",
                    required_action=git_before.get("summary") or "请先处理项目内未提交改动后重试。",
                    git_status=git_before,
                )
                record = {
                    "run_id": run_id,
                    "project_id": profile.project_id,
                    "trigger": trigger,
                    "loop_type": "dev",
                    "status": "failed",
                    "started_at": now,
                    "ended_at": now,
                    "duration_seconds": 0,
                    "summary": "自动 commit 已启用，但运行前工作区不干净，已取消本轮执行。",
                    "required_action": git_before.get("summary") or "请先处理项目内未提交改动后重试。",
                    "commit": git_before,
                    "task_id": item.get("id"),
                    "task_title": item.get("title"),
                    "task_state": item.get("status"),
                    "diagnostic": diagnostic,
                }
                record["notification"] = _send_run_notification(config_path, profile, record)
                return result("failed", record["summary"], run=_write_run(profile, record))

        authority_roots: Dict[str, Any] = {}
        if profile.worktree_managed:
            preparation_error: Optional[WorktreePreparationError] = None
            try:
                prepared_execution = prepare_task_worktrees(profile, item, run_id)
                item["execution_root"] = prepared_execution["execution_root"]
                if task_uses_document_contract(item):
                    reservation = load_published_reservation_for_task(profile, str(item.get("id") or ""))
                    authority_roots = _runtime_authority_roots(item, reservation)
            except TaskPublisherError as exc:
                preparation_error = WorktreePreparationError(
                    exc.code,
                    exc.summary,
                    {"detail": exc.detail} if exc.detail else {},
                )
            except WorktreePreparationError as exc:
                preparation_error = exc
            if preparation_error is not None:
                exc = preparation_error
                now = utc_now()
                blocker = {
                    "id": exc.code,
                    "code": exc.code,
                    "type": exc.code,
                    "message": exc.summary,
                    "summary": exc.summary,
                    "resolution": "请修正仓库、分支或 worktree ownership 冲突后重新运行当前任务。",
                    "detail": exc.detail,
                }
                final_task = update_item(
                    profile,
                    str(item.get("id") or ""),
                    status="dev_blocked",
                    blockers=[blocker],
                    blocker_reason=exc.summary,
                    required_action=blocker["resolution"],
                )
                record = {
                    "run_id": run_id,
                    "project_id": profile.project_id,
                    "trigger": trigger,
                    "loop_type": "dev",
                    "status": "blocked",
                    "started_at": now,
                    "ended_at": now,
                    "duration_seconds": 0,
                    "summary": exc.summary,
                    "required_action": blocker["resolution"],
                    "task_id": final_task.get("id"),
                    "task_title": final_task.get("title"),
                    "task_state": final_task.get("status"),
                    "previous_state": str(item.get("status") or ""),
                    "next_state": final_task.get("status"),
                    "blocker_type": "dev_blocked",
                    "worktree_error": {"code": exc.code, **exc.detail},
                }
                record["notification"] = _send_run_notification(config_path, profile, record)
                written = _write_run(profile, record)
                transition_event = _write_transition_event(profile, record)
                return result("blocked", record["summary"], run=written, event=transition_event, task=final_task)
            item["planning_runtime"] = ensure_planning_workspace(profile, item)

        if stale_task:
            item.update(
                {
                    "agent_status": "timeout",
                    "agent_ended_at": utc_now(),
                    "last_error": stale_task["required_action"],
                }
            )

        started_at = utc_now()
        previous_state = str(item.get("status") or "open")
        next_state = "claimed" if previous_state == "open" else previous_state
        for key in RUN_START_CLEARED_FIELDS:
            item.pop(key, None)
        clear_blocker_fields(item)
        item.update(
            {
                "status": next_state,
                "agent": item.get("agent") or profile.default_agent,
                "agent_executor": profile.executor,
                "agent_status": "running",
                "agent_run_id": run_id,
                "agent_started_at": started_at,
                "agent_ended_at": None,
            }
        )
        save_dev_tasks(profile, payload)
        normalized_before = normalize_item(item, profile)
        clean_run_instruction = run_instruction.strip()
        if clean_run_instruction:
            normalized_before["run_instruction"] = clean_run_instruction
        if authority_roots:
            normalized_before["authority_roots"] = authority_roots
        with lock_heartbeat(profile, run_id):
            executor_result = run_executor(profile, normalized_before, run_id)
        loaded_worker_result = load_worker_result(executor_result)
        dev_decision = worker_result_decision(
            loaded_worker_result,
            item,
            execution_root=item.get("execution_root") or profile.root_dir,
            profile=profile,
        )
        if profile.worktree_managed and dev_decision and dev_decision["next_state"] == "ready_for_review":
            planning_status = planning_gate(profile, item, loaded_worker_result or {})
            if not planning_status["ready"]:
                dev_decision.update(
                    {
                        "next_state": "dev_blocked",
                        "summary": planning_status["summary"],
                        "required_action": planning_status["summary"],
                        "blockers": [
                            {
                                "type": "planning_incomplete",
                                "code": "planning_incomplete",
                                "summary": planning_status["summary"],
                            }
                        ],
                    }
                )
        final_status = executor_result.get("final_status")
        if dev_decision:
            final_status = dev_decision["next_state"]
        update_fields: Dict[str, Any] = {
            "agent_status": executor_result.get("status"),
            "agent_exit_code": executor_result.get("exit_code"),
            "agent_ended_at": utc_now(),
            "last_run_summary": executor_result.get("summary"),
            "last_message_path": executor_result.get("last_message_path"),
            "log_path": executor_result.get("log_path"),
        }
        if executor_result.get("status") == "failed":
            update_fields["last_error"] = executor_result.get("stderr") or executor_result.get("summary")
        if final_status:
            update_fields["status"] = final_status
            if final_status == "completed":
                update_fields["completed_at"] = utc_now()
            elif final_status == "ready_for_review":
                update_fields["review_ready_at"] = utc_now()
            elif final_status in {"spec_blocked", "dev_blocked"} and dev_decision:
                update_fields["blockers"] = dev_decision["blockers"]
                update_fields["blocker_reason"] = dev_decision["required_action"] or dev_decision["summary"]
        if dev_decision:
            if dev_decision["documentation"]:
                update_fields["documentation_result"] = dev_decision["documentation"]
            if dev_decision["review_required"]:
                update_fields["review_required"] = True
            worker_payload = dev_decision.get("worker_result")
            if isinstance(worker_payload, dict) and isinstance(worker_payload.get("planning"), dict):
                update_fields["planning"] = worker_payload["planning"]
            updated_targets = merge_target_results(item, dev_decision["targets"])
            if updated_targets is not None:
                update_fields["targets"] = updated_targets
        try:
            final_task = update_item(profile, normalized_before["id"], **update_fields)
        except KeyError:
            if executor_result.get("status") != "completed":
                raise
            final_task = _archived_task_after_success(normalized_before, update_fields)
        commit_result = {"status": "disabled"}
        successful_terminal_or_review = final_task.get("status") in {"completed", "ready_for_review"}
        non_terminal_success = (
            executor_result.get("status") == "completed"
            and not successful_terminal_or_review
            and dev_decision is None
        )
        if non_terminal_success:
            commit_result = {
                "status": "skipped_non_terminal",
                "summary": "任务未完成，跳过自动 commit。",
            }
        elif profile.auto_commit and executor_result.get("status") == "completed" and successful_terminal_or_review:
            commit_result = _auto_commit_changes(profile, final_task, run_id)
        ended_at = utc_now()
        status = str(executor_result.get("status") or "failed")
        summary = executor_result.get("summary") or ""
        required_action = executor_result.get("required_action") or ""
        blocker_type = None
        if dev_decision:
            next_state = dev_decision["next_state"]
            summary = dev_decision["summary"]
            required_action = dev_decision["required_action"]
            if next_state == "ready_for_review":
                status = "completed"
            elif next_state in {"spec_blocked", "dev_blocked"}:
                status = "blocked"
                blocker_type = next_state
        if non_terminal_success:
            final_state = str(final_task.get("status") or "")
            task_blocker_reason = str(final_task.get("blocker_reason") or "").strip()
            status = "failed"
            summary = f"agent 已退出但任务未完成，当前状态：{_task_state_label(final_task.get('status'))}"
            if final_state in {"prd_blocked", "blocked"}:
                required_action = task_blocker_reason or "请打开当前任务规格文档，补齐阻塞的需求口径后再运行。"
                blocker_type = final_state
            else:
                required_action = task_blocker_reason or "请继续运行一轮，或检查 worker 为什么没有推进到 completed / blocked。"
                blocker_type = "non_terminal_task"
        if commit_result.get("status") == "failed":
            status = "failed"
            summary = commit_result.get("summary") or "自动 commit 失败"
            required_action = commit_result.get("required_action") or ""
        artifacts = executor_result.get("artifacts") if isinstance(executor_result.get("artifacts"), dict) else {}
        diagnostic = final_task.get("diagnostic") if isinstance(final_task.get("diagnostic"), dict) else None
        if diagnostic is None and (status != "completed" or required_action or blocker_type):
            diagnostic = diagnostic_for_item(
                final_task,
                profile,
                kind=status,
                summary=summary,
                required_action=required_action,
                artifacts=artifacts,
            )
        if diagnostic is not None:
            diagnostic = {
                **diagnostic,
                "failure_kind": blocker_type or diagnostic.get("failure_kind") or status,
                "summary": summary or diagnostic.get("summary", ""),
                "next_action": required_action or diagnostic.get("next_action", ""),
                "artifacts": artifacts,
            }
        if diagnostic is not None and executor_result.get("result_path"):
            merge_result_artifact(str(executor_result["result_path"]), diagnostic=diagnostic)
        record = {
            "run_id": run_id,
            "project_id": profile.project_id,
            "trigger": trigger,
            "loop_type": "dev",
            "status": status,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": 0,
            "summary": summary,
            "task_id": final_task["id"],
            "task_title": final_task["title"],
            "task_state": final_task["status"],
            "previous_state": previous_state,
            "next_state": final_task["status"],
            "agent": final_task["agent"],
            "executor": profile.executor,
            "agent_exit_code": executor_result.get("exit_code"),
            "last_message_path": executor_result.get("last_message_path"),
            "log_path": executor_result.get("log_path"),
            "result_path": executor_result.get("result_path"),
            "artifacts": artifacts,
            "commit": commit_result,
            "associated_project_ids": task_associated_project_ids(profile.project_id, final_task),
            **request_metadata(profile),
        }
        if isinstance(executor_result.get("token_usage"), dict):
            record["token_usage"] = executor_result["token_usage"]
        if isinstance(executor_result.get("provider_usage"), dict):
            record["provider_usage"] = executor_result["provider_usage"]
        if executor_result.get("reported_cost_usd") is not None:
            record["reported_cost_usd"] = executor_result["reported_cost_usd"]
        if executor_result.get("session_id"):
            record["worker_session_id"] = executor_result["session_id"]
        if dev_decision:
            record["worker_result"] = dev_decision["worker_result"]
            record["worker_result_source"] = dev_decision["worker_result_source"]
            if dev_decision.get("worker_result_path"):
                record["worker_result_path"] = dev_decision["worker_result_path"]
            if dev_decision.get("validation"):
                record["validation"] = dev_decision["validation"]
        if diagnostic is not None:
            record["diagnostic"] = diagnostic
        if final_task.get("blocker_reason"):
            record["blocker_reason"] = final_task["blocker_reason"]
        if blocker_type:
            record["blocker_type"] = blocker_type
        if required_action:
            record["required_action"] = required_action
        record["notification"] = _send_run_notification(config_path, profile, record)
        written = _write_run(profile, record)
        transition_event = _write_transition_event(profile, record)
        cancel_event = None
        if status == "cancelled":
            cancel_event = _write_event(
                profile,
                "run_cancelled",
                record.get("summary") or "运行已取消",
                run_id=run_id,
                task_id=final_task.get("id"),
                task_title=final_task.get("title"),
            )
        return result(status, record.get("summary", ""), run=written, event=transition_event, cancel_event=cancel_event, task=final_task)


def project_start_run(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    """手动一次启动，按任务状态自动衔接需求校准与开发执行。"""

    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors), steps=[])
    try:
        payload = load_dev_tasks(profile)
        item = choose_item(payload)
    except Exception as exc:
        error_payload = _dev_task_error_payload(exc)
        return result(error_payload["status"], error_payload["summary"], required_action=error_payload["required_action"], steps=[])
    if item is None:
        return result("skipped_no_task", "没有可运行的 dev-task item", steps=[])

    task_state = str(item.get("status") or "open")
    normalized = normalize_item(item, profile)
    if task_state in USER_WAITING_TASK_STATES:
        if task_state == "ready_for_review":
            return result("completed", "当前任务已进入人工验收，无需继续运行。", task=normalized, steps=[])
        return result(
            "blocked",
            "当前任务正在等待人工处理，未启动新的运行。",
            required_action=normalized.get("blocker_reason") or normalized.get("next_action") or "请先处理当前阻塞。",
            task=normalized,
            steps=[],
        )

    steps: List[Dict[str, Any]] = []
    if task_state in {"open", "claimed", "spec_ready", "prd_ready", "coding"}:
        development = project_run_once(config_path, project_id, trigger="manual_start", loop_type="dev")
        steps.append(development)
        return {**development, "steps": steps}

    return result("skipped_no_task", "当前任务状态无需启动运行。", task=normalized, steps=steps)


def _git_status(profile: ProjectProfile) -> Dict[str, Any]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(profile.root_dir), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return {"status": "failed", "summary": f"读取 git 状态失败：{exc}", "required_action": str(exc)}
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        return {"status": "failed", "summary": "项目不是可提交的 git 工作区", "required_action": detail}
    porcelain = _filter_loopforge_state_changes(profile, proc.stdout)
    if profile.worktree_managed and _uses_single_task_storage(profile):
        porcelain = _filter_single_task_store_changes(porcelain)
    if porcelain:
        return {
            "status": "dirty_before_run",
            "summary": "运行前工作区已有未提交改动，LoopForge 无法安全自动 commit。",
            "porcelain": porcelain,
        }
    return {"status": "clean", "summary": "运行前工作区干净"}


def _loopforge_state_pathspec(profile: ProjectProfile) -> Optional[str]:
    try:
        rel = profile.loopforge_dir.resolve().relative_to(profile.root_dir.resolve())
    except ValueError:
        return None
    return rel.as_posix()


def _filter_loopforge_state_changes(profile: ProjectProfile, porcelain: str) -> str:
    state_path = _loopforge_state_pathspec(profile)
    if not state_path:
        return porcelain.strip()
    kept = []
    for raw_line in porcelain.splitlines():
        path = _git_status_path(raw_line)
        if path == state_path or path.startswith(f"{state_path}/"):
            continue
        kept.append(raw_line)
    return "\n".join(kept).strip()


def _filter_single_task_store_changes(porcelain: str) -> str:
    """受管跨仓任务的父级 main 只关心自身 Task Store 变化。"""

    kept = []
    for raw_line in porcelain.splitlines():
        path = _git_status_path(raw_line)
        if path.startswith("data/tasks/"):
            kept.append(raw_line)
    return "\n".join(kept).strip()


def _dev_task_pathspec(profile: ProjectProfile, item: Optional[Dict[str, Any]] = None) -> Optional[str]:
    if _uses_single_task_storage(profile):
        task_id = str((item or {}).get("id") or "").strip()
        return f"data/tasks/{task_id}.json" if task_id else None
    try:
        rel = (profile.root_dir / "data" / "dev-task.json").resolve().relative_to(profile.root_dir.resolve())
    except ValueError:
        return None
    return rel.as_posix()


def _git_status_path(raw_line: str) -> str:
    line = raw_line.rstrip()
    path = line[3:] if len(line) > 3 and line[2] == " " else line[2:]
    if " -> " in path:
        path = path.split(" -> ", 1)[1]
    return path.strip()


def _is_only_dev_task_dirty(profile: ProjectProfile, porcelain: str) -> bool:
    dev_task_path = _dev_task_pathspec(profile)
    if not dev_task_path:
        return False
    paths = [_git_status_path(line) for line in porcelain.splitlines() if line.strip()]
    return bool(paths) and all(path == dev_task_path for path in paths)


def _is_current_task_dirty(profile: ProjectProfile, item: Dict[str, Any], porcelain: str) -> bool:
    dev_task_path = _dev_task_pathspec(profile, item)
    allowed = {dev_task_path} if dev_task_path else set()
    # 任务可以声明属于自己的规划工作区；该目录内的改动算作当前任务自有改动。
    planning_workspace = str(item.get("planning_workspace") or "").strip().strip("/")
    if planning_workspace and not planning_workspace.startswith(".."):
        allowed.add(planning_workspace)
    paths = [_git_status_path(line) for line in porcelain.splitlines() if line.strip()]
    if not paths or not allowed:
        return False
    for path in paths:
        if path in allowed:
            continue
        if any(base and path.startswith(f"{base}/") for base in allowed):
            continue
        if path.endswith("/") and any(base and base.startswith(path.rstrip("/")) for base in allowed):
            continue
        return False
    return True


def _auto_commit_changes(profile: ProjectProfile, task: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    status = _git_status(profile)
    if status["status"] == "clean":
        return {"status": "skipped_no_changes", "summary": "没有需要提交的改动，可能已由 agent 完成 commit。"}
    if status["status"] not in {"dirty_before_run"}:
        status["summary"] = status.get("summary") or "自动 commit 前读取 git 状态失败"
        return status

    add_command = ["git", "-C", str(profile.root_dir), "add", "-A", "--", "."]
    state_path = _loopforge_state_pathspec(profile)
    add = subprocess.run(add_command, check=False, capture_output=True, text=True)
    if add.returncode != 0:
        detail = (add.stderr or add.stdout or "").strip()
        return {"status": "failed", "summary": "自动 commit 失败：git add 失败", "required_action": detail}
    if state_path:
        reset = subprocess.run(
            ["git", "-C", str(profile.root_dir), "reset", "-q", "--", state_path],
            check=False,
            capture_output=True,
            text=True,
        )
        if reset.returncode != 0:
            detail = (reset.stderr or reset.stdout or "").strip()
            return {"status": "failed", "summary": "自动 commit 失败：排除 LoopForge 状态目录失败", "required_action": detail}

    title = str(task.get("title") or task.get("id") or run_id).strip()
    message = f"loopforge: {title}"
    commit = subprocess.run(
        ["git", "-C", str(profile.root_dir), "commit", "-m", message],
        check=False,
        capture_output=True,
        text=True,
    )
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or "").strip()
        return {"status": "failed", "summary": "自动 commit 失败：git commit 失败", "required_action": detail}

    rev = subprocess.run(
        ["git", "-C", str(profile.root_dir), "rev-parse", "--short", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return {
        "status": "committed",
        "summary": "自动 commit 已完成",
        "commit": rev.stdout.strip(),
        "message": message,
    }


def _archived_task_after_success(task: Dict[str, Any], update_fields: Dict[str, Any]) -> Dict[str, Any]:
    status = str(update_fields.get("status") or "completed")
    final_task = dict(task)
    final_task.update(
        {
            "status": status,
            "state": status,
            "state_label": _task_state_label(status),
            "agent_status": update_fields.get("agent_status"),
            "agent_exit_code": update_fields.get("agent_exit_code"),
            "agent_ended_at": update_fields.get("agent_ended_at"),
            "last_run_summary": update_fields.get("last_run_summary"),
            "last_message_path": update_fields.get("last_message_path"),
            "log_path": update_fields.get("log_path"),
            "completed_at": update_fields.get("completed_at") or utc_now(),
        }
    )
    return final_task


def project_preview_run(config_path: Optional[Path], project_id: str, loop_type: str = "dev") -> Dict[str, Any]:
    try:
        clean_loop_type = _normalize_loop_type(loop_type)
    except ValueError as exc:
        return result("failed", str(exc))
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    run_id = _run_id()
    if clean_loop_type == "scan":
        plan = {
            "executor": "loopforge_scan",
            "loop_type": "scan",
            "phases": ["collect_signals", "generate_clues", "write_scan_cursor", "write_results"],
            "inputs": [
                "data/tasks/<task-id>.json",
                "docs/README.md",
                "docs/**/requirements.md",
                "docs/**/design.md",
                "docs/**/specs.md",
                "最近运行记录",
                "last_completed_scan_ref..HEAD diff",
            ],
            "writes": [
                str(profile.loopforge_dir / "clues"),
                str(scan_state_path(profile)),
                str(profile.history_path),
                str(profile.events_path),
            ],
        }
        return result("completed", f"预演项目巡检：{profile.name}", project=profile_to_dict(profile), plan=plan, run_id=run_id)
    payload = load_dev_tasks(profile)
    item = _choose_dev_item_with_legacy_fallback(payload)
    if item is None:
        return result("skipped_no_task", "没有可运行的 dev-task item")
    normalized = normalize_item(item, profile)
    plan = executor_preview(profile, normalized, run_id)
    plan["loop_type"] = "dev"
    return result("completed", f"预演项目：{profile.name}", project=profile_to_dict(profile), task=normalized, plan=plan, run_id=run_id)


def _write_transition_event(profile: ProjectProfile, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    previous_state = record.get("previous_state")
    next_state = record.get("next_state") or record.get("task_state")
    task_id = record.get("task_id")
    if not previous_state or not next_state or not task_id or previous_state == next_state:
        return None
    path_labels = record.get("state_path_labels")
    if isinstance(path_labels, list) and path_labels:
        summary = "完整执行：" + " 到 ".join(str(label) for label in path_labels)
    else:
        summary = f"任务状态 {_task_state_label(previous_state)} 到 {_task_state_label(next_state)}"
    return _write_event(
        profile,
        "task_transition",
        summary,
        task_id=task_id,
        task_title=record.get("task_title"),
        previous_state=previous_state,
        next_state=next_state,
        state_path=record.get("state_path"),
        state_path_labels=path_labels,
        run_id=record.get("run_id"),
    )


def _normalize_timeout_status(profile: ProjectProfile, record: Dict[str, Any]) -> str:
    status = str(record.get("status"))
    task_id = str(record.get("task_id") or "")
    if status != "timeout_continue" or not task_id:
        return status
    previous = read_jsonl(profile.history_path, limit=20)
    if count_consecutive_timeouts(previous, task_id) >= 1:
        record["blocker_type"] = "timeout_blocked"
        record["required_action"] = record.get("required_action") or "同一任务连续 2 次超时，请人工决策。"
        return "timeout_blocked"
    return status


def project_cancel_run(config_path: Optional[Path], project_id: str, run_id: str = "", reason: str = "") -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    lock = read_lock(profile.lock_path)
    if not lock or lock.get("invalid") or not is_lock_active(lock):
        return result("skipped_no_running", "当前项目没有有效运行中的 run")
    current_run_id = str(lock.get("run_id") or "")
    requested_run_id = run_id.strip() or current_run_id
    if requested_run_id != current_run_id:
        return result(
            "skipped_no_running",
            "指定 run_id 与当前运行不匹配，未取消。",
            requested_run_id=requested_run_id,
            current_run_id=current_run_id,
            lock=lock,
        )
    try:
        cancel_request = write_cancel_request(profile, current_run_id, reason.strip() or "用户在控制台取消")
    except ValueError as exc:
        return result("failed", str(exc))
    event = _write_event(
        profile,
        "cancel_run_requested",
        f"已请求取消运行：{current_run_id}",
        run_id=current_run_id,
        loop_type=lock.get("loop_type") or "dev",
        reason=cancel_request.get("reason"),
    )
    return result(
        "cancel_requested",
        "取消请求已记录，等待当前运行在安全检查点停止。",
        run_id=current_run_id,
        loop_type=lock.get("loop_type") or "dev",
        cancel_request=cancel_request,
        event=event,
    )


def project_pause(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    event = _write_event(profile, "pause", "项目暂停事件已记录", next_state="paused")
    return result("completed", "项目暂停事件已记录", event=event)


def project_resume_once(config_path: Optional[Path], project_id: str, reason: str = "") -> Dict[str, Any]:
    return project_run_once(config_path, project_id, trigger="resume_once", run_instruction=reason)


def project_resolve_and_resume(config_path: Optional[Path], project_id: str, reason: str = "") -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    clean_reason = reason.strip() or "人工确认 blocker 已处理"
    before_payload = load_dev_tasks(profile)
    resolved_task = resolve_blocked_item(profile, clean_reason)
    event = _write_event(
        profile,
        "resolve_blocker",
        clean_reason,
        task_id=resolved_task.get("id") if resolved_task else None,
        task_title=resolved_task.get("title") if resolved_task else None,
        next_state=resolved_task.get("state") if resolved_task else None,
    )
    run_payload = project_run_once(config_path, project_id, trigger="resolve_and_resume")
    run_record = run_payload.get("run") if isinstance(run_payload.get("run"), dict) else {}
    commit_record = run_record.get("commit") if isinstance(run_record.get("commit"), dict) else {}
    if run_payload.get("status") == "failed" and commit_record.get("status") == "dirty_before_run":
        save_dev_tasks(profile, before_payload)
        return result(
            "failed",
            "已取消恢复运行，当前阻塞状态已回滚",
            event=event,
            run=run_payload,
            required_action=run_record.get("required_action") or "请先处理项目内未提交改动后重试。",
        )
    return result(run_payload["status"], "已记录处理说明并触发恢复运行", event=event, run=run_payload, task=resolved_task)


def project_latest_report(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    runs = read_jsonl(profile.history_path, limit=1)
    latest = runs[-1] if runs else {}
    return result(
        "completed",
        "已读取最近运行报告",
        report={
            "exists": bool(latest.get("last_message_path")),
            "report_path": latest.get("last_message_path"),
            "run_id": latest.get("run_id"),
            "created_at": latest.get("ended_at"),
            "title": latest.get("task_title") or "最近运行输出",
        },
    )


def project_runs(config_path: Optional[Path], project_id: str, limit: int = 50) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    return result("completed", "已读取运行历史", runs=read_jsonl(profile.history_path, limit=limit))


def task_token_usage(config_path: Optional[Path], project_id: str = "", limit: int = 20) -> Dict[str, Any]:
    safe_limit = max(1, min(100, int(limit)))
    payload = build_task_usage(load_projects(config_path), project_id=project_id, limit=safe_limit)
    return result(
        "completed",
        "已读取任务 Token 用量",
        items=payload["items"],
        issues=payload["issues"],
        project_id=project_id,
        limit=safe_limit,
    )


def project_events(config_path: Optional[Path], project_id: str, limit: int = 50) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    return result("completed", "已读取事件历史", events=read_jsonl(profile.events_path, limit=limit))


def project_clues(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    clues = list_clues(profile, limit=100)
    return result("completed", "线索列表已读取", clues=clues, counts=clue_counts(clues))


def project_clue_add(config_path: Optional[Path], project_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    try:
        clue, created = create_or_update_clue(profile, payload)
    except (KeyError, ValueError) as exc:
        return result("failed", str(exc))
    event = _write_event(
        profile,
        "clue_created" if created else "clue_seen",
        clue.get("summary") or "线索已写入",
        clue_id=clue.get("id"),
        clue_type=clue.get("type"),
        dedupe_key=clue.get("dedupe_key"),
    )
    return result(
        "completed",
        "线索已创建" if created else "线索已去重更新",
        clue=clue,
        created=created,
        event=event,
    )


def project_clue_decide(
    config_path: Optional[Path],
    project_id: str,
    clue_id: str,
    decision: str,
    reason: str = "",
    task_title: str = "",
    task_description: str = "",
) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    clean_decision = decision.strip()
    try:
        task = None
        if clean_decision == "create_task":
            original = list_clues(profile, limit=1000)
            clue = next((item for item in original if item.get("id") == clue_id), None)
            if clue is None:
                return result("failed", f"未知线索：{clue_id}")
            title = task_title.strip() or str(clue.get("summary") or "线索升级任务")
            description = task_description.strip() or f"由线索 {clue_id} 升级：{clue.get('summary') or ''}"
            task = add_task(profile, title, description, "clue")
            task = update_item(
                profile,
                task["id"],
                status="spec_blocked",
                blocker_reason=reason.strip() or "线索已确认，需要补齐模块三件套后再开发。",
                clue_id=clue_id,
            )
            clue = decide_clue(profile, clue_id, clean_decision, reason, task_id=task["id"])
        else:
            clue = decide_clue(profile, clue_id, clean_decision, reason)
    except (KeyError, ValueError) as exc:
        return result("failed", str(exc))
    event = _write_event(
        profile,
        "clue_decided",
        f"线索已裁决：{clean_decision}",
        clue_id=clue.get("id"),
        decision=clean_decision,
        reason=reason,
        task_id=clue.get("task_id"),
    )
    return result(
        "completed",
        "线索裁决已保存",
        clue=clue,
        task=task,
        mapped_status="spec_blocked" if clean_decision == "create_task" else clue.get("status"),
        event=event,
    )


def project_tasks(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors), tasks=[])
    try:
        payload = load_dev_tasks(profile)
        tasks = active_items(payload, profile)
        sync_blocker = read_sync_blocker(profile)
        if sync_blocker:
            tasks = [
                {**task, "sync_blocked": sync_blocker}
                if task.get("id") == sync_blocker.get("task_id")
                else task
                for task in tasks
            ]
    except Exception as exc:
        error_payload = _dev_task_error_payload(exc)
        return result(
            error_payload["status"],
            error_payload["summary"],
            required_action=error_payload["required_action"],
            tasks=[],
            capabilities={},
        )
    return result(
        "completed",
        "dev-task 非终态任务列表已读取",
        tasks=tasks,
        capabilities={
            "task_add": True,
            "task_ai_create": True,
            "task_create_from_docs": True,
            "task_reorder": False,
        },
        sync_blocked=sync_blocker,
    )


def project_task_history(config_path: Optional[Path], project_id: str, limit: int = 20) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    clean_limit = max(1, min(20, int(limit)))
    history = load_history_tasks(profile, limit=clean_limit)
    return result(
        "completed",
        "最近历史任务已读取",
        tasks=[normalize_item(item, profile) for item in history["items"]],
        issues=history["issues"],
        limit=clean_limit,
    )


def project_task_reserve(config_path: Optional[Path], project_id: str, request: Dict[str, Any]) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    reservation = reserve_task(profile, request)
    return result(
        "completed",
        "任务 ID、feature branch 与 worktree 已预留",
        reservation={
            "reservation_id": reservation.reservation_id,
            "task_id": reservation.task_id,
            "status": reservation.status,
            "bindings": reservation.bindings,
        },
    )


def project_task_reservation_cancel(
    config_path: Optional[Path],
    project_id: str,
    reservation_id: str,
    *,
    discard_changes: bool = False,
) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    try:
        cancelled = cancel_reservation(
            profile,
            reservation_id.strip(),
            discard_changes=discard_changes,
        )
    except TaskPublisherError as exc:
        return result("failed", exc.summary, code=exc.code, detail=exc.detail)
    event = _write_event(
        profile,
        "task_reservation_cancelled" if cancelled["status"] == "completed" else "task_reservation_cancel_failed",
        cancelled["summary"],
        task_id=cancelled.get("reservation", {}).get("task_id"),
        reservation_id=reservation_id.strip(),
        cleanup_result=cancelled.get("cleanup_result"),
        discard_confirmed=bool(discard_changes),
    )
    return result(
        cancelled["status"],
        cancelled["summary"],
        reservation=cancelled.get("reservation"),
        cleanup_result=cancelled.get("cleanup_result"),
        required_action=cancelled.get("required_action"),
        discard_confirmed=bool(discard_changes),
        event=event,
    )


def project_task_reservation_migrate_branches(
    config_path: Optional[Path],
    project_id: str,
    reservation_id: str,
) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    try:
        reservation = migrate_reservation_feature_branches(profile, reservation_id.strip())
    except TaskPublisherError as exc:
        return result("failed", exc.summary, code=exc.code, detail=exc.detail)
    event = _write_event(
        profile,
        "task_reservation_branches_migrated",
        "reservation 受管特性分支已迁移为 feature/ 前缀",
        task_id=reservation.task_id,
        reservation_id=reservation.reservation_id,
    )
    return result(
        "completed",
        "reservation 受管特性分支已迁移为 feature/ 前缀",
        reservation={
            "reservation_id": reservation.reservation_id,
            "task_id": reservation.task_id,
            "status": reservation.status,
            "bindings": reservation.bindings,
        },
        event=event,
    )


def project_task_add(
    config_path: Optional[Path],
    project_id: str,
    title: str,
    description: str = "",
    acceptance: Optional[List[Any]] = None,
    targets: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    clean_title = title.strip()
    if not clean_title:
        return result("failed", "任务标题不能为空")
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    clean_acceptance = _clean_acceptance_list(acceptance)
    try:
        clean_targets = _clean_target_list(profile, targets)
    except TargetPlanError as exc:
        return result("failed", exc.summary, code=exc.code, blocker=_target_plan_blocker(exc))
    task = add_task(
        profile,
        clean_title,
        description.strip(),
        "manual",
        acceptance=clean_acceptance or None,
        targets=clean_targets,
    )
    event = None
    event = _write_event(
        profile,
        "task_item_added",
        f"新增任务：{task.get('title') or clean_title}",
        task_id=task.get("id"),
        task_title=task.get("title") or clean_title,
        source="manual",
    )
    return result("completed", "任务已通过 Git publisher 发布", task=task, event=event)


def project_task_ai_create(config_path: Optional[Path], project_id: str, prompt: str) -> Dict[str, Any]:
    clean_prompt = prompt.strip()
    if not clean_prompt:
        return result("failed", "一句话需求不能为空")
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    task = ai_create_task(profile, clean_prompt)
    event = _write_event(
        profile,
        "task_item_ai_created",
        f"规则生成任务：{task.get('title') or clean_prompt}",
        task_id=task.get("id"),
        task_title=task.get("title") or clean_prompt,
        source="ai",
        prompt=clean_prompt,
    )
    return result("completed", "任务已通过 Git publisher 发布", task=task, event=event)


def _uses_single_task_storage(profile: ProjectProfile) -> bool:
    return not (profile.root_dir / "data" / "dev-task.json").exists() and (profile.root_dir / "data" / "tasks").is_dir()


def _reservation_validation_profile(profile: ProjectProfile, reservation: Any) -> ProjectProfile:
    ledger = next((binding for binding in reservation.bindings if binding.get("kind") == "ledger"), None)
    if ledger is None:
        raise TaskPublisherError("ledger_binding_missing", "reservation 缺少文档 worktree binding")

    target_bindings = [binding for binding in reservation.bindings if binding.get("kind") == "target"]
    if not target_bindings:
        return replace(profile, root_dir=Path(str(ledger["worktree_path"])))

    group = dict(profile.project_group) if isinstance(profile.project_group, dict) else {}
    children = group.get("children")
    if not isinstance(children, list):
        raise TaskPublisherError("project_group_missing", "多 Target reservation 缺少 project_group 配置")
    by_identity: Dict[str, Dict[str, Any]] = {}
    for binding in target_bindings:
        for identity in (binding.get("target_id"), binding.get("project")):
            clean = str(identity or "").strip()
            if clean:
                by_identity[clean] = binding

    resolved_children: List[Dict[str, Any]] = []
    matched: set[str] = set()
    for raw_child in children:
        child = dict(raw_child) if isinstance(raw_child, dict) else {}
        identities = {
            str(child.get("key") or "").strip(),
            str(child.get("id") or "").strip(),
            str(child.get("project_id") or "").strip(),
        }
        binding = next((by_identity[value] for value in identities if value and value in by_identity), None)
        if binding is not None:
            child["path"] = str(binding["worktree_path"])
            matched.add(str(binding.get("target_id") or binding.get("project") or ""))
        resolved_children.append(child)

    missing = sorted(
        str(binding.get("target_id") or binding.get("project") or "")
        for binding in target_bindings
        if str(binding.get("target_id") or binding.get("project") or "") not in matched
    )
    if missing:
        raise TaskPublisherError("target_binding_missing", "reservation Target 无法映射到 project_group", ", ".join(missing))
    group["children"] = resolved_children
    return replace(
        profile,
        root_dir=Path(str(ledger["worktree_path"])),
        project_group=group,
    )


def _runtime_authority_roots(item: Dict[str, Any], reservation: Any) -> Dict[str, Any]:
    """把任务文档路由到创建它们的父级与 Target feature worktree。"""

    ledger = next((binding for binding in reservation.bindings if binding.get("kind") == "ledger"), None)
    if ledger is None:
        raise TaskPublisherError("ledger_binding_missing", "reservation 缺少父级文档 worktree binding")

    roots: Dict[str, Any] = {"targets": {}}
    project_docs = item.get("docs")
    if isinstance(project_docs, dict) and project_docs:
        roots["project"] = _authority_root_from_binding(ledger, project_docs, "项目")

    bindings: Dict[str, Dict[str, Any]] = {}
    for binding in reservation.bindings:
        if binding.get("kind") != "target":
            continue
        for identity in (binding.get("target_id"), binding.get("project")):
            clean = str(identity or "").strip()
            if clean:
                bindings[clean] = binding

    for index, raw_target in enumerate(item.get("targets") or []):
        if not isinstance(raw_target, dict) or not isinstance(raw_target.get("docs"), dict):
            continue
        target_id = str(raw_target.get("id") or raw_target.get("project") or f"target-{index + 1}").strip()
        binding = bindings.get(target_id) or bindings.get(str(raw_target.get("project") or "").strip())
        if binding is None:
            raise TaskPublisherError("target_binding_missing", "Target 缺少文档 worktree binding", target_id)
        roots["targets"][target_id] = _authority_root_from_binding(
            binding,
            raw_target["docs"],
            f"Target {target_id}",
        )
    return roots


def _authority_root_from_binding(binding: Dict[str, Any], docs: Dict[str, Any], label: str) -> Dict[str, str]:
    root = Path(str(binding.get("worktree_path") or "")).resolve()
    revision = str(docs.get("revision") or "").strip()
    if not root.is_dir():
        raise TaskPublisherError("authority_worktree_missing", f"{label} 文档 worktree 不存在", str(root))
    if not revision:
        raise TaskPublisherError("authority_revision_missing", f"{label} 缺少 docs.revision")
    ancestor = subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", revision, "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    if ancestor.returncode != 0:
        raise TaskPublisherError(
            "authority_revision_mismatch",
            f"{label} 文档版本不属于当前 feature worktree",
            f"root={root} revision={revision}",
        )
    return {"root": str(root), "revision": revision}


def project_task_review(config_path: Optional[Path], project_id: str, task_id: str, decision: str, feedback: str = "") -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    try:
        action = review_task(profile, task_id, decision, feedback)
    except (KeyError, ValueError) as exc:
        return result("failed", str(exc))
    task = action["task"]
    transition_event = _write_transition_event(profile, _task_action_record(profile, action))
    event = _write_event(
        profile,
        "task_review_decided",
        action["summary"],
        task_id=task.get("id"),
        task_title=task.get("title"),
        decision=decision,
        previous_state=action.get("previous_state"),
        next_state=action.get("next_state"),
    )
    return result(action["status"], action["summary"], task=task, event=event, transition_event=transition_event)


def project_task_merge(
    config_path: Optional[Path],
    project_id: str,
    task_id: str,
    target_branch: str = "",
    merge_method: str = "",
) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    try:
        action = merge_task(profile, task_id, target_branch, merge_method)
    except (KeyError, ValueError) as exc:
        return result("failed", str(exc))
    task = action["task"]
    transition_event = _write_transition_event(profile, _task_action_record(profile, action))
    event_type = "task_merge_completed" if action["status"] == "completed" else "task_merge_failed"
    event = _write_event(
        profile,
        event_type,
        action["summary"],
        task_id=task.get("id"),
        task_title=task.get("title"),
        previous_state=action.get("previous_state"),
        next_state=action.get("next_state"),
        merge_result=action.get("merge_result"),
    )
    notification = _send_task_action_notification(config_path, profile, action, "merge_failed") if action["status"] == "failed" else {"status": "skipped", "reason": "合入成功不触发通知"}
    return result(
        action["status"],
        action["summary"],
        task=task,
        event=event,
        transition_event=transition_event,
        merge_result=action.get("merge_result"),
        required_action=action.get("required_action"),
        notification=notification,
    )


def project_task_cleanup(
    config_path: Optional[Path],
    project_id: str,
    task_id: str,
    *,
    discard_changes: bool = False,
) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    try:
        action = cleanup_task(profile, task_id, discard_changes=discard_changes)
    except (KeyError, ValueError) as exc:
        return result("failed", str(exc))
    task = action["task"]
    transition_event = _write_transition_event(profile, _task_action_record(profile, action))
    event_type = "task_cleanup_completed" if action["status"] == "completed" else "task_cleanup_failed"
    event = _write_event(
        profile,
        event_type,
        action["summary"],
        task_id=task.get("id"),
        task_title=task.get("title"),
        previous_state=action.get("previous_state"),
        next_state=action.get("next_state"),
        cleanup_result=action.get("cleanup_result"),
        discard_confirmed=bool(discard_changes),
    )
    notification = _send_task_action_notification(config_path, profile, action, "cleanup_failed") if action["status"] == "failed" else {"status": "skipped", "reason": "清理成功不触发通知"}
    return result(
        action["status"],
        action["summary"],
        task=task,
        event=event,
        transition_event=transition_event,
        cleanup_result=action.get("cleanup_result"),
        required_action=action.get("required_action"),
        discard_confirmed=bool(discard_changes),
        notification=notification,
    )


def project_task_abandon(
    config_path: Optional[Path],
    project_id: str,
    task_id: str,
    reason: str = "",
    *,
    discard_changes: bool = False,
) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    try:
        action = abandon_task(profile, task_id, reason, discard_changes=discard_changes)
    except (KeyError, ValueError) as exc:
        return result("failed", str(exc))
    task = action["task"]
    transition_event = _write_transition_event(profile, _task_action_record(profile, action))
    event = _write_event(
        profile,
        "task_abandoned" if action["status"] == "completed" else "task_abandon_cleanup_failed",
        action["summary"],
        task_id=task.get("id"),
        task_title=task.get("title"),
        previous_state=action.get("previous_state"),
        next_state=action.get("next_state"),
        reason=reason.strip(),
        cleanup_result=action.get("cleanup_result"),
        discard_confirmed=bool(discard_changes),
    )
    return result(
        action["status"],
        action["summary"],
        task=task,
        event=event,
        transition_event=transition_event,
        cleanup_result=action.get("cleanup_result"),
        required_action=action.get("required_action"),
        discard_confirmed=bool(discard_changes),
    )


def _task_action_record(profile: ProjectProfile, action: Dict[str, Any]) -> Dict[str, Any]:
    task = action.get("task") if isinstance(action.get("task"), dict) else {}
    return {
        "project_id": profile.project_id,
        "task_id": task.get("id"),
        "task_title": task.get("title"),
        "previous_state": action.get("previous_state"),
        "next_state": action.get("next_state") or task.get("status") or task.get("state"),
    }


def _send_task_action_notification(
    config_path: Optional[Path],
    profile: ProjectProfile,
    action: Dict[str, Any],
    notification_event: str,
) -> Dict[str, Any]:
    task = action.get("task") if isinstance(action.get("task"), dict) else {}
    record = {
        "run_id": f"task-action-{uuid.uuid4().hex[:8]}",
        "project_id": profile.project_id,
        "trigger": "manual_action",
        "loop_type": "task_action",
        "status": "failed",
        "notification_event": notification_event,
        "summary": action.get("summary") or "任务动作失败",
        "required_action": action.get("required_action") or "",
        "task_id": task.get("id"),
        "task_title": task.get("title"),
        "task_state": task.get("status") or task.get("state"),
    }
    return _send_run_notification(config_path, profile, record)


def global_settings(config_path: Optional[Path] = None) -> Dict[str, Any]:
    settings = read_global_config()
    settings["onboarding_defaults"] = {"owner": getpass.getuser().strip()}
    return result("completed", "全局设置已读取", settings=settings)


def global_notification_config(
    config_path: Optional[Path],
    enabled: bool,
    webhook_env: str = "",
    webhook_url: str = "",
) -> Dict[str, Any]:
    settings = update_wecom_config(enabled, webhook_env=webhook_env, webhook_url=webhook_url)
    return result("completed", "全局通知配置已保存", settings=settings)


def project_notification_config(config_path: Optional[Path], project_id: str, notification_channel: str, webhook_url: str = "") -> Dict[str, Any]:
    channel = notification_channel.strip() or "wecom_robot"
    if channel not in NOTIFICATION_CHANNELS:
        return result("failed", f"非法通知渠道：{channel}")
    clean_webhook_url = webhook_url.strip()
    settings = update_wecom_config(channel == "wecom_robot", webhook_url=clean_webhook_url)
    profile = _find_project(load_projects(config_path), project_id)
    event = _write_event(profile, "notification_config_updated", f"全局通知渠道已更新：{channel}", notification_channel=channel)
    return result(
        "completed",
        "全局通知配置已保存",
        project=_project_payload(profile, _profile_status_payload(profile), _latest_run(profile)),
        settings=settings,
        event=event,
    )


def project_runtime_config(
    config_path: Optional[Path],
    project_id: str,
    schedule_enabled: bool,
    auto_commit: bool,
    schedule_frequency: str = "hourly",
    codex_model: str = DEFAULT_CODEX_MODEL,
    codex_reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT,
    codex_sandbox: str = DEFAULT_CODEX_SANDBOX,
    automation_mode: str = "",
    worker_provider: str = "",
    worker_settings: Optional[Dict[str, Any]] = None,
    worker_network: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    clean_frequency = schedule_frequency.strip() or "hourly"
    from .domain import SCHEDULE_FREQUENCIES

    if clean_frequency not in SCHEDULE_FREQUENCIES:
        return result("failed", f"非法调度频率：{clean_frequency}")
    clean_automation_mode = automation_mode.strip()
    if clean_automation_mode:
        if clean_automation_mode not in AUTOMATION_MODES:
            return result("failed", f"非法自动化模式：{clean_automation_mode}")
    else:
        clean_automation_mode = "execute" if schedule_enabled else "off"
    next_schedule_enabled = clean_automation_mode != "off"
    previous = _find_project(load_projects(config_path), project_id)
    clean_worker_provider = worker_provider.strip()
    clean_worker_settings = worker_settings if isinstance(worker_settings, dict) else {}
    worker_updates: Dict[str, Any] = {}
    if worker_network is not None:
        network_errors: List[str] = []
        clean_worker_network = normalize_worker_network(
            worker_network,
            network_errors,
            fallback=previous.worker_network,
        )
        if network_errors:
            return result("failed", network_errors[0])
        worker_updates["worker_network"] = clean_worker_network
    if clean_worker_provider and clean_worker_provider not in WORKER_PROVIDER_EXECUTORS:
        return result("failed", f"非法 Worker Provider：{clean_worker_provider}")
    if clean_worker_provider and previous.executor != "fake_codex":
        executor = WORKER_PROVIDER_EXECUTORS[clean_worker_provider]
        if clean_worker_provider == "codex":
            clean_codex_model = str(clean_worker_settings.get("model") or previous.codex_model or DEFAULT_CODEX_MODEL).strip()
            clean_reasoning_effort = str(
                clean_worker_settings.get("reasoning_effort")
                or previous.codex_reasoning_effort
                or DEFAULT_CODEX_REASONING_EFFORT
            ).strip()
            if clean_reasoning_effort not in CODEX_REASONING_EFFORTS:
                return result("failed", f"非法 Codex reasoning 级别：{clean_reasoning_effort}")
            clean_codex_sandbox = str(
                clean_worker_settings.get("sandbox") or previous.codex_sandbox or DEFAULT_CODEX_SANDBOX
            ).strip()
            if clean_codex_sandbox not in CODEX_SANDBOXES:
                return result("failed", f"非法 Codex sandbox：{clean_codex_sandbox}")
            worker_updates.update({
                "executor": executor,
                "default_agent": "codex",
                "codex_model": clean_codex_model,
                "codex_reasoning_effort": clean_reasoning_effort,
                "codex_sandbox": clean_codex_sandbox,
            })
        elif clean_worker_provider == "claude":
            clean_claude_model = str(
                clean_worker_settings.get("model") or previous.claude_model or DEFAULT_CLAUDE_MODEL
            ).strip() or DEFAULT_CLAUDE_MODEL
            clean_permission_mode = str(
                clean_worker_settings.get("permission_mode")
                or previous.claude_permission_mode
                or DEFAULT_CLAUDE_PERMISSION_MODE
            ).strip()
            if clean_permission_mode not in CLAUDE_PERMISSION_MODES:
                return result("failed", f"非法 Claude 权限模式：{clean_permission_mode}")
            worker_updates.update({
                "executor": executor,
                "default_agent": "claude",
                "claude_model": clean_claude_model,
                "claude_permission_mode": clean_permission_mode,
            })
    elif not clean_worker_provider:
        clean_codex_model = codex_model.strip() or DEFAULT_CODEX_MODEL
        clean_reasoning_effort = codex_reasoning_effort.strip() or DEFAULT_CODEX_REASONING_EFFORT
        if clean_reasoning_effort not in CODEX_REASONING_EFFORTS:
            return result("failed", f"非法 Codex reasoning 级别：{clean_reasoning_effort}")
        clean_codex_sandbox = codex_sandbox.strip() or DEFAULT_CODEX_SANDBOX
        if clean_codex_sandbox not in CODEX_SANDBOXES:
            return result("failed", f"非法 Codex sandbox：{clean_codex_sandbox}")
        worker_updates.update({
            "codex_model": clean_codex_model,
            "codex_reasoning_effort": clean_reasoning_effort,
            "codex_sandbox": clean_codex_sandbox,
        })
    next_schedule_started_at = previous.schedule_started_at
    if next_schedule_enabled:
        if (
            not previous.schedule_enabled
            or previous.automation_mode == "off"
            or previous.schedule_frequency != clean_frequency
            or not previous.schedule_started_at
        ):
            next_schedule_started_at = utc_now()
    else:
        next_schedule_started_at = None
    updates = {
        "schedule_enabled": next_schedule_enabled,
        "schedule_frequency": clean_frequency,
        "schedule_started_at": next_schedule_started_at,
        "automation_mode": clean_automation_mode,
        "auto_commit": bool(auto_commit),
        **worker_updates,
    }
    update_project_config(project_id, updates, config_path)
    profile = _find_project(load_projects(config_path), project_id)
    event = _write_event(
        profile,
        "runtime_config_updated",
        "运行配置已更新",
        schedule_enabled=profile.schedule_enabled,
        schedule_frequency=profile.schedule_frequency,
        schedule_started_at=profile.schedule_started_at,
        automation_mode=profile.automation_mode,
        auto_commit=profile.auto_commit,
        worker_provider=profile_to_dict(profile)["worker_runtime"]["provider"],
        worker_settings=profile_to_dict(profile)["worker_runtime"]["settings"],
        worker_network_mode=profile.worker_network.get("mode", "inherit"),
        codex_model=profile.codex_model,
        codex_reasoning_effort=profile.codex_reasoning_effort,
        codex_sandbox=profile.codex_sandbox,
    )
    return result("completed", "运行配置已保存，下一轮运行使用新配置", project=_project_payload(profile, _profile_status_payload(profile), _latest_run(profile)), event=event)


def project_detail(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    status = project_status(config_path, project_id)
    runs = read_jsonl(profile.history_path, limit=30)
    latest_run = runs[-1] if runs else None
    return result(
        status["status"],
        status["summary"],
        project=_project_payload(profile, status.get("runner") or {}, latest_run),
        status_payload=status.get("runner"),
        runs=runs,
        clues=list_clues(profile, limit=50),
        events=read_jsonl(profile.events_path, limit=30),
    )


def report_metadata(config_path: Optional[Path], project_id: str) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    latest = project_latest_report(config_path, project_id)
    report = latest.get("report") or {}
    report_path = report.get("report_path")
    exists = bool(report.get("exists"))
    if report_path:
        try:
            safe_path = _safe_report_path(profile, str(report_path))
            exists = safe_path.exists()
        except ValueError:
            exists = False
    return result(
        latest["status"],
        latest["summary"],
        report={
            "exists": exists,
            "run_id": report.get("run_id") or "latest",
            "report_path": report_path,
            "created_at": report.get("created_at"),
            "title": report.get("title") or "最近报告",
        },
    )


def read_report_html(config_path: Optional[Path], project_id: str, run_id: str) -> str:
    profile = _find_project(load_projects(config_path), project_id)
    if run_id == "latest":
        latest = project_latest_report(config_path, project_id).get("report") or {}
        report_path = latest.get("report_path")
    else:
        report_path = str(profile.reports_dir / f"{run_id}.html")
    if not report_path:
        return _empty_report("暂无报告", "项目还没有生成 HTML report。")
    path = _safe_report_path(profile, report_path)
    if not path.exists():
        return _empty_report("报告不存在", "报告路径已登记，但文件不存在。")
    content = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".html", ".htm"}:
        return content
    return _text_report(path.name, content)


def _safe_report_path(profile: ProjectProfile, report_path: str) -> Path:
    allowed_bases = [profile.reports_dir.resolve(), profile.loopforge_dir.resolve()]
    raw_path = Path(report_path).expanduser()
    if raw_path.is_absolute():
        path = raw_path.resolve()
    else:
        direct_path = raw_path.resolve()
        if _path_allowed(direct_path, allowed_bases):
            path = direct_path
        else:
            path = (profile.root_dir / raw_path).resolve()
    if not any(path == base or base in path.parents for base in allowed_bases):
        raise ValueError("report 路径越界")
    return path


def _path_allowed(path: Path, allowed_bases: List[Path]) -> bool:
    return any(path == base or base in path.parents for base in allowed_bases)


def _text_report(title: str, content: str) -> str:
    safe_title = html.escape(title)
    safe_content = html.escape(content)
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<meta charset=\"utf-8\">
<title>{safe_title}</title>
<body style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 32px; color: #1f2937; line-height: 1.65;\">
<h1>{safe_title}</h1>
<pre style=\"white-space: pre-wrap; background: #f8fafc; border: 1px solid #e2e8f0; padding: 16px; border-radius: 8px;\">{safe_content}</pre>
</body>
</html>"""


def _empty_report(title: str, message: str) -> str:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    return f"""<!doctype html>
<html lang=\"zh-CN\">
<meta charset=\"utf-8\">
<title>{safe_title}</title>
<body style=\"font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 32px; color: #1f2937;\">
<h1>{safe_title}</h1>
<p>{safe_message}</p>
</body>
</html>"""


def notify_placeholder(config_path: Optional[Path], project_id: str, manual_resend: bool = False) -> Dict[str, Any]:
    profile = _find_project(load_projects(config_path), project_id)
    if not profile.is_valid:
        return result("misconfigured", "项目配置错误", required_action="；".join(profile.errors))
    notification = send_test_notification(profile, manual_resend=manual_resend)
    sent = notification.get("status") == "sent"
    summary = "通知已发送" if sent else "通知发送失败" if notification.get("status") == "failed" else "通知已跳过"
    event = _write_event(
        profile,
        "manual_notify_resend" if manual_resend else "notification_test",
        summary,
        manual_resend=manual_resend,
        notification=notification,
    )
    status = "completed" if notification.get("status") in {"sent", "skipped"} else "failed"
    return result(status, summary, event=event, notification=notification)


def schedule_tick(
    config_path: Optional[Path],
    respect_frequency: bool = False,
    run_project: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    profiles = load_projects(config_path)
    statuses: List[Any] = []
    for profile in profiles:
        status = _profile_status_payload(profile)
        if respect_frequency:
            try:
                runs = read_jsonl(profile.history_path, limit=1)
                status["last_run"] = runs[-1] if runs else None
            except OSError:
                status["last_run"] = None
        statuses.append((profile, status))
    chosen = choose_project_loop(statuses, respect_frequency=respect_frequency)
    if not chosen:
        return result("skipped_no_task", "没有可调度项目")
    chosen_project, loop_type = chosen
    execute = run_project or project_run_once
    run = execute(config_path, chosen_project.project_id, trigger="schedule", loop_type=loop_type)
    return result(
        run["status"],
        f"已调度项目：{chosen_project.project_id}（{loop_type}）",
        project_id=chosen_project.project_id,
        loop_type=loop_type,
        run=run,
    )


def blocker_fingerprint(project_id: str, task_id: str, blocker_type: str, required_action: str) -> str:
    raw = "|".join([project_id, task_id, blocker_type, required_action])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
