"""Worker Provider 的命令、输出与运行元数据适配。"""

from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from .config import ProjectProfile, worker_provider_for_executor


PROXY_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")


def build_worker_environment(profile: ProjectProfile, base_env: Dict[str, str] | None = None) -> Dict[str, str]:
    """构造仅作用于 Worker 子进程的代理环境。"""

    environment = dict(os.environ if base_env is None else base_env)
    network = profile.worker_network or {}
    mode = str(network.get("mode") or "inherit")
    if mode == "inherit":
        return environment
    for name in PROXY_ENV_NAMES:
        environment.pop(name, None)
    if mode == "direct":
        return environment

    aliases = {
        "http_proxy": ("HTTP_PROXY", "http_proxy"),
        "https_proxy": ("HTTPS_PROXY", "https_proxy"),
        "all_proxy": ("ALL_PROXY", "all_proxy"),
    }
    for key, names in aliases.items():
        value = str(network.get(key) or "").strip()
        if value:
            for name in names:
                environment[name] = value
    return environment


@dataclass(frozen=True)
class WorkerProcessOutcome:
    status: str
    summary: str
    last_message: str = ""
    session_id: str = ""
    provider_usage: Dict[str, Any] = field(default_factory=dict)
    reported_cost_usd: float | None = None
    stderr: str = ""
    required_action: str = ""
    error_detail: str = ""


def provider_label(profile: ProjectProfile) -> str:
    provider = worker_provider_for_executor(profile.executor)
    if provider == "codex":
        return "Codex"
    if provider == "claude":
        return "Claude"
    if provider == "fake":
        return "演示"
    return profile.executor or "未知"


def request_metadata(profile: ProjectProfile) -> Dict[str, Any]:
    provider = worker_provider_for_executor(profile.executor)
    if provider == "codex":
        return {
            "worker_provider": "codex",
            "worker_model": profile.codex_model,
            "worker_settings": {
                "reasoning_effort": profile.codex_reasoning_effort,
                "sandbox": profile.codex_sandbox,
            },
        }
    if provider == "claude":
        return {
            "worker_provider": "claude",
            "worker_model": profile.claude_model,
            "worker_settings": {"permission_mode": profile.claude_permission_mode},
        }
    return {"worker_provider": "fake", "worker_model": "", "worker_settings": {}}


def build_worker_command(
    profile: ProjectProfile,
    execution_root: str,
    *,
    mode: str = "development",
    binary: str = "",
    last_message_path: Path | None = None,
) -> List[str]:
    if profile.executor == "codex_cli":
        command = [binary or "codex", "exec", "--cd", execution_root or str(profile.root_dir)]
        if profile.codex_model:
            command.extend(["--model", profile.codex_model])
        if profile.codex_reasoning_effort:
            command.extend(["-c", f'model_reasoning_effort="{profile.codex_reasoning_effort}"'])
        sandbox = "read-only" if mode == "readonly" else profile.codex_sandbox
        command.extend(
            [
                "-c",
                'approval_policy="never"',
                "--sandbox",
                sandbox,
                "--json",
                "--output-last-message",
                str(last_message_path or Path("last-message.md")),
                "-",
            ]
        )
        return command
    if profile.executor == "claude_cli":
        command = [
            binary or "claude",
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
        ]
        if profile.claude_model:
            command.extend(["--model", profile.claude_model])
        if mode == "readonly":
            command.extend(["--permission-mode", "plan", "--tools", ""])
        else:
            command.extend(["--permission-mode", profile.claude_permission_mode])
        return command
    raise ValueError(f"不支持的 Worker executor：{profile.executor}")


def parse_worker_output(executor: str, stdout: str, stderr: str, returncode: int) -> WorkerProcessOutcome:
    if executor == "codex_cli":
        if returncode == 0:
            return WorkerProcessOutcome(status="completed", summary="codex_cli 执行完成", stderr=(stderr or "").strip())
        return WorkerProcessOutcome(
            status="failed",
            summary=_process_failure_summary(executor, returncode, stderr),
            stderr=(stderr or "").strip(),
        )
    if executor != "claude_cli":
        return WorkerProcessOutcome(status="failed", summary=f"不支持的 Worker executor：{executor}")
    return _parse_claude_output(stdout, stderr, returncode)


def _parse_claude_output(stdout: str, stderr: str, returncode: int) -> WorkerProcessOutcome:
    clean_stderr = (stderr or "").strip()
    events: List[Dict[str, Any]] = []
    for line in (stdout or "").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return WorkerProcessOutcome(
                status="failed",
                summary="claude_cli 执行失败：stream-json 包含非法 JSON",
                stderr=clean_stderr,
                error_detail=line[:240],
            )
        if not isinstance(payload, dict):
            return WorkerProcessOutcome(
                status="failed",
                summary="claude_cli 执行失败：stream-json 事件不是 JSON object",
                stderr=clean_stderr,
            )
        events.append(payload)

    final = next((event for event in reversed(events) if event.get("type") == "result"), None)
    if final is None and returncode != 0:
        return WorkerProcessOutcome(
            status="failed",
            summary=_process_failure_summary("claude_cli", returncode, clean_stderr),
            stderr=clean_stderr,
        )
    if final is None:
        return WorkerProcessOutcome(
            status="failed",
            summary="claude_cli 执行失败：未收到最终 result 事件",
            stderr=clean_stderr,
        )

    message = str(final.get("result") or "").strip()
    session_id = str(final.get("session_id") or "").strip()
    usage = final.get("usage") if isinstance(final.get("usage"), dict) else {}
    raw_cost = final.get("total_cost_usd")
    cost = (
        float(raw_cost)
        if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool) and raw_cost >= 0
        else None
    )
    combined = " ".join([message, clean_stderr]).lower()
    if any(marker in combined for marker in ("invalid api key", "authentication", "authenticate", "/login")):
        return WorkerProcessOutcome(
            status="failed",
            summary="claude_cli 执行失败：Claude Code 认证失败",
            last_message=message,
            session_id=session_id,
            provider_usage=dict(usage),
            reported_cost_usd=cost,
            stderr=clean_stderr,
            required_action="请运行 claude 完成登录，或检查环境/企业 Provider 的认证配置后重试。",
            error_detail=message,
        )
    if returncode != 0:
        return WorkerProcessOutcome(
            status="failed",
            summary=_process_failure_summary("claude_cli", returncode, clean_stderr or message),
            last_message=message,
            session_id=session_id,
            provider_usage=dict(usage),
            reported_cost_usd=cost,
            stderr=clean_stderr,
            error_detail=message,
        )
    if final.get("is_error") is True or str(final.get("subtype") or "") != "success":
        return WorkerProcessOutcome(
            status="failed",
            summary=f"claude_cli 执行失败：{message or final.get('subtype') or '未知错误'}",
            last_message=message,
            session_id=session_id,
            provider_usage=dict(usage),
            reported_cost_usd=cost,
            stderr=clean_stderr,
            error_detail=message,
        )
    return WorkerProcessOutcome(
        status="completed",
        summary="claude_cli 执行完成",
        last_message=message,
        session_id=session_id,
        provider_usage=dict(usage),
        reported_cost_usd=cost,
        stderr=clean_stderr,
    )


def _process_failure_summary(executor: str, returncode: int, stderr: str) -> str:
    clean_stderr = (stderr or "").strip()
    if clean_stderr:
        return f"{executor} 执行失败：{clean_stderr.splitlines()[0].strip()}"
    if returncode < 0:
        try:
            signal_name = signal.Signals(-returncode).name
        except ValueError:
            signal_name = f"SIG{-returncode}"
        return f"{executor} 被系统信号终止：{signal_name}"
    return f"{executor} 执行失败：exit_code={returncode}"
