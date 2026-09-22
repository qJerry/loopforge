"""父 task 多 target 的规范化与执行顺序。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence

from .config import ProjectProfile


class TargetPlanError(ValueError):
    def __init__(self, code: str, summary: str, detail: Dict[str, Any] | None = None) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.detail = detail or {}


@dataclass(frozen=True)
class TaskTarget:
    id: str
    project: str
    repo: str
    repo_path: Path
    has_repo_path: bool
    branch: str
    target_branch: str
    merge_method: str
    worktree_path: str
    scope: str
    after: Sequence[str]
    raw: Dict[str, Any]

    def to_payload(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "project": self.project,
            "repo": self.repo,
            "repo_path": str(self.repo_path),
            "branch": self.branch,
            "target_branch": self.target_branch,
            "merge_method": self.merge_method,
            "worktree_path": self.worktree_path,
            "scope": self.scope,
            "after": list(self.after),
        }


@dataclass(frozen=True)
class TargetPlan:
    targets: Sequence[TaskTarget]
    ordered: Sequence[TaskTarget]
    explicit: bool

    def to_payload(self) -> Dict[str, Any]:
        return {
            "status": "ready",
            "explicit": self.explicit,
            "ordered_target_ids": [target.id for target in self.ordered],
            "targets": [target.to_payload() for target in self.targets],
        }


def task_target_plan(
    profile: ProjectProfile,
    task: Dict[str, Any],
    *,
    target_branch_override: str = "",
    merge_method_override: str = "",
    require_repo_paths: bool = False,
) -> TargetPlan:
    raw_targets = task.get("targets")
    explicit = isinstance(raw_targets, list) and len(raw_targets) > 0
    source_targets = raw_targets if explicit else [_fallback_target(task, profile)]
    child_map = _project_group_children(profile)
    targets = [
        _target_from_raw(
            profile,
            task,
            raw,
            index=index,
            child_map=child_map,
            target_branch_override=target_branch_override,
            merge_method_override=merge_method_override,
        )
        for index, raw in enumerate(source_targets)
    ]
    _validate_unique_ids(targets)
    if require_repo_paths:
        _validate_repo_paths(profile, targets)
    ordered = _topological_order(targets)
    return TargetPlan(targets=targets, ordered=ordered, explicit=explicit)


def target_plan_error_payload(error: TargetPlanError) -> Dict[str, Any]:
    return {"status": "invalid", "code": error.code, "summary": error.summary, "detail": error.detail}


def _fallback_target(task: Dict[str, Any], profile: ProjectProfile) -> Dict[str, Any]:
    git = task.get("git") if isinstance(task.get("git"), dict) else {}
    return {
        "id": profile.project_id,
        "project": profile.project_id,
        "repo": profile.project_id,
        "repo_path": str(profile.root_dir),
        "branch": task.get("branch") or task.get("feature_branch") or git.get("feature_branch") or "",
        "target_branch": task.get("target_branch") or git.get("target_branch") or "main",
        "merge_method": task.get("merge_method") or "squash",
        "worktree_path": task.get("worktree_path") or task.get("worktree") or "",
    }


def _target_from_raw(
    profile: ProjectProfile,
    task: Dict[str, Any],
    raw: Any,
    *,
    index: int,
    child_map: Dict[str, Dict[str, Any]],
    target_branch_override: str,
    merge_method_override: str,
) -> TaskTarget:
    payload = raw if isinstance(raw, dict) else {"project": str(raw or "").strip()}
    git = payload.get("git") if isinstance(payload.get("git"), dict) else {}
    target_id = _target_id(payload, index)
    project = str(payload.get("project") or payload.get("project_id") or target_id).strip() or target_id
    child = child_map.get(project) or child_map.get(target_id) or {}
    repo = str(payload.get("repo") or payload.get("repo_name") or child.get("repo") or project).strip() or project
    repo_value = (
        payload.get("repo_path")
        or payload.get("root_dir")
        or payload.get("root")
        or payload.get("path")
        or child.get("repo_path")
        or child.get("root_dir")
        or child.get("root")
        or child.get("path")
    )
    repo_path = _resolve_path(profile.root_dir, repo_value) if repo_value else profile.root_dir
    worktree_path = _string_path(
        repo_path,
        payload.get("worktree_path") or payload.get("worktree") or "",
    )
    return TaskTarget(
        id=target_id,
        project=project,
        repo=repo,
        repo_path=repo_path,
        has_repo_path=bool(repo_value),
        branch=str(payload.get("branch") or payload.get("feature_branch") or git.get("feature_branch") or "").strip(),
        target_branch=(
            target_branch_override
            or str(payload.get("target_branch") or git.get("target_branch") or task.get("target_branch") or "main")
        ).strip()
        or "main",
        merge_method=(merge_method_override or str(payload.get("merge_method") or task.get("merge_method") or "squash")).strip() or "squash",
        worktree_path=worktree_path,
        scope=str(payload.get("scope") or "").strip(),
        after=_after_values(payload.get("after")),
        raw=dict(payload),
    )


def _target_id(payload: Dict[str, Any], index: int) -> str:
    for key in ["id", "project", "project_id", "target", "key"]:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return f"target-{index + 1}"


def _project_group_children(profile: ProjectProfile) -> Dict[str, Dict[str, Any]]:
    group = profile.project_group if isinstance(profile.project_group, dict) else {}
    children = group.get("children")
    if not isinstance(children, list):
        return {}
    result: Dict[str, Dict[str, Any]] = {}
    for child in children:
        if not isinstance(child, dict):
            continue
        key = str(child.get("key") or child.get("id") or child.get("project_id") or "").strip()
        if key:
            result[key] = child
    return result


def _resolve_path(base: Path, value: Any) -> Path:
    path = Path(str(value)).expanduser()
    if not path.is_absolute():
        path = base / path
    return path.resolve()


def _string_path(base: Path, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def _after_values(value: Any) -> Sequence[str]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return (str(value).strip(),) if str(value).strip() else ()


def _validate_unique_ids(targets: Sequence[TaskTarget]) -> None:
    seen: set[str] = set()
    duplicates: List[str] = []
    for target in targets:
        if target.id in seen:
            duplicates.append(target.id)
        seen.add(target.id)
    if duplicates:
        raise TargetPlanError(
            "target_id_duplicate",
            f"targets 存在重复 id：{', '.join(sorted(set(duplicates)))}",
            {"duplicates": sorted(set(duplicates))},
        )


def _validate_repo_paths(profile: ProjectProfile, targets: Sequence[TaskTarget]) -> None:
    missing = [
        target.id
        for target in targets
        if target.project != profile.project_id and not target.has_repo_path
    ]
    if missing:
        raise TargetPlanError(
            "target_repo_missing",
            f"targets 缺少子仓 path 或 project_group.children 配置：{', '.join(missing)}",
            {"targets": missing},
        )


def _topological_order(targets: Sequence[TaskTarget]) -> Sequence[TaskTarget]:
    by_id = {target.id: target for target in targets}
    missing = sorted({dep for target in targets for dep in target.after if dep not in by_id})
    if missing:
        raise TargetPlanError(
            "target_after_missing",
            f"targets[].after 引用了不存在的 target：{', '.join(missing)}",
            {"missing": missing},
        )
    remaining = {target.id: set(target.after) for target in targets}
    ordered: List[TaskTarget] = []
    while remaining:
        ready = [target for target in targets if target.id in remaining and not remaining[target.id]]
        if not ready:
            cycle = sorted(remaining)
            raise TargetPlanError(
                "target_after_cycle",
                f"targets[].after 形成环：{', '.join(cycle)}",
                {"targets": cycle},
            )
        for target in ready:
            ordered.append(target)
            remaining.pop(target.id, None)
            for dependencies in remaining.values():
                dependencies.discard(target.id)
    return ordered
