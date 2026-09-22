"""通知发送实现。"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .config import ProjectProfile
from .domain import label_status, utc_now
from .global_config import wecom_settings


NOTIFIABLE_RUN_STATUSES = {
    "completed",
    "blocked",
    "failed",
    "misconfigured",
    "timeout_continue",
    "timeout_blocked",
}
NOTIFIABLE_EVENTS = {
    "spec_blocked",
    "dev_blocked",
    "ready_for_review",
    "merge_failed",
    "cleanup_failed",
    "scan_budget_exceeded_repeated",
    "ready_for_review_limit_reached",
    "completed",
    "failed",
    "misconfigured",
    "timeout_continue",
    "timeout_blocked",
}
NON_NOTIFY_OUTCOMES = {"no_op", "normal_poll", "auto_fix_success", "weak_clue_open"}


def send_test_notification(profile: ProjectProfile, manual_resend: bool = False) -> Dict[str, Any]:
    title = "LoopForge 手动重发测试" if manual_resend else "LoopForge 通知测试"
    content = "\n".join(
        [
            f"**{title}**",
            f"> 项目：{profile.name}（{profile.project_id}）",
            f"> 时间：{utc_now()}",
            "> 结果：通知链路已触发",
        ]
    )
    return send_notification(profile, content)


def send_run_notification(profile: ProjectProfile, record: Dict[str, Any]) -> Dict[str, Any]:
    status = str(record.get("status") or "")
    event = notification_event(record)
    if not event:
        return {
            "status": "skipped",
            "channel": _effective_channel(profile),
            "reason": f"运行状态 {status or 'unknown'} 不触发通知",
        }

    task_line = record.get("task_title") or record.get("task_id") or "无任务信息"
    required_action = record.get("required_action") or "无"
    content = "\n".join(
        [
            f"**LoopForge 运行{label_status(status)}**",
            f"> 项目：{profile.name}（{profile.project_id}）",
            f"> 任务：{task_line}",
            f"> 状态：{label_status(status)}",
            f"> 事件：{event}",
            f"> 摘要：{record.get('summary') or '无摘要'}",
            f"> 处理建议：{required_action}",
            f"> run_id：{record.get('run_id') or 'unknown'}",
        ]
    )
    return send_notification(profile, content)


def notification_event(record: Dict[str, Any]) -> str:
    explicit = str(record.get("notification_event") or "").strip()
    if explicit:
        return explicit if explicit in NOTIFIABLE_EVENTS else ""
    outcome = str(record.get("outcome") or "").strip()
    if outcome in NON_NOTIFY_OUTCOMES:
        return ""
    blocker_type = str(record.get("blocker_type") or "").strip()
    if blocker_type in {"spec_blocked", "prd_blocked"}:
        return "spec_blocked"
    if blocker_type in {"dev_blocked", "blocked"}:
        return "dev_blocked"
    task_state = str(record.get("task_state") or record.get("next_state") or "").strip()
    if task_state in {"spec_blocked", "prd_blocked"}:
        return "spec_blocked"
    if task_state in {"dev_blocked", "blocked"}:
        return "dev_blocked"
    if task_state == "ready_for_review":
        return "ready_for_review"
    status = str(record.get("status") or "").strip()
    if status in {"failed", "misconfigured", "timeout_continue", "timeout_blocked"}:
        return status
    if status == "blocked":
        return blocker_type if blocker_type in NOTIFIABLE_EVENTS else "dev_blocked"
    if status == "completed" and str(record.get("loop_type") or "") != "scan":
        return "completed"
    return ""


def send_notification(profile: ProjectProfile, markdown_content: str, timeout_seconds: float = 6) -> Dict[str, Any]:
    settings = wecom_settings(profile.global_config or {})
    if settings["enabled"]:
        webhook_url, source_or_error = _resolve_global_wecom_webhook(settings)
        if not webhook_url:
            return {"status": "failed", "channel": "wecom_robot", "error": source_or_error}
        return _post_wecom_markdown(webhook_url, markdown_content, source_or_error, timeout_seconds)

    if profile.notification_channel == "none":
        return {"status": "skipped", "channel": "none", "reason": "通知渠道为 none"}
    if profile.notification_channel != "wecom_robot":
        return {
            "status": "failed",
            "channel": profile.notification_channel,
            "error": f"不支持的通知渠道：{profile.notification_channel}",
        }

    webhook_url, source_or_error = _resolve_wecom_webhook(profile)
    if not webhook_url:
        return {"status": "failed", "channel": "wecom_robot", "error": source_or_error}

    return _post_wecom_markdown(webhook_url, markdown_content, source_or_error, timeout_seconds)


def _resolve_wecom_webhook(profile: ProjectProfile) -> Tuple[Optional[str], str]:
    if profile.webhook_url:
        return profile.webhook_url, "webhook_url"
    if profile.webhook_env:
        value = os.environ.get(profile.webhook_env)
        if value:
            return value, f"env:{profile.webhook_env}"
        return None, f"环境变量未设置：{profile.webhook_env}"
    return None, "wecom_robot 缺少 webhook_url 或 webhook_env"


def _resolve_global_wecom_webhook(settings: Dict[str, Any]) -> Tuple[Optional[str], str]:
    webhook_url = str(settings.get("webhook_url") or "").strip()
    if webhook_url:
        return webhook_url, "global:webhook_url"
    webhook_env = str(settings.get("webhook_env") or "LOOPFORGE_WECOM_WEBHOOK").strip() or "LOOPFORGE_WECOM_WEBHOOK"
    value = os.environ.get(webhook_env)
    if value:
        return value, f"global:env:{webhook_env}"
    return None, f"全局企微环境变量未设置：{webhook_env}"


def _effective_channel(profile: ProjectProfile) -> str:
    settings = wecom_settings(profile.global_config or {})
    if settings["enabled"]:
        return "wecom_robot"
    return profile.notification_channel


def _post_wecom_markdown(webhook_url: str, markdown_content: str, source: str, timeout_seconds: float) -> Dict[str, Any]:
    payload = {"msgtype": "markdown", "markdown": {"content": markdown_content}}
    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status_code = response.getcode()
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "status": "failed",
            "channel": "wecom_robot",
            "source": source,
            "http_status": exc.code,
            "error": f"企微 webhook HTTP {exc.code}",
            "response": _shorten(body),
        }
    except urllib.error.URLError as exc:
        return {
            "status": "failed",
            "channel": "wecom_robot",
            "source": source,
            "error": f"企微 webhook 请求失败：{exc.reason}",
        }
    except TimeoutError:
        return {
            "status": "failed",
            "channel": "wecom_robot",
            "source": source,
            "error": "企微 webhook 请求超时",
        }
    except (OSError, ValueError) as exc:
        return {
            "status": "failed",
            "channel": "wecom_robot",
            "source": source,
            "error": f"企微 webhook 请求失败：{exc}",
        }

    parsed = _parse_json(body)
    if isinstance(parsed, dict) and parsed.get("errcode") not in (None, 0):
        return {
            "status": "failed",
            "channel": "wecom_robot",
            "source": source,
            "http_status": status_code,
            "error": str(parsed.get("errmsg") or f"企微 errcode={parsed.get('errcode')}"),
            "response": _shorten(body),
        }

    return {
        "status": "sent",
        "channel": "wecom_robot",
        "source": source,
        "http_status": status_code,
        "sent_at": utc_now(),
        "response": _shorten(body),
    }


def _parse_json(body: str) -> Any:
    try:
        return json.loads(body) if body.strip() else None
    except json.JSONDecodeError:
        return None


def _shorten(value: str, limit: int = 300) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
