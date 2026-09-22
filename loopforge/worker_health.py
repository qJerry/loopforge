"""本机 Worker CLI 的无模型调用健康检测。"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List

from .config import ProjectProfile
from .executor import resolve_claude_binary, resolve_codex_binary
from .worker_adapters import build_worker_environment


DEFAULT_PROBE_TIMEOUT_SECONDS = 15
ProbeRunner = Callable[..., subprocess.CompletedProcess[str]]


def detect_worker_health(
    *,
    profile: ProjectProfile | None = None,
    timeout_seconds: int = DEFAULT_PROBE_TIMEOUT_SECONDS,
    runner: ProbeRunner = subprocess.run,
) -> Dict[str, Any]:
    """检测所有受支持 Worker，不执行模型请求。"""

    checked_at = datetime.now(timezone.utc).isoformat()
    environment = build_worker_environment(profile) if profile is not None else None
    providers = {
        "codex": _detect_provider(
            "codex",
            resolve_codex_binary(),
            ["--version"],
            ["login", "status"],
            checked_at,
            timeout_seconds,
            runner,
            environment,
        ),
        "claude": _detect_provider(
            "claude",
            resolve_claude_binary(),
            ["--version"],
            ["auth", "status"],
            checked_at,
            timeout_seconds,
            runner,
            environment,
        ),
    }
    return {
        "status": "completed",
        "summary": "Worker 环境检测完成",
        "checked_at": checked_at,
        "providers": providers,
    }


def _detect_provider(
    provider: str,
    binary: str,
    version_args: List[str],
    auth_args: List[str],
    checked_at: str,
    timeout_seconds: int,
    runner: ProbeRunner,
    environment: Dict[str, str] | None,
) -> Dict[str, Any]:
    resolved_path = _resolved_binary_path(binary)
    base = {
        "provider": provider,
        "ready": False,
        "status": "error",
        "binary_path": resolved_path,
        "version": "",
        "authenticated": False,
        "auth_method": "",
        "issue": "",
        "required_action": "",
        "checked_at": checked_at,
    }
    try:
        version_kwargs = {
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": max(1, int(timeout_seconds)),
        }
        if environment is not None:
            version_kwargs["env"] = environment
        version = runner(
            [binary, *version_args],
            **version_kwargs,
        )
    except FileNotFoundError:
        return {
            **base,
            "status": "missing",
            "issue": f"找不到 {provider} CLI 可执行文件",
            "required_action": _install_action(provider),
        }
    except subprocess.TimeoutExpired:
        return {
            **base,
            "issue": f"{provider} CLI 版本检测超时",
            "required_action": "请检查 CLI 安装是否正常，并重新检测。",
        }
    except OSError:
        return {
            **base,
            "issue": f"无法启动 {provider} CLI",
            "required_action": "请检查 CLI 文件权限与安装路径，并重新检测。",
        }

    if version.returncode != 0:
        return {
            **base,
            "issue": f"{provider} CLI 版本检测失败",
            "required_action": "请在终端运行 CLI 的 --version 命令检查安装。",
        }

    version_text = _first_safe_line(version.stdout) or _first_safe_line(version.stderr)
    if not version_text:
        return {
            **base,
            "issue": f"{provider} CLI 未返回版本信息",
            "required_action": "请升级或重新安装 CLI 后再检测。",
        }
    base["version"] = version_text

    try:
        auth = runner(
            [binary, *auth_args],
            **version_kwargs,
        )
    except subprocess.TimeoutExpired:
        return {
            **base,
            "issue": f"{provider} CLI 登录状态检测超时",
            "required_action": "请在终端确认 CLI 登录状态后重新检测。",
        }
    except OSError:
        return {
            **base,
            "issue": f"无法检测 {provider} CLI 登录状态",
            "required_action": "请检查 CLI 配置文件权限后重新检测。",
        }

    auth_state = _parse_codex_auth(auth) if provider == "codex" else _parse_claude_auth(auth)
    if auth_state["authenticated"]:
        if provider == "claude":
            service_issue = _probe_claude_service(binary, timeout_seconds, runner, environment)
            if service_issue:
                return {**base, **auth_state, **service_issue}
        return {
            **base,
            **auth_state,
            "ready": True,
            "status": "ready",
        }
    return {**base, **auth_state}


def _probe_claude_service(
    binary: str,
    timeout_seconds: int,
    runner: ProbeRunner,
    environment: Dict[str, str] | None,
) -> Dict[str, Any] | None:
    """使用 doctor 的无模型请求检查服务/组织策略是否可达。"""

    try:
        probe_kwargs = {
            "check": False,
            "capture_output": True,
            "text": True,
            "timeout": max(1, int(timeout_seconds)),
        }
        if environment is not None:
            probe_kwargs["env"] = environment
        process = runner(
            [binary, "doctor"],
            **probe_kwargs,
        )
    except subprocess.TimeoutExpired:
        return {
            "ready": False,
            "status": "error",
            "issue": "Claude Code 服务与组织策略检测超时",
            "required_action": "请检查网络、代理或组织策略后重新检测。",
        }
    except OSError:
        return {
            "ready": False,
            "status": "error",
            "issue": "无法运行 Claude Code doctor 检测",
            "required_action": "请在终端运行 claude doctor 检查安装与服务连接。",
        }

    combined = "\n".join(part for part in (process.stdout, process.stderr) if part).strip().lower()
    if any(marker in combined for marker in ("refused the request", "http 403", "request not allowed")):
        return {
            "ready": False,
            "status": "error",
            "issue": "Claude Code 服务访问或组织策略请求被拒绝（HTTP 403）",
            "required_action": "请检查代理是否放行 api.anthropic.com，或联系组织管理员确认本机 IP 与 Claude Code 使用权限。",
        }
    if process.returncode != 0:
        return {
            "ready": False,
            "status": "error",
            "issue": "Claude Code 服务与组织策略检测失败",
            "required_action": "请在终端运行 claude doctor 查看并处理诊断结果。",
        }
    return None


def _parse_codex_auth(process: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    combined = "\n".join(part for part in (process.stdout, process.stderr) if part).strip().lower()
    if process.returncode == 0 and "logged in" in combined:
        method = "chatgpt" if "chatgpt" in combined else "configured"
        return {"authenticated": True, "auth_method": method, "issue": "", "required_action": ""}
    if any(marker in combined for marker in ("not logged in", "please run", "login")):
        return {
            "status": "unauthenticated",
            "authenticated": False,
            "auth_method": "",
            "issue": "Codex CLI 尚未登录",
            "required_action": "请运行 codex login 完成登录后重新检测。",
        }
    return {
        "status": "error",
        "authenticated": False,
        "auth_method": "",
        "issue": "无法确认 Codex CLI 登录状态",
        "required_action": "请在终端运行 codex login status 检查登录状态。",
    }


def _parse_claude_auth(process: subprocess.CompletedProcess[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] | None = None
    if process.stdout.strip():
        try:
            decoded = json.loads(process.stdout)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            payload = decoded
    if payload is not None:
        logged_in = payload.get("loggedIn") is True
        if logged_in and process.returncode == 0:
            return {
                "authenticated": True,
                "auth_method": _safe_auth_method(payload.get("authMethod")),
                "issue": "",
                "required_action": "",
            }
        if payload.get("loggedIn") is False:
            return {
                "status": "unauthenticated",
                "authenticated": False,
                "auth_method": "",
                "issue": "Claude Code CLI 尚未登录",
                "required_action": "请运行 claude 并执行 /login，完成登录后重新检测。",
            }

    combined = "\n".join(part for part in (process.stdout, process.stderr) if part).strip().lower()
    if any(marker in combined for marker in ("invalid api key", "not logged in", "please run /login", "authenticate")):
        return {
            "status": "unauthenticated",
            "authenticated": False,
            "auth_method": "",
            "issue": "Claude Code CLI 认证无效或尚未登录",
            "required_action": "请运行 claude 并执行 /login，或检查企业 Provider 认证配置。",
        }
    return {
        "status": "error",
        "authenticated": False,
        "auth_method": "",
        "issue": "无法确认 Claude Code CLI 登录状态",
        "required_action": "请在终端运行 claude auth status 检查登录状态。",
    }


def _resolved_binary_path(binary: str) -> str:
    resolved = shutil.which(binary)
    if resolved:
        return str(Path(resolved).resolve())
    path = Path(binary).expanduser()
    return str(path.resolve()) if path.is_absolute() else binary


def _first_safe_line(value: str) -> str:
    for line in (value or "").splitlines():
        clean = line.strip()
        if clean:
            return clean[:160]
    return ""


def _safe_auth_method(value: Any) -> str:
    clean = str(value or "").strip().lower()
    return clean if clean in {"claude.ai", "api_key", "bedrock", "vertex"} else "configured"


def _install_action(provider: str) -> str:
    if provider == "codex":
        return "请安装 Codex CLI，或设置 LOOPFORGE_CODEX_BIN 指向可执行文件。"
    return "请安装 Claude Code CLI，或设置 LOOPFORGE_CLAUDE_BIN 指向可执行文件。"
