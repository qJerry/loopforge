"""LoopForge 管理的 feature branch 与 Git worktree 生命周期。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict

from .config import ProjectProfile
from .domain import utc_now
from .managed_branches import (
    is_managed_feature_branch,
    managed_feature_branch,
    readable_feature_branch,
    slug_branch_segment,
)
from .targets import TargetPlanError, TaskTarget, task_target_plan


class WorktreePreparationError(RuntimeError):
    def __init__(self, code: str, summary: str, detail: Dict[str, Any] | None = None) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.detail = detail or {}


def prepare_task_worktrees(profile: ProjectProfile, item: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    return _prepare_task_worktrees(profile, item, run_id=run_id, reservation_id="")


def reserve_task_worktrees(profile: ProjectProfile, item: Dict[str, Any], reservation_id: str) -> Dict[str, Any]:
    """在任务进入 backlog 前预留并恢复受管 worktree。"""

    return _prepare_task_worktrees(profile, item, run_id="", reservation_id=reservation_id)


def readable_worktree_path(repo: Path, managed_name: str, target_id: str = "") -> Path:
    """返回冻结可读任务名对应的默认受管 worktree 路径。"""

    suffix = f"-{_slug(target_id)}" if target_id else ""
    return (_managed_worktree_root(repo.resolve()) / f"{_slug(managed_name)}{suffix}").resolve()


def _prepare_task_worktrees(
    profile: ProjectProfile,
    item: Dict[str, Any],
    *,
    run_id: str,
    reservation_id: str,
) -> Dict[str, Any]:
    try:
        plan = task_target_plan(profile, item, require_repo_paths=True)
    except TargetPlanError as exc:
        raise WorktreePreparationError(exc.code, exc.summary, exc.detail) from exc

    prepared: list[Dict[str, Any]] = []
    markers: list[str] = []
    for target in plan.ordered:
        result = _prepare_target(profile, item, target, run_id, reservation_id, plan.explicit)
        prepared.append(result)
        markers.append(result["ownership_marker"])

    by_id = {target["id"]: target for target in prepared}
    ordered = [by_id[target.id] for target in plan.ordered]
    target_plan = {
        "status": "ready",
        "explicit": plan.explicit,
        "ordered_target_ids": [target["id"] for target in ordered],
        "targets": prepared,
    }
    _apply_prepared_locations(item, prepared, plan.explicit)
    execution_root = str(profile.root_dir.resolve()) if plan.explicit else prepared[0]["worktree_path"]
    return {
        "status": "prepared",
        "target_plan": target_plan,
        "execution_root": execution_root,
        "ownership_markers": markers,
    }


def ownership_marker_for(repo_path: Path, task_id: str, target_id: str) -> Path:
    common_dir = _git(repo_path, "rev-parse", "--git-common-dir")
    if common_dir.returncode != 0:
        raise WorktreePreparationError("target_not_git_repo", f"Target 不是可用 Git 仓库：{repo_path}")
    raw = Path(common_dir.stdout.strip())
    resolved = raw if raw.is_absolute() else (repo_path / raw).resolve()
    return resolved / "loopforge-worktrees" / _slug(task_id) / f"{_slug(target_id)}.json"


def verify_worktree_ownership(profile: ProjectProfile, target: TaskTarget, task_id: str) -> Path:
    marker_path = ownership_marker_for(target.repo_path, task_id, target.id)
    marker = _read_marker(marker_path)
    expected = {
        "project_id": profile.project_id,
        "task_id": task_id,
        "target_id": target.id,
        "branch": target.branch,
    }
    if not _marker_matches(marker, expected) or str(marker.get("worktree_path") or "") != target.worktree_path:
        raise WorktreePreparationError(
            "worktree_not_owned",
            f"拒绝清理没有匹配 ownership marker 的 worktree：{target.worktree_path}",
            {"target": target.id},
        )
    return marker_path


def cleanup_managed_worktree(
    profile: ProjectProfile,
    *,
    task_id: str,
    target_id: str,
    repo_path: Path,
    branch: str,
    worktree_path: str,
    discard_changes: bool = False,
) -> Dict[str, Any]:
    """幂等清理 LoopForge 拥有的本地 worktree、分支与 ownership marker。"""

    repo = repo_path.resolve()
    marker_path = ownership_marker_for(repo, task_id, target_id)
    marker = _read_marker(marker_path)
    expected = {
        "project_id": profile.project_id,
        "task_id": task_id,
        "target_id": target_id,
        "branch": branch,
    }
    registered = _registered_worktree_for_branch(repo, branch) if branch else None
    worktree = Path(worktree_path).resolve() if worktree_path else registered
    branch_exists = bool(branch) and _branch_exists(repo, branch)
    worktree_exists = worktree is not None and worktree.exists()
    result: Dict[str, Any] = {
        "status": "completed",
        "summary": "本地 branch/worktree 清理完成。",
        "target": target_id,
        "repo_path": str(repo),
        "branch": branch,
        "worktree_path": str(worktree) if worktree is not None else "",
        "worktree_removed": False,
        "branch_deleted": False,
        "ownership_marker_deleted": False,
        "discard_confirmed": bool(discard_changes),
        "discarded_changes": False,
        "remote_branch_preserved": True,
    }

    if not is_managed_feature_branch(branch) or _git(repo, "check-ref-format", "--branch", branch).returncode != 0:
        return {
            **result,
            "status": "failed",
            "code": "managed_branch_invalid",
            "summary": f"拒绝清理非法或非 LoopForge 受管分支：{branch}",
        }
    managed_root = _managed_worktree_root(repo)
    if worktree is not None and worktree != managed_root and managed_root not in worktree.parents:
        return {
            **result,
            "status": "failed",
            "code": "worktree_path_unmanaged",
            "summary": f"拒绝清理 LoopForge 受管目录以外的 worktree：{worktree}",
        }

    if not marker:
        if not branch_exists and registered is None and not worktree_exists:
            return {**result, "summary": "本地受管资源此前已清理。", "already_cleaned": True}
        return {
            **result,
            "status": "failed",
            "code": "worktree_not_owned",
            "summary": f"拒绝清理没有匹配 ownership marker 的本地资源：{worktree or branch}",
        }
    if not _marker_matches(marker, expected):
        return {
            **result,
            "status": "failed",
            "code": "worktree_not_owned",
            "summary": f"拒绝清理 ownership marker 不匹配的本地资源：{worktree or branch}",
        }

    marker_worktree_raw = str(marker.get("worktree_path") or "").strip()
    marker_worktree = Path(marker_worktree_raw).resolve() if marker_worktree_raw else None
    if worktree is None:
        worktree = marker_worktree
        result["worktree_path"] = str(worktree) if worktree is not None else ""
        worktree_exists = worktree is not None and worktree.exists()
    if worktree is not None and worktree != managed_root and managed_root not in worktree.parents:
        return {
            **result,
            "status": "failed",
            "code": "worktree_path_unmanaged",
            "summary": f"拒绝清理 LoopForge 受管目录以外的 worktree：{worktree}",
        }
    if marker_worktree != worktree or (registered is not None and registered != worktree):
        return {
            **result,
            "status": "failed",
            "code": "worktree_not_owned",
            "summary": f"拒绝清理路径与 ownership marker 不匹配的 worktree：{worktree}",
        }

    if worktree_exists and worktree is not None:
        status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0:
            return {
                **result,
                "status": "failed",
                "code": "worktree_status_failed",
                "summary": "无法检查 worktree 是否存在未提交变化。",
                "detail": status.stderr.strip() or status.stdout.strip(),
            }
        dirty = bool(status.stdout.strip())
        if dirty and not discard_changes:
            return {
                **result,
                "status": "failed",
                "code": "dirty_worktree_cleanup_requires_discard",
                "summary": "worktree 存在未提交变化，必须显式确认 discard_changes 才能清理。",
                "required_action": "确认允许丢弃未提交变化后重试清理。",
            }
        remove_args = ["worktree", "remove"]
        if dirty:
            remove_args.append("--force")
        remove_args.append(str(worktree))
        remove = _git(repo, *remove_args)
        if remove.returncode != 0:
            return {
                **result,
                "status": "failed",
                "code": "worktree_remove_failed",
                "summary": "worktree 清理失败。",
                "detail": remove.stderr.strip() or remove.stdout.strip(),
            }
        result["worktree_removed"] = True
        result["discarded_changes"] = dirty

    if branch_exists:
        current_branch = _git(repo, "branch", "--show-current")
        if current_branch.returncode == 0 and current_branch.stdout.strip() == branch:
            return {
                **result,
                "status": "failed",
                "code": "branch_in_use",
                "summary": "不能删除当前正在使用的 feature branch。",
            }
        delete = _git(repo, "branch", "-D", branch)
        if delete.returncode != 0:
            return {
                **result,
                "status": "failed",
                "code": "branch_delete_failed",
                "summary": "feature branch 删除失败。",
                "detail": delete.stderr.strip() or delete.stdout.strip(),
            }
        result["branch_deleted"] = True

    marker_path.unlink(missing_ok=True)
    result["ownership_marker_deleted"] = True
    return result


def _prepare_target(
    profile: ProjectProfile,
    item: Dict[str, Any],
    target: TaskTarget,
    run_id: str,
    reservation_id: str,
    explicit: bool,
) -> Dict[str, Any]:
    repo = target.repo_path.resolve()
    if _git(repo, "rev-parse", "--show-toplevel").returncode != 0:
        raise WorktreePreparationError("target_not_git_repo", f"Target 不是可用 Git 仓库：{repo}", {"target": target.id})
    task_id = str(item.get("id") or "task")
    managed_name = str(item.get("_managed_name") or "").strip()
    branch = target.branch or _default_branch(profile.project_id, task_id, managed_name, target.id, explicit)
    managed_root = _managed_worktree_root(repo)
    worktree = (
        Path(target.worktree_path).resolve()
        if target.worktree_path
        else _default_worktree(repo, task_id, managed_name, target.id, explicit)
    )
    if worktree != managed_root and managed_root not in worktree.parents:
        raise WorktreePreparationError(
            "worktree_path_unmanaged",
            f"worktree_path 必须位于 LoopForge 受管目录：{managed_root}",
            {"target": target.id, "worktree_path": str(worktree)},
        )
    marker = ownership_marker_for(repo, task_id, target.id)
    owned = _read_marker(marker)
    expected_owner = {"project_id": profile.project_id, "task_id": task_id, "target_id": target.id, "branch": branch}

    registered = _registered_worktree_for_branch(repo, branch)
    if registered:
        if not _marker_matches(owned, expected_owner):
            raise WorktreePreparationError(
                "worktree_not_owned",
                f"feature branch 已在非 LoopForge 管理的 worktree 中使用：{branch}",
                {"target": target.id, "worktree_path": str(registered)},
            )
        worktree = registered
    elif _branch_exists(repo, branch):
        if not _marker_matches(owned, expected_owner):
            raise WorktreePreparationError(
                "branch_not_owned",
                f"feature branch 已存在但没有匹配的 LoopForge ownership marker：{branch}",
                {"target": target.id},
            )
        _ensure_empty_destination(worktree, target.id)
        _run_git_or_raise(repo, target.id, "worktree", "add", str(worktree), branch)
    else:
        if _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{target.target_branch}").returncode != 0:
            raise WorktreePreparationError(
                "target_branch_missing",
                f"Target 基线分支不存在：{target.target_branch}",
                {"target": target.id},
            )
        _ensure_empty_destination(worktree, target.id)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        _run_git_or_raise(repo, target.id, "worktree", "add", "-b", branch, str(worktree), target.target_branch)

    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps(
            {
                **expected_owner,
                "repo_path": str(repo),
                "worktree_path": str(worktree),
                "target_branch": target.target_branch,
                "run_id": run_id,
                "reservation_id": reservation_id or str(owned.get("reservation_id") or ""),
                "created_at": owned.get("created_at") if isinstance(owned, dict) and owned.get("created_at") else utc_now(),
                "updated_at": utc_now(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    payload = target.to_payload()
    payload.update({"branch": branch, "worktree_path": str(worktree), "ownership_marker": str(marker)})
    return payload


def _apply_prepared_locations(item: Dict[str, Any], prepared: list[Dict[str, Any]], explicit: bool) -> None:
    if not explicit:
        item["branch"] = prepared[0]["branch"]
        item["feature_branch"] = prepared[0]["branch"]
        item["worktree_path"] = prepared[0]["worktree_path"]
        return
    by_id = {target["id"]: target for target in prepared}
    updated: list[Any] = []
    for index, raw in enumerate(item.get("targets") or []):
        payload = dict(raw) if isinstance(raw, dict) else {"project": str(raw)}
        target_id = str(payload.get("id") or payload.get("project") or f"target-{index + 1}")
        prepared_target = by_id.get(target_id)
        if prepared_target:
            for key in ("repo_path", "branch", "worktree_path"):
                payload[key] = prepared_target[key]
        updated.append(payload)
    item["targets"] = updated


def _default_branch(project_id: str, task_id: str, managed_name: str, target_id: str, explicit: bool) -> str:
    if managed_name:
        return readable_feature_branch(managed_name, target_id if explicit else "")
    return managed_feature_branch(project_id, task_id, target_id if explicit else "")


def _default_worktree(repo: Path, task_id: str, managed_name: str, target_id: str, explicit: bool) -> Path:
    if managed_name:
        return readable_worktree_path(repo, managed_name, target_id if explicit else "")
    return (_managed_worktree_root(repo) / f"{_slug(task_id)}-{_slug(target_id)}").resolve()


def _managed_worktree_root(repo: Path) -> Path:
    return (repo.parent / ".loopforge-worktrees" / repo.name).resolve()


def _ensure_empty_destination(path: Path, target_id: str) -> None:
    if path.exists():
        raise WorktreePreparationError(
            "worktree_path_occupied",
            f"预分配 worktree 路径已存在且未登记：{path}",
            {"target": target_id},
        )


def _registered_worktree_for_branch(repo: Path, branch: str) -> Path | None:
    result = _git(repo, "worktree", "list", "--porcelain")
    if result.returncode != 0:
        return None
    current_path: Path | None = None
    expected_ref = f"refs/heads/{branch}"
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = Path(line.split(" ", 1)[1]).resolve()
        elif line == f"branch {expected_ref}" and current_path is not None:
            return current_path
    return None


def _branch_exists(repo: Path, branch: str) -> bool:
    return _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0


def _run_git_or_raise(repo: Path, target_id: str, *args: str) -> None:
    result = _git(repo, *args)
    if result.returncode != 0:
        summary = result.stderr.strip() or result.stdout.strip() or "Git 命令失败"
        raise WorktreePreparationError("worktree_prepare_failed", summary, {"target": target_id, "command": list(args)})


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True)


def _read_marker(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _marker_matches(marker: Dict[str, Any], expected: Dict[str, str]) -> bool:
    return bool(marker) and all(str(marker.get(key) or "") == value for key, value in expected.items())


def _slug(value: str) -> str:
    return slug_branch_segment(value)
