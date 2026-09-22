"""LoopForge Schema v2 单任务 JSON 存储。

本模块只负责文件合同、并发版本校验和原子写入，不执行 Git 操作。
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from .config import ProjectProfile
from .domain import TASK_STATES, TASK_STATE_TRANSITIONS, TERMINAL_TASK_STATES, utc_now
from .task_contract import task_uses_document_contract


TASK_SCHEMA_VERSION = 2
TASK_PRIORITIES = ("P0", "P1", "P2", "P3", "P4")
_PRIORITY_RANK = {priority: index for index, priority in enumerate(TASK_PRIORITIES)}
_TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUIRED_FIELDS = (
    "schema_version",
    "id",
    "version",
    "title",
    "description",
    "status",
    "priority",
    "created_at",
    "updated_at",
    "git",
    "docs",
    "targets",
)
_IMMUTABLE_MUTATION_FIELDS = {"schema_version", "id", "version", "created_at"}
_TASK_FIELD_ORDER = (
    "schema_version",
    "id",
    "title",
    "description",
    "status",
    "priority",
    "planning_level",
    "source",
    "version",
    "created_at",
    "updated_at",
    "acceptance",
    "acceptance_refs",
    "acceptance_supplements",
    "git",
    "docs",
    "targets",
)
_GIT_FIELD_ORDER = ("remote", "target_branch", "base_revision", "feature_branch", "branch_revision")
_DOCS_FIELD_ORDER = (
    "mode",
    "module",
    "revision",
    "phases",
    "requirements",
    "design",
    "specs",
    "supporting",
)
_PHASE_FIELD_ORDER = (
    "id",
    "title",
    "module",
    "after",
    "requirements",
    "design",
    "specs",
    "supporting",
)
_TARGET_FIELD_ORDER = ("id", "project", "scope", "after", "repo", "git", "docs")


class TaskStoreError(ValueError):
    """Task Store 的稳定错误合同。"""

    def __init__(self, code: str, summary: str, detail: str = "") -> None:
        self.code = code
        self.summary = summary
        self.detail = detail
        message = f"{summary}：{detail}" if detail else summary
        super().__init__(message)


@dataclass(frozen=True)
class TaskFileChange:
    """一次尚待 Git publisher 发布的精确文件变化。"""

    task_id: str
    operation: str
    paths: Tuple[Path, ...]
    before: Optional[Dict[str, Any]]
    after: Optional[Dict[str, Any]]


def tasks_dir(profile: ProjectProfile) -> Path:
    """返回并验证项目的活跃任务目录。"""

    root = profile.root_dir.resolve()
    directory = profile.root_dir / "data" / "tasks"
    if directory.is_symlink():
        raise TaskStoreError("unsafe_task_path", "任务目录不能是符号链接", str(directory))
    resolved = directory.resolve(strict=False)
    if not _is_relative_to(resolved, root):
        raise TaskStoreError("unsafe_task_path", "任务目录越过项目边界", str(directory))
    return directory


def load_active_tasks(profile: ProjectProfile) -> list[Dict[str, Any]]:
    """读取所有活跃任务并按 priority、created_at、id 确定性排序。"""

    directory = tasks_dir(profile)
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise TaskStoreError("invalid_tasks_dir", "data/tasks 必须是目录", str(directory))

    items: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(directory.glob("*.json"), key=lambda item: item.name):
        task = _read_task_path(profile, path, active=True)
        task_id = str(task["id"])
        if task_id in seen:
            raise TaskStoreError("duplicate_task_id", "发现重复任务 id", task_id)
        seen.add(task_id)
        items.append(task)
    items.sort(key=_active_sort_key)
    return items


def load_task(profile: ProjectProfile, task_id: str) -> Dict[str, Any]:
    """按稳定 id 读取一个活跃任务。"""

    path = _active_task_path(profile, task_id)
    if not path.exists():
        if _find_history_task_path(profile, task_id) is not None:
            raise TaskStoreError("immutable_history_task", "历史任务不可修改", task_id)
        raise TaskStoreError("task_not_found", "未知任务", task_id)
    return _read_task_path(profile, path, active=True)


def load_history_tasks(profile: ProjectProfile, limit: int = 20) -> Dict[str, Any]:
    """按年月倒序有界扫描历史任务，并将坏文件投影为 issues。"""

    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise TaskStoreError("invalid_history_limit", "历史读取 limit 必须是正整数")
    root = _history_root(profile)
    if not root.exists():
        return {"items": [], "issues": []}
    if not root.is_dir():
        raise TaskStoreError("invalid_history_dir", "任务历史路径必须是目录", str(root))

    items: list[Dict[str, Any]] = []
    issues: list[Dict[str, str]] = []
    for month_dir in _history_month_dirs(profile):
        for path in sorted(month_dir.glob("*.json"), key=lambda item: item.name):
            try:
                items.append(_read_task_path(profile, path, active=False))
            except TaskStoreError as exc:
                issues.append(_issue_payload(profile, path, exc))
        if len(items) >= limit:
            break

    items.sort(key=lambda task: (-_parse_timestamp(task["archived_at"], "archived_at").timestamp(), str(task["id"])))
    return {"items": items[:limit], "issues": issues}


def create_task_file(profile: ProjectProfile, task: Dict[str, Any]) -> TaskFileChange:
    """创建一个活跃任务文件，不覆盖同 id 文件。"""

    normalized = _validate_task(task, active=True, enforce_contract=True)
    _validate_supersedes_reference(profile, normalized)
    path = _active_task_path(profile, str(normalized["id"]))
    if path.exists() or path.is_symlink():
        raise TaskStoreError("task_exists", "任务已存在", str(normalized["id"]))
    _write_json_atomic(path, normalized)
    return TaskFileChange(
        task_id=str(normalized["id"]),
        operation="create",
        paths=(_relative_project_path(profile, path),),
        before=None,
        after=copy.deepcopy(normalized),
    )


def archive_task_file(
    profile: ProjectProfile,
    task_id: str,
    expected_version: int,
    allowed_states: Iterable[str],
    terminal_status: str,
    fields: Optional[Dict[str, Any]] = None,
    *,
    at: Optional[datetime] = None,
    remove_fields: Iterable[str] = (),
) -> TaskFileChange:
    """完成终态 mutation，并把活跃文件移动到年月历史目录。"""

    if terminal_status not in TERMINAL_TASK_STATES:
        raise TaskStoreError("invalid_terminal_status", "归档状态必须是 completed 或 abandoned", terminal_status)
    before = load_task(profile, task_id)
    if expected_version != before["version"]:
        raise TaskStoreError(
            "stale_task_version",
            "任务版本已过期",
            f"期望 {expected_version}，实际 {before['version']}",
        )
    current_status = str(before["status"])
    allowed = {str(state) for state in allowed_states}
    if current_status not in allowed:
        raise TaskStoreError(
            "unexpected_task_state",
            "任务当前状态不允许归档",
            f"当前 {current_status}，允许 {sorted(allowed)}",
        )
    if terminal_status not in TASK_STATE_TRANSITIONS[current_status]:
        raise TaskStoreError(
            "invalid_task_transition",
            "非法任务状态跃迁",
            f"{current_status} -> {terminal_status}",
        )

    observed_at = at or datetime.now(timezone.utc).replace(microsecond=0)
    if observed_at.tzinfo is None:
        raise TaskStoreError("invalid_task_timestamp", "归档时间必须包含时区")
    archived_at = observed_at.replace(microsecond=0).isoformat()
    extra = copy.deepcopy(fields or {})
    removals = {str(field) for field in remove_fields}
    forbidden = _IMMUTABLE_MUTATION_FIELDS.union({"status", "archived_at"}).intersection(extra)
    if forbidden:
        raise TaskStoreError("immutable_task_field", "归档字段包含保留字段", ", ".join(sorted(forbidden)))
    protected = sorted(set(_REQUIRED_FIELDS).intersection(removals))
    if protected:
        raise TaskStoreError("required_task_field", "任务必填字段不能删除", ", ".join(protected))

    after = copy.deepcopy(before)
    after.update(extra)
    for field in removals:
        after.pop(field, None)
    after["status"] = terminal_status
    after["version"] = int(before["version"]) + 1
    after["updated_at"] = archived_at
    after["archived_at"] = archived_at
    if terminal_status == "completed":
        after.setdefault("completed_at", archived_at)
    else:
        after.setdefault("abandoned_at", archived_at)
    normalized = _validate_task(after, active=False, enforce_contract=True)

    active_path = _active_task_path(profile, task_id)
    history_path = _history_task_path(profile, task_id, observed_at)
    if history_path.exists() or history_path.is_symlink():
        raise TaskStoreError("immutable_history_task", "历史任务不可覆盖", task_id)
    _write_json_atomic(active_path, normalized)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    os.replace(active_path, history_path)
    return TaskFileChange(
        task_id=task_id,
        operation="archive",
        paths=(_relative_project_path(profile, active_path), _relative_project_path(profile, history_path)),
        before=copy.deepcopy(before),
        after=copy.deepcopy(normalized),
    )


def mutate_task_file(
    profile: ProjectProfile,
    task_id: str,
    expected_version: int,
    allowed_states: Iterable[str],
    fields: Dict[str, Any],
    *,
    remove_fields: Iterable[str] = (),
) -> TaskFileChange:
    """以 compare-and-swap 方式修改一个活跃任务。"""

    before = load_task(profile, task_id)
    actual_version = before["version"]
    if expected_version != actual_version:
        raise TaskStoreError(
            "stale_task_version",
            "任务版本已过期",
            f"期望 {expected_version}，实际 {actual_version}",
        )

    allowed = {str(state) for state in allowed_states}
    current_status = str(before["status"])
    if current_status not in allowed:
        raise TaskStoreError(
            "unexpected_task_state",
            "任务当前状态不允许本次修改",
            f"当前 {current_status}，允许 {sorted(allowed)}",
        )
    removals = {str(field) for field in remove_fields}
    if not isinstance(fields, dict) or (not fields and not removals):
        raise TaskStoreError("empty_task_mutation", "任务修改字段不能为空")
    immutable = sorted(_IMMUTABLE_MUTATION_FIELDS.intersection(fields))
    if immutable:
        raise TaskStoreError("immutable_task_field", "任务不可修改身份字段", ", ".join(immutable))
    protected = sorted(set(_REQUIRED_FIELDS).intersection(removals))
    if protected:
        raise TaskStoreError("required_task_field", "任务必填字段不能删除", ", ".join(protected))

    next_status = str(fields.get("status", current_status))
    if next_status in TERMINAL_TASK_STATES:
        raise TaskStoreError("terminal_requires_archive", "终态任务必须通过归档操作写入历史", next_status)
    if next_status != current_status and next_status not in TASK_STATE_TRANSITIONS[current_status]:
        raise TaskStoreError(
            "invalid_task_transition",
            "非法任务状态跃迁",
            f"{current_status} -> {next_status}",
        )

    after = copy.deepcopy(before)
    after.update(copy.deepcopy(fields))
    for field in removals:
        after.pop(field, None)
    after["version"] = actual_version + 1
    after["updated_at"] = utc_now()
    normalized = _validate_task(after, active=True, enforce_contract=True)
    path = _active_task_path(profile, task_id)
    _write_json_atomic(path, normalized)
    return TaskFileChange(
        task_id=task_id,
        operation="mutate",
        paths=(_relative_project_path(profile, path),),
        before=copy.deepcopy(before),
        after=copy.deepcopy(normalized),
    )


def _active_task_path(profile: ProjectProfile, task_id: str) -> Path:
    _validate_task_id(task_id)
    directory = tasks_dir(profile)
    path = directory / f"{task_id}.json"
    if path.is_symlink():
        raise TaskStoreError("unsafe_task_path", "任务文件不能是符号链接", str(path))
    resolved = path.resolve(strict=False)
    if not _is_relative_to(resolved, directory.resolve(strict=False)):
        raise TaskStoreError("unsafe_task_path", "任务文件越过任务目录边界", str(path))
    return path


def _read_task_path(profile: ProjectProfile, path: Path, *, active: bool) -> Dict[str, Any]:
    if path.is_symlink():
        raise TaskStoreError("unsafe_task_path", "任务文件不能是符号链接", str(path))
    expected_root = tasks_dir(profile).resolve(strict=False)
    resolved = path.resolve(strict=True)
    if not _is_relative_to(resolved, expected_root):
        raise TaskStoreError("unsafe_task_path", "任务文件越过任务目录边界", str(path))
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskStoreError("invalid_task_file", "任务文件无法读取", f"{path}: {exc}") from exc
    normalized = _validate_task(payload, active=active)
    if path.name != f"{normalized['id']}.json":
        raise TaskStoreError(
            "task_filename_mismatch",
            "任务文件名与 id 不一致",
            f"{path.name} != {normalized['id']}.json",
        )
    if active:
        _validate_supersedes_reference(profile, normalized)
    return normalized


def _validate_task(task: Any, *, active: bool, enforce_contract: bool = False) -> Dict[str, Any]:
    if not isinstance(task, dict):
        raise TaskStoreError("invalid_task_schema", "任务根节点必须是 JSON object")
    for field in _REQUIRED_FIELDS:
        if field not in task:
            raise TaskStoreError("missing_task_field", f"任务缺少必填字段 {field}")

    if task.get("schema_version") != TASK_SCHEMA_VERSION:
        raise TaskStoreError("invalid_schema_version", "schema_version 必须是 2")
    _validate_task_id(task.get("id"))
    version = task.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise TaskStoreError("invalid_task_version", "version 必须是大于等于 1 的整数")

    for field in ("title", "description"):
        _require_text(task, field)
    if "source" in task:
        _require_text(task, "source")
    if "planning_level" in task:
        planning_level = _require_text(task, "planning_level")
        if planning_level not in {"complex", "lightweight"}:
            raise TaskStoreError("invalid_planning_level", "planning_level 只允许 complex 或 lightweight")
    status = _require_text(task, "status")
    if status not in TASK_STATES:
        raise TaskStoreError("invalid_task_status", "不支持的任务状态", status)
    if active and status in TERMINAL_TASK_STATES:
        raise TaskStoreError("terminal_task_in_active", "终态任务不能保留在活跃目录", status)
    if not active and status not in TERMINAL_TASK_STATES:
        raise TaskStoreError("active_task_in_history", "历史目录只能包含终态任务", status)

    priority = _require_text(task, "priority").upper()
    if priority not in _PRIORITY_RANK:
        raise TaskStoreError("invalid_task_priority", "不支持的任务优先级", priority)
    if not isinstance(task.get("git"), dict):
        raise TaskStoreError("invalid_task_git", "git 必须是 JSON object")
    for field in ("base_revision", "feature_branch", "branch_revision"):
        _require_text(task["git"], field, prefix="git.")
    for field in ("remote", "target_branch"):
        if field in task["git"]:
            _require_text(task["git"], field, prefix="git.")
    if not isinstance(task.get("docs"), dict):
        raise TaskStoreError("invalid_task_docs", "docs 必须是 JSON object")
    if not isinstance(task.get("targets"), list):
        raise TaskStoreError("invalid_task_targets", "targets 必须是数组")
    _parse_timestamp(task.get("created_at"), "created_at")
    _parse_timestamp(task.get("updated_at"), "updated_at")
    if not active:
        _parse_timestamp(task.get("archived_at"), "archived_at")

    normalized = copy.deepcopy(task)
    normalized["id"] = str(task["id"])
    normalized["priority"] = priority
    if enforce_contract:
        if task_uses_document_contract(normalized):
            normalized.pop("acceptance", None)
            normalized.pop("acceptance_refs", None)
        else:
            acceptance = normalized.get("acceptance")
            if not isinstance(acceptance, list) or not acceptance or any(
                not isinstance(item, str) or not item.strip() for item in acceptance
            ):
                raise TaskStoreError("invalid_task_acceptance", "普通任务 acceptance 必须是非空字符串数组")
            if not isinstance(normalized.get("acceptance_refs"), list):
                raise TaskStoreError("invalid_task_acceptance_refs", "普通任务 acceptance_refs 必须是数组")
    return canonical_task_payload(normalized)


def canonical_task_payload(task: Dict[str, Any]) -> Dict[str, Any]:
    """返回使用稳定语义字段顺序的任务副本。"""

    compacted = dict(task)
    if str(compacted.get("source") or "").strip() == "manual":
        compacted.pop("source", None)
    if str(compacted.get("planning_level") or "").strip() == "complex":
        compacted.pop("planning_level", None)
    compacted.pop("intake", None)
    compacted.pop("documentation_mode", None)
    return _ordered_object(compacted, _TASK_FIELD_ORDER, _canonical_task_value)


def _canonical_task_value(key: str, value: Any) -> Any:
    if key == "git" and isinstance(value, dict):
        return _canonical_git(value)
    if key == "docs" and isinstance(value, dict):
        return _canonical_docs(value)
    if key == "targets" and isinstance(value, list):
        return [_canonical_target(item) if isinstance(item, dict) else _canonical_value(item) for item in value]
    return _canonical_value(value)


def _canonical_git(git: Dict[str, Any]) -> Dict[str, Any]:
    compacted = dict(git)
    if str(compacted.get("remote") or "").strip() == "origin":
        compacted.pop("remote", None)
    if str(compacted.get("target_branch") or "").strip() == "main":
        compacted.pop("target_branch", None)
    return _ordered_object(compacted, _GIT_FIELD_ORDER)


def _canonical_target(target: Dict[str, Any]) -> Dict[str, Any]:
    compacted = dict(target)
    if str(compacted.get("repo") or "").strip() == str(compacted.get("project") or "").strip():
        compacted.pop("repo", None)
    if compacted.get("after") == []:
        compacted.pop("after", None)

    def transform(key: str, value: Any) -> Any:
        if key == "git" and isinstance(value, dict):
            return _canonical_git(value)
        if key == "docs" and isinstance(value, dict):
            return _canonical_docs(value)
        return _canonical_value(value)

    return _ordered_object(compacted, _TARGET_FIELD_ORDER, transform)


def _canonical_docs(docs: Dict[str, Any]) -> Dict[str, Any]:
    compacted = dict(docs)
    if compacted.get("supporting") == []:
        compacted.pop("supporting", None)
    phases = compacted.get("phases")

    def transform(key: str, value: Any) -> Any:
        if key == "phases" and isinstance(value, list):
            return [
                _canonical_phase(item) if isinstance(item, dict) else _canonical_value(item)
                for item in value
            ]
        return _canonical_value(value)

    return _ordered_object(compacted, _DOCS_FIELD_ORDER, transform)


def _canonical_phase(phase: Dict[str, Any]) -> Dict[str, Any]:
    compacted = dict(phase)
    if str(compacted.get("title") or "").strip() == str(compacted.get("id") or "").strip():
        compacted.pop("title", None)
    if compacted.get("after") == []:
        compacted.pop("after", None)
    return _ordered_object(compacted, _PHASE_FIELD_ORDER)


def _ordered_object(
    payload: Dict[str, Any],
    order: tuple[str, ...],
    transform: Any = None,
) -> Dict[str, Any]:
    convert = transform or (lambda _key, value: _canonical_value(value))
    result: Dict[str, Any] = {}
    for key in order:
        if key in payload:
            result[key] = convert(key, payload[key])
    for key in sorted(set(payload) - set(order)):
        result[key] = convert(key, payload[key])
    return result


def _canonical_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    return value


def _validate_task_id(value: Any) -> str:
    if not isinstance(value, str) or not _TASK_ID_PATTERN.fullmatch(value):
        raise TaskStoreError("invalid_task_id", "任务 id 必须是安全文件名片段", str(value))
    return value


def _require_text(payload: Dict[str, Any], field: str, *, prefix: str = "") -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise TaskStoreError("invalid_task_field", f"{prefix}{field} 必须是非空字符串")
    return value.strip()


def _parse_timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TaskStoreError("invalid_task_timestamp", f"{field} 必须是 ISO-8601 时间")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskStoreError("invalid_task_timestamp", f"{field} 必须是 ISO-8601 时间", value) from exc
    if parsed.tzinfo is None:
        raise TaskStoreError("invalid_task_timestamp", f"{field} 必须包含时区", value)
    return parsed


def _active_sort_key(task: Dict[str, Any]) -> tuple[int, float, str]:
    return (
        _PRIORITY_RANK[str(task["priority"])],
        _parse_timestamp(task["created_at"], "created_at").timestamp(),
        str(task["id"]),
    )


def _history_root(profile: ProjectProfile) -> Path:
    root = tasks_dir(profile) / "history"
    if root.is_symlink():
        raise TaskStoreError("unsafe_task_path", "任务历史目录不能是符号链接", str(root))
    resolved = root.resolve(strict=False)
    if not _is_relative_to(resolved, tasks_dir(profile).resolve(strict=False)):
        raise TaskStoreError("unsafe_task_path", "任务历史目录越过项目边界", str(root))
    return root


def _history_task_path(profile: ProjectProfile, task_id: str, observed_at: datetime) -> Path:
    _validate_task_id(task_id)
    year = observed_at.strftime("%Y")
    month = observed_at.strftime("%m")
    path = _history_root(profile) / year / month / f"{task_id}.json"
    for parent in (path.parent.parent, path.parent):
        if parent.is_symlink():
            raise TaskStoreError("unsafe_task_path", "任务历史年月目录不能是符号链接", str(parent))
    if path.is_symlink():
        raise TaskStoreError("unsafe_task_path", "历史任务文件不能是符号链接", str(path))
    if not _is_relative_to(path.resolve(strict=False), _history_root(profile).resolve(strict=False)):
        raise TaskStoreError("unsafe_task_path", "历史任务文件越过历史目录边界", str(path))
    return path


def _history_month_dirs(profile: ProjectProfile) -> list[Path]:
    root = _history_root(profile)
    months: list[Path] = []
    if not root.exists():
        return months
    for year_dir in sorted(root.iterdir(), key=lambda item: item.name, reverse=True):
        if not year_dir.is_dir() or year_dir.is_symlink() or not re.fullmatch(r"\d{4}", year_dir.name):
            continue
        for month_dir in sorted(year_dir.iterdir(), key=lambda item: item.name, reverse=True):
            if not month_dir.is_dir() or month_dir.is_symlink() or not re.fullmatch(r"0[1-9]|1[0-2]", month_dir.name):
                continue
            months.append(month_dir)
    return months


def _find_history_task_path(profile: ProjectProfile, task_id: str) -> Optional[Path]:
    _validate_task_id(task_id)
    matches: list[Path] = []
    for month_dir in _history_month_dirs(profile):
        path = month_dir / f"{task_id}.json"
        if path.is_symlink():
            raise TaskStoreError("unsafe_task_path", "历史任务文件不能是符号链接", str(path))
        if path.exists():
            matches.append(path)
    if len(matches) > 1:
        raise TaskStoreError("duplicate_history_task", "历史目录存在重复任务 id", task_id)
    return matches[0] if matches else None


def _validate_supersedes_reference(profile: ProjectProfile, task: Dict[str, Any]) -> None:
    value = task.get("supersedes")
    if value in (None, ""):
        return
    supersedes = _validate_task_id(value)
    if supersedes == task["id"]:
        raise TaskStoreError("invalid_supersedes", "supersedes 不能引用任务自身", supersedes)
    path = _find_history_task_path(profile, supersedes)
    if path is None:
        raise TaskStoreError("invalid_supersedes", "supersedes 必须引用已有历史任务", supersedes)
    _read_task_path(profile, path, active=False)


def _issue_payload(profile: ProjectProfile, path: Path, error: TaskStoreError) -> Dict[str, str]:
    return {
        "code": error.code,
        "path": str(_relative_project_path(profile, path)),
        "summary": error.summary,
        "detail": error.detail,
    }


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(canonical_task_payload(payload), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _relative_project_path(profile: ProjectProfile, path: Path) -> Path:
    try:
        return path.relative_to(profile.root_dir)
    except ValueError as exc:
        raise TaskStoreError("unsafe_task_path", "任务文件不在项目目录内", str(path)) from exc


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
