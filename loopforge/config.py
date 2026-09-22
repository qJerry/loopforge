"""项目注册配置加载与校验。"""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from .domain import (
    AUTOMATION_MODE_ALLOWED_LOOPS,
    AUTOMATION_MODE_DESCRIPTIONS,
    AUTOMATION_MODE_LABELS,
    AUTOMATION_MODES,
    NOTIFICATION_CHANNELS,
    SCHEDULE_FREQUENCIES,
    SCHEDULE_FREQUENCY_LABELS,
)
from .global_config import read_global_config


DEFAULT_CONFIG_PATH = Path("~/.config/loopforge/projects.json").expanduser()
# codex_model is free text; these are just starter suggestions for the UI dropdown.
CODEX_MODEL_OPTIONS = [
    {"slug": "gpt-5-codex", "display_name": "GPT-5-Codex"},
]
DEFAULT_CODEX_MODEL = CODEX_MODEL_OPTIONS[0]["slug"]
DEFAULT_CODEX_REASONING_EFFORT = "high"
CODEX_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
DEFAULT_CODEX_SANDBOX = "workspace-write"
CODEX_SANDBOXES = {"read-only", "workspace-write", "danger-full-access"}
CLAUDE_MODEL_OPTIONS = [
    {"slug": "sonnet", "display_name": "Sonnet"},
    {"slug": "opus", "display_name": "Opus"},
    {"slug": "haiku", "display_name": "Haiku"},
]
DEFAULT_CLAUDE_MODEL = CLAUDE_MODEL_OPTIONS[0]["slug"]
DEFAULT_CLAUDE_PERMISSION_MODE = "bypassPermissions"
CLAUDE_PERMISSION_MODES = {"bypassPermissions", "acceptEdits"}
WORKER_PROXY_MODES = {"custom", "inherit", "direct"}
DEFAULT_WORKER_NETWORK = {
    "mode": "inherit",
    "http_proxy": "",
    "https_proxy": "",
    "all_proxy": "",
}
WORKER_EXECUTORS = {"codex_cli", "claude_cli", "fake_codex"}
WORKER_PROVIDER_OPTIONS = [
    {"value": "codex", "label": "Codex", "executor": "codex_cli"},
    {"value": "claude", "label": "Claude", "executor": "claude_cli"},
]
WORKER_PROVIDER_EXECUTORS = {item["value"]: item["executor"] for item in WORKER_PROVIDER_OPTIONS}
TRACKED_PROJECT_CONFIG = Path(".loopforge/project.json")
TRACKED_PROJECT_CONFIG_FIELDS = {"schema_version", "project_type"}


def default_worker_network() -> Dict[str, str]:
    return dict(DEFAULT_WORKER_NETWORK)


def normalize_worker_network(
    value: Any,
    errors: Optional[List[str]] = None,
    *,
    fallback: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """规范化项目级 Worker 代理配置，不允许在 URL 中持久化凭据。"""

    issues = errors if errors is not None else []
    base = default_worker_network()
    if isinstance(fallback, dict):
        for key in base:
            if key in fallback:
                base[key] = str(fallback.get(key) or "").strip()
    if value in (None, ""):
        return base
    if not isinstance(value, dict):
        issues.append("worker_network 必须是对象")
        return base

    normalized = {
        key: str(value.get(key) if key in value else base[key] or "").strip()
        for key in base
    }
    mode = normalized["mode"]
    if mode not in WORKER_PROXY_MODES:
        issues.append(f"非法 Worker 代理模式：{mode}")
        normalized["mode"] = base["mode"]
        return normalized
    if mode != "custom":
        return normalized

    allowed_schemes = {
        "http_proxy": {"http", "https"},
        "https_proxy": {"http", "https"},
        "all_proxy": {"http", "https", "socks5", "socks5h"},
    }
    if not any(normalized[key] for key in allowed_schemes):
        issues.append("自定义代理至少需要填写一个代理地址")
    for key, schemes in allowed_schemes.items():
        proxy_url = normalized[key]
        if not proxy_url:
            continue
        parsed = urlsplit(proxy_url)
        if parsed.scheme.lower() not in schemes or not parsed.hostname:
            issues.append(f"{key} 不是有效代理地址")
            continue
        if parsed.username is not None or parsed.password is not None:
            issues.append("代理地址不能包含用户名或密码")
    return normalized


@dataclass
class ProjectProfile:
    project_id: str
    name: str
    root_dir: Path
    runner_command: List[str] = field(default_factory=list)
    enabled: bool = True
    schedule_enabled: bool = True
    schedule_frequency: str = "hourly"
    schedule_started_at: Optional[str] = None
    automation_mode: str = "execute"
    priority: int = 100
    notification_channel: str = "wecom_robot"
    default_agent: str = "codex"
    executor: str = "codex_cli"
    codex_model: str = DEFAULT_CODEX_MODEL
    codex_reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT
    codex_sandbox: str = DEFAULT_CODEX_SANDBOX
    claude_model: str = DEFAULT_CLAUDE_MODEL
    claude_permission_mode: str = DEFAULT_CLAUDE_PERMISSION_MODE
    worker_network: Dict[str, str] = field(default_factory=default_worker_network)
    auto_commit: bool = False
    worktree_managed: bool = False
    project_type: str = ""
    planning_adapter: str = ""
    project_group: Dict[str, Any] = field(default_factory=dict)
    global_config: Dict[str, Any] = field(default_factory=dict)
    webhook_env: Optional[str] = None
    webhook_url: Optional[str] = None
    state_dir: Optional[Path] = None
    report_dir: Optional[Path] = None
    errors: List[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def loopforge_dir(self) -> Path:
        return self.state_dir or (self.root_dir / ".loopforge")

    @property
    def history_path(self) -> Path:
        return self.loopforge_dir / "index.jsonl"

    @property
    def events_path(self) -> Path:
        return self.loopforge_dir / "events.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.loopforge_dir / "lock.json"

    @property
    def reports_dir(self) -> Path:
        return self.report_dir or (self.loopforge_dir / "reports")


def default_config_path() -> Path:
    configured = os.environ.get("LOOPFORGE_CONFIG")
    return Path(configured).expanduser() if configured else DEFAULT_CONFIG_PATH


def _as_command(value: Any) -> List[str]:
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return value
    if isinstance(value, str):
        return shlex.split(value)
    return []


def _as_bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _as_schedule_frequency(value: Any) -> str:
    frequency = str(value or "hourly").strip()
    return frequency if frequency in SCHEDULE_FREQUENCIES else "hourly"


def _as_automation_mode(raw_mode: Any, schedule_enabled: bool, errors: List[str]) -> str:
    if raw_mode not in (None, ""):
        mode = str(raw_mode).strip()
        if mode in AUTOMATION_MODES:
            return mode
        errors.append(f"非法自动化模式：{raw_mode}")
        return "off"
    return "execute" if schedule_enabled else "off"


def worker_provider_for_executor(executor: str) -> str:
    if executor == "codex_cli":
        return "codex"
    if executor == "claude_cli":
        return "claude"
    if executor == "fake_codex":
        return "fake"
    return "unknown"


def _options_with_current(options: List[Dict[str, str]], current: str) -> List[Dict[str, str]]:
    projected = [dict(option) for option in options]
    if current and all(option.get("slug") != current for option in projected):
        projected.append({"slug": current, "display_name": f"当前配置：{current}"})
    return projected


def worker_runtime_for_profile(profile: ProjectProfile) -> Dict[str, Any]:
    provider = worker_provider_for_executor(profile.executor)
    provider_options = [
        {"value": item["value"], "label": item["label"]}
        for item in WORKER_PROVIDER_OPTIONS
    ]
    codex_model_options = _options_with_current(CODEX_MODEL_OPTIONS, profile.codex_model)
    claude_model_options = _options_with_current(CLAUDE_MODEL_OPTIONS, profile.claude_model)
    providers = {
        "codex": {
            "label": "Codex",
            "settings": {
                "model": profile.codex_model,
                "reasoning_effort": profile.codex_reasoning_effort,
                "sandbox": profile.codex_sandbox,
            },
            "controls": [
                {
                    "key": "model",
                    "type": "select",
                    "label": "模型",
                    "description": "传给 Codex CLI 的模型。",
                    "options": [
                        {"value": option["slug"], "label": option["display_name"]}
                        for option in codex_model_options
                    ],
                },
                {
                    "key": "reasoning_effort",
                    "type": "select",
                    "label": "Reasoning",
                    "description": "Codex 的推理强度。",
                    "options": [
                        {"value": value, "label": value}
                        for value in ("low", "medium", "high", "xhigh")
                    ],
                },
                {
                    "key": "sandbox",
                    "type": "select",
                    "label": "执行权限",
                    "description": "Codex CLI 的 Sandbox。",
                    "options": [
                        {"value": "read-only", "label": "只读"},
                        {"value": "workspace-write", "label": "工作区可写"},
                        {"value": "danger-full-access", "label": "完全访问"},
                    ],
                },
            ],
        },
        "claude": {
            "label": "Claude",
            "settings": {
                "model": profile.claude_model,
                "permission_mode": profile.claude_permission_mode,
            },
            "controls": [
                {
                    "key": "model",
                    "type": "select",
                    "label": "模型",
                    "description": "传给 Claude Code CLI 的模型。",
                    "options": [
                        {"value": option["slug"], "label": option["display_name"]}
                        for option in claude_model_options
                    ],
                },
                {
                    "key": "permission_mode",
                    "type": "select",
                    "label": "执行权限",
                    "description": "完全自动执行会跳过 Claude 权限确认。",
                    "risk": "high",
                    "options": [
                        {"value": "bypassPermissions", "label": "完全自动执行"},
                        {"value": "acceptEdits", "label": "受限执行"},
                    ],
                },
            ],
        },
    }
    network = {
        "settings": dict(profile.worker_network),
        "controls": [
            {
                "key": "mode",
                "type": "select",
                "label": "代理模式",
                "description": "配置只作用于当前项目的 Worker 子进程。",
                "options": [
                    {"value": "custom", "label": "使用配置代理（默认）"},
                    {"value": "inherit", "label": "继承 LoopForge 环境"},
                    {"value": "direct", "label": "直连"},
                ],
            },
            {
                "key": "http_proxy",
                "type": "text",
                "label": "HTTP_PROXY",
                "description": "HTTP 请求代理地址。",
            },
            {
                "key": "https_proxy",
                "type": "text",
                "label": "HTTPS_PROXY",
                "description": "Claude Code 访问 Anthropic API 时主要使用此项。",
            },
            {
                "key": "all_proxy",
                "type": "text",
                "label": "ALL_PROXY",
                "description": "其他支持该变量的网络请求代理地址。",
            },
        ],
    }
    if provider in providers:
        current = providers[provider]
        return {
            "provider": provider,
            "provider_label": current["label"],
            "selectable": True,
            "provider_options": provider_options,
            "providers": providers,
            "network": network,
            "settings": dict(current["settings"]),
            "controls": [dict(control) for control in current["controls"]],
        }
    return {
        "provider": "fake" if provider == "fake" else "unknown",
        "provider_label": "演示 Worker" if provider == "fake" else "未知 Worker",
        "selectable": False,
        "provider_options": provider_options,
        "providers": providers,
        "network": network,
        "settings": {},
        "controls": [],
    }


def _tracked_project_contract(
    root_dir: Path,
    errors: List[str],
    *,
    field_name: str = "项目 Git 配置",
) -> Dict[str, Any] | None:
    path = root_dir / TRACKED_PROJECT_CONFIG
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"{field_name} 无法读取：{exc}")
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{field_name} 必须是对象")
        return {}
    if raw.get("schema_version") != 1:
        errors.append(f"{field_name}.schema_version 必须为 1")
    unexpected = sorted(set(raw) - TRACKED_PROJECT_CONFIG_FIELDS)
    if unexpected:
        errors.append(f"{field_name} 包含不支持字段：{', '.join(unexpected)}")
    project_type = str(raw.get("project_type") or "").strip()
    if not project_type:
        errors.append(f"{field_name}.project_type 不能为空")
    contract: Dict[str, Any] = {"project_type": project_type}
    return contract


def _as_project_group(value: Any, project_id: str, root_dir: Path, errors: List[str]) -> Dict[str, Any]:
    if value in (None, ""):
        return {}
    if not isinstance(value, dict):
        errors.append("project_group 必须是对象")
        return {}
    group_key = str(value.get("key") or project_id).strip() or project_id
    raw_children = value.get("children") or []
    if not isinstance(raw_children, list):
        errors.append("project_group.children 必须是列表")
        raw_children = []
    children: List[Dict[str, Any]] = []
    seen_children: set[str] = set()
    for index, child in enumerate(raw_children):
        if not isinstance(child, dict):
            errors.append(f"project_group.children[{index}] 必须是对象")
            continue
        key = str(child.get("key") or child.get("id") or child.get("project_id") or "").strip()
        if not key:
            errors.append(f"project_group.children[{index}] 缺少 key")
            continue
        if key in seen_children:
            errors.append(f"project_group.children key 重复：{key}")
        seen_children.add(key)
        path = child.get("path") or child.get("root_dir") or child.get("root") or child.get("repo_path")
        if not path:
            errors.append(f"project_group.children[{key}] 缺少 path")
        normalized = dict(child)
        normalized["key"] = key
        child_root = Path(str(path)).expanduser()
        if not child_root.is_absolute():
            child_root = root_dir / child_root
        tracked = _tracked_project_contract(
            child_root,
            errors,
            field_name=f"project_group.children[{key}] 的 Git 配置",
        )
        if tracked is not None:
            normalized["project_type"] = tracked.get("project_type", "")
        children.append(normalized)
    return {"key": group_key, "children": children}


def _project_from_raw(raw: Dict[str, Any], seen: Iterable[str], global_config: Optional[Dict[str, Any]] = None) -> ProjectProfile:
    errors: List[str] = []
    project_id = str(raw.get("id") or raw.get("project_id") or "").strip()
    if not project_id:
        errors.append("缺少项目 id")
    if project_id in seen:
        errors.append(f"项目 id 重复：{project_id}")

    name = str(raw.get("name") or project_id or "未命名项目")
    root_value = raw.get("root_dir") or raw.get("root")
    root_dir = Path(str(root_value or "")).expanduser()
    if not root_value:
        errors.append("缺少项目 root_dir")
    elif not root_dir.exists():
        errors.append(f"项目 root_dir 不存在：{root_dir}")

    runner_command = _as_command(raw.get("runner_command"))
    raw_schedule_frequency = raw.get("schedule_frequency")
    if raw_schedule_frequency and str(raw_schedule_frequency).strip() not in SCHEDULE_FREQUENCIES:
        errors.append(f"非法调度频率：{raw_schedule_frequency}")

    notification_channel = str(raw.get("notification_channel") or "wecom_robot")
    if notification_channel not in NOTIFICATION_CHANNELS:
        errors.append(f"非法通知渠道：{notification_channel}")
    executor = str(raw.get("executor") or "codex_cli").strip()
    if executor not in WORKER_EXECUTORS:
        errors.append(f"非法 executor：{executor}")
    codex_reasoning_effort = str(raw.get("codex_reasoning_effort") or DEFAULT_CODEX_REASONING_EFFORT).strip()
    if executor == "codex_cli" and codex_reasoning_effort not in CODEX_REASONING_EFFORTS:
        errors.append(f"非法 Codex reasoning 级别：{codex_reasoning_effort}")
    codex_sandbox = str(raw.get("codex_sandbox") or DEFAULT_CODEX_SANDBOX).strip()
    if executor == "codex_cli" and codex_sandbox not in CODEX_SANDBOXES:
        errors.append(f"非法 Codex sandbox：{codex_sandbox}")
    claude_permission_mode = str(raw.get("claude_permission_mode") or DEFAULT_CLAUDE_PERMISSION_MODE).strip()
    if executor == "claude_cli" and claude_permission_mode not in CLAUDE_PERMISSION_MODES:
        errors.append(f"非法 Claude 权限模式：{claude_permission_mode}")

    webhook_env = raw.get("webhook_env")
    webhook_url = raw.get("webhook_url")
    state_dir = raw.get("state_dir")
    report_dir = raw.get("report_dir")
    raw_schedule_enabled = _as_bool(raw.get("schedule_enabled"), True)
    automation_mode = _as_automation_mode(raw.get("automation_mode"), raw_schedule_enabled, errors)
    schedule_enabled = automation_mode != "off"
    tracked = _tracked_project_contract(root_dir, errors)
    project_type = (
        str(tracked.get("project_type") or "").strip()
        if tracked is not None
        else str(raw.get("project_type") or "").strip()
    )
    profile = ProjectProfile(
        project_id=project_id,
        name=name,
        root_dir=root_dir,
        runner_command=runner_command,
        enabled=_as_bool(raw.get("enabled"), True),
        schedule_enabled=schedule_enabled,
        schedule_frequency=_as_schedule_frequency(raw.get("schedule_frequency")),
        schedule_started_at=str(raw.get("schedule_started_at")) if raw.get("schedule_started_at") else None,
        automation_mode=automation_mode,
        priority=int(raw.get("priority") or 100),
        notification_channel=notification_channel,
        default_agent=str(raw.get("default_agent") or ("claude" if executor == "claude_cli" else "codex")),
        executor=executor,
        codex_model=str(raw.get("codex_model") or DEFAULT_CODEX_MODEL).strip() or DEFAULT_CODEX_MODEL,
        codex_reasoning_effort=codex_reasoning_effort,
        codex_sandbox=codex_sandbox,
        claude_model=str(raw.get("claude_model") or DEFAULT_CLAUDE_MODEL).strip() or DEFAULT_CLAUDE_MODEL,
        claude_permission_mode=claude_permission_mode,
        worker_network=normalize_worker_network(raw.get("worker_network"), errors),
        auto_commit=_as_bool(raw.get("auto_commit"), False),
        worktree_managed=_as_bool(raw.get("worktree_managed"), False),
        project_type=project_type,
        planning_adapter=str(raw.get("planning_adapter") or "").strip(),
        project_group=_as_project_group(raw.get("project_group"), project_id, root_dir, errors),
        global_config=global_config or {},
        webhook_env=str(webhook_env) if webhook_env else None,
        webhook_url=str(webhook_url) if webhook_url else None,
        state_dir=Path(str(state_dir)).expanduser() if state_dir else None,
        report_dir=Path(str(report_dir)).expanduser() if report_dir else None,
        errors=errors,
    )
    return profile


def load_projects(path: Optional[Path] = None) -> List[ProjectProfile]:
    config_path = path or default_config_path()
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)

    projects_raw = raw.get("projects") if isinstance(raw, dict) else None
    if not isinstance(projects_raw, list):
        raise ValueError("projects 配置必须是列表")

    profiles: List[ProjectProfile] = []
    seen = set()
    global_config = read_global_config()
    for item in projects_raw:
        if not isinstance(item, dict):
            continue
        profile = _project_from_raw(item, seen, global_config)
        profiles.append(profile)
        if profile.project_id:
            seen.add(profile.project_id)
    return profiles


def read_projects_config(path: Optional[Path] = None) -> Dict[str, Any]:
    config_path = path or default_config_path()
    if not config_path.exists():
        return {"projects": []}
    with config_path.open("r", encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict):
        raise ValueError("projects 配置必须是对象")
    projects = raw.get("projects")
    if projects is None:
        raw["projects"] = []
    elif not isinstance(projects, list):
        raise ValueError("projects 配置必须是列表")
    return raw


def write_projects_config(payload: Dict[str, Any], path: Optional[Path] = None) -> None:
    config_path = path or default_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def append_project_config(entry: Dict[str, Any], path: Optional[Path] = None) -> None:
    payload = read_projects_config(path)
    projects = payload.setdefault("projects", [])
    if not isinstance(projects, list):
        raise ValueError("projects 配置必须是列表")
    projects.append(entry)
    write_projects_config(payload, path)


def update_project_config(project_id: str, updates: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    payload = read_projects_config(path)
    projects = payload.setdefault("projects", [])
    if not isinstance(projects, list):
        raise ValueError("projects 配置必须是列表")
    for item in projects:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("project_id") or "")
        if item_id == project_id:
            item.update(updates)
            write_projects_config(payload, path)
            return item
    raise KeyError(f"未知项目：{project_id}")


def remove_project_config(project_id: str, path: Optional[Path] = None) -> Dict[str, Any]:
    payload = read_projects_config(path)
    projects = payload.setdefault("projects", [])
    if not isinstance(projects, list):
        raise ValueError("projects 配置必须是列表")
    for index, item in enumerate(projects):
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("project_id") or "")
        if item_id == project_id:
            removed = projects.pop(index)
            write_projects_config(payload, path)
            return removed
    raise KeyError(f"未知项目：{project_id}")


def profile_to_dict(profile: ProjectProfile) -> Dict[str, Any]:
    return {
        "project_id": profile.project_id,
        "name": profile.name,
        "root_dir": str(profile.root_dir.resolve()),
        "enabled": profile.enabled,
        "schedule_enabled": profile.schedule_enabled,
        "schedule_frequency": profile.schedule_frequency,
        "schedule_frequency_label": SCHEDULE_FREQUENCY_LABELS.get(profile.schedule_frequency, profile.schedule_frequency),
        "schedule_started_at": profile.schedule_started_at,
        "automation_mode": profile.automation_mode,
        "automation_mode_label": AUTOMATION_MODE_LABELS.get(profile.automation_mode, profile.automation_mode),
        "automation_mode_description": AUTOMATION_MODE_DESCRIPTIONS.get(profile.automation_mode, ""),
        "automation_allowed_loops": [
            loop_type
            for loop_type in ["scan", "dev"]
            if loop_type in AUTOMATION_MODE_ALLOWED_LOOPS.get(profile.automation_mode, set())
        ],
        "priority": profile.priority,
        "notification_channel": profile.notification_channel,
        "webhook_env": profile.webhook_env,
        "webhook_url": profile.webhook_url,
        "default_agent": profile.default_agent,
        "executor": profile.executor,
        "codex_model": profile.codex_model,
        "codex_model_options": [dict(option) for option in CODEX_MODEL_OPTIONS],
        "codex_reasoning_effort": profile.codex_reasoning_effort,
        "codex_sandbox": profile.codex_sandbox,
        "claude_model": profile.claude_model,
        "claude_permission_mode": profile.claude_permission_mode,
        "worker_runtime": worker_runtime_for_profile(profile),
        "auto_commit": profile.auto_commit,
        "worktree_managed": profile.worktree_managed,
        "project_type": profile.project_type,
        "planning_adapter": profile.planning_adapter or "builtin",
        "project_group": profile.project_group,
        "global_config": profile.global_config,
        "state_dir": str(profile.loopforge_dir.resolve()),
        "report_dir": str(profile.reports_dir.resolve()),
        "is_valid": profile.is_valid,
        "errors": list(profile.errors),
    }
