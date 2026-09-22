"""协议枚举、中文文案和通用结果工具。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict


PROJECT_STATUSES = {
    "idle",
    "running",
    "paused",
    "blocked",
    "timeout",
    "completed",
    "skipped",
    "failed",
    "misconfigured",
}

RUN_STATUSES = {
    "completed",
    "blocked",
    "timeout_continue",
    "timeout_blocked",
    "skipped_no_task",
    "skipped_paused",
    "skipped_already_running",
    "skipped_no_running",
    "skipped_not_implemented",
    "cancel_requested",
    "cancelled",
    "failed",
    "misconfigured",
}

BLOCKER_TYPES = {
    "dirty_worktree",
    "active_task_exists",
    "external_task_in_progress",
    "prd_blocked",
    "check_failed_after_2_fixes",
    "final_gate_failed",
    "environment_required",
    "user_decision_required",
    "commit_isolation_failed",
    "non_terminal_task",
    "stale_task",
    "timeout_blocked",
    "runner_misconfigured",
    "unknown_blocker",
}

NOTIFICATION_CHANNELS = {"none", "wecom_robot"}

SCHEDULE_FREQUENCIES = {
    "half_hourly": 30 * 60,
    "hourly": 60 * 60,
    "daily": 24 * 60 * 60,
    "weekly": 7 * 24 * 60 * 60,
}

SCHEDULE_FREQUENCY_LABELS = {
    "half_hourly": "每半小时",
    "hourly": "每小时",
    "daily": "每天",
    "weekly": "每周",
}

AUTOMATION_MODES = {"off", "observe", "execute"}

AUTOMATION_MODE_LABELS = {
    "off": "关闭",
    "observe": "观察模式",
    "execute": "执行模式",
}

AUTOMATION_MODE_DESCRIPTIONS = {
    "off": "不参与后台自动调度，仍可手动运行。",
    "observe": "只允许后台自动项目巡检，不自动改代码。",
    "execute": "允许后台自动巡检和开发执行。",
}

AUTOMATION_MODE_ALLOWED_LOOPS = {
    "off": set(),
    "observe": {"scan"},
    "execute": {"scan", "dev"},
}

# 任务状态是后端状态机、Task Store、CLI 和前端投影共用的协议。
TASK_STATES = {
    "open",
    "claimed",
    "spec_ready",
    "spec_blocked",
    "prd_ready",
    "prd_blocked",
    "coding",
    "dev_blocked",
    "ready_for_review",
    "accepted",
    "merged",
    "blocked",
    "completed",
    "abandoned",
}

TERMINAL_TASK_STATES = {"completed", "abandoned"}

# 只描述受 LoopForge 控制的持久状态跃迁。保持原状态的字段更新由 Task Store 另行允许。
TASK_STATE_TRANSITIONS = {
    "open": {"claimed", "spec_ready", "prd_ready", "abandoned"},
    "claimed": {"spec_ready", "prd_ready", "ready_for_review", "dev_blocked", "blocked", "abandoned"},
    "spec_ready": {"coding", "spec_blocked", "dev_blocked", "ready_for_review", "blocked", "abandoned"},
    "spec_blocked": {"spec_ready", "coding", "abandoned"},
    "prd_ready": {"coding", "prd_blocked", "dev_blocked", "ready_for_review", "blocked", "abandoned"},
    "prd_blocked": {"prd_ready", "coding", "abandoned"},
    "coding": {"ready_for_review", "spec_blocked", "dev_blocked", "blocked", "completed", "abandoned"},
    "dev_blocked": {"coding", "abandoned"},
    "blocked": {"coding", "abandoned"},
    "ready_for_review": {"coding", "spec_blocked", "dev_blocked", "accepted", "abandoned"},
    "accepted": {"ready_for_review", "merged", "blocked", "abandoned"},
    "merged": {"completed", "blocked"},
    "completed": set(),
    "abandoned": set(),
}

PROJECT_STATUS_LABELS = {
    "idle": "空闲",
    "running": "运行中",
    "paused": "已暂停",
    "blocked": "阻塞",
    "timeout": "超时",
    "completed": "已完成",
    "skipped": "已跳过",
    "failed": "失败",
    "misconfigured": "配置错误",
}

RUN_STATUS_LABELS = {
    "completed": "已完成",
    "blocked": "阻塞",
    "timeout_continue": "超时可继续",
    "timeout_blocked": "超时阻塞",
    "skipped_no_task": "无任务跳过",
    "skipped_paused": "暂停跳过",
    "skipped_already_running": "已有运行跳过",
    "skipped_no_running": "无运行跳过",
    "skipped_not_implemented": "未实现跳过",
    "cancel_requested": "取消请求已记录",
    "cancelled": "已取消",
    "failed": "失败",
    "misconfigured": "配置错误",
}


def utc_now() -> str:
    """返回 UTC ISO 时间，统一用于协议记录。"""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def label_status(status: str) -> str:
    return RUN_STATUS_LABELS.get(status) or PROJECT_STATUS_LABELS.get(status) or status


def result(status: str, summary: str, **extra: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "status": status,
        "status_label": label_status(status),
        "summary": summary,
    }
    payload.update(extra)
    return payload
