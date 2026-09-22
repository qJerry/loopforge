"""LoopForge 单任务文件的受控 Git 发布。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .config import ProjectProfile
from .domain import utc_now
from .managed_branches import is_managed_feature_branch
from .task_store import TaskFileChange, create_task_file, mutate_task_file


TASK_REMOTE = "origin"
TASK_TARGET_BRANCH = "main"
_FULL_REVISION = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SAFE_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class TaskGitError(RuntimeError):
    """Git 发布失败的稳定错误合同。"""

    def __init__(self, code: str, summary: str, detail: str = "") -> None:
        self.code = code
        self.summary = summary
        self.detail = detail
        message = f"{summary}：{detail}" if detail else summary
        super().__init__(message)


@dataclass(frozen=True)
class PublishResult:
    status: str
    commit: str
    remote_revision: str
    replayed: bool
    blocker: Optional[Dict[str, Any]] = None


@dataclass(frozen=True)
class FeaturePushResult:
    status: str
    branch: str
    revision: str
    remote: str


def require_clean_synced_main(profile: ProjectProfile) -> str:
    """在任何任务文件落盘前确认 main 可由任务发布器安全接管。"""

    repo = profile.root_dir.resolve()
    _require_git_repo(repo)
    _require_main_branch(repo)
    _require_remote(repo, TASK_REMOTE)
    existing_blocker = read_sync_blocker(profile)
    if existing_blocker is not None:
        raise TaskGitError(
            "sync_blocked",
            "项目存在尚未人工解决的 Git 同步阻塞",
            str(existing_blocker.get("task_id") or ""),
        )
    changed_paths = _working_change_paths(repo)
    if changed_paths:
        raise TaskGitError("dirty_worktree", "main 工作区存在未批准变化", str(sorted(changed_paths)))
    base_revision = _revision(repo, "HEAD")
    cached_remote = _revision(repo, f"refs/remotes/{TASK_REMOTE}/{TASK_TARGET_BRANCH}")
    if base_revision != cached_remote:
        raise TaskGitError(
            "main_not_synced",
            "main 存在未发布提交或远端基线未同步",
            f"HEAD={base_revision} origin/main={cached_remote}",
        )
    return base_revision


def publish_main_change(
    profile: ProjectProfile,
    change: TaskFileChange,
    message: str,
) -> PublishResult:
    """精确提交一次 Task Store 变化并显式推送到 origin/main。"""

    return _publish_main_change(profile, change, message, require_main=True)


def publish_new_task_from_isolated_main(
    profile: ProjectProfile,
    task: Dict[str, Any],
    message: str,
) -> PublishResult:
    """在一次性 detached worktree 中创建并发布新任务，再快进用户 main。"""

    repo = profile.root_dir.resolve()
    _require_git_repo(repo)
    _require_main_branch(repo)
    _require_remote(repo, TASK_REMOTE)
    existing_blocker = read_sync_blocker(profile)
    if existing_blocker is not None:
        raise TaskGitError(
            "sync_blocked",
            "项目存在尚未人工解决的 Git 同步阻塞",
            str(existing_blocker.get("task_id") or ""),
        )
    if not isinstance(message, str) or not message.strip():
        raise TaskGitError("invalid_commit_message", "任务发布提交信息不能为空")

    cached_remote = _revision(repo, f"refs/remotes/{TASK_REMOTE}/{TASK_TARGET_BRANCH}")

    worktree = Path(tempfile.mkdtemp(prefix=f"loopforge-publish-{task.get('id', 'task')}-"))
    worktree.rmdir()
    added = False
    publish_error: Optional[Exception] = None
    published: Optional[PublishResult] = None
    try:
        _git_or_raise(
            repo,
            "publish_worktree_create_failed",
            "无法创建临时任务发布 worktree",
            "worktree",
            "add",
            "--detach",
            str(worktree),
            cached_remote,
        )
        added = True
        isolated_profile = replace(
            profile,
            root_dir=worktree,
            state_dir=profile.loopforge_dir.resolve(),
            report_dir=profile.reports_dir.resolve(),
        )
        change = create_task_file(isolated_profile, task)
        published = _publish_main_change(isolated_profile, change, message, require_main=False)
    except Exception as exc:
        publish_error = exc
    finally:
        cleanup_error = _remove_publish_worktree(repo, worktree, added=added)

    if cleanup_error is not None:
        if publish_error is not None:
            raise cleanup_error from publish_error
        raise cleanup_error
    if publish_error is not None:
        raise publish_error
    if published is None:
        raise TaskGitError("publish_result_missing", "临时任务发布没有返回结果")

    if published.status == "published" and _revision(repo, "HEAD") == cached_remote:
        _git_or_raise(
            repo,
            "main_fast_forward_failed",
            "任务已发布，但用户 main 无法安全快进",
            "merge",
            "--ff-only",
            published.remote_revision,
        )
    return published


def publish_task_mutation_from_isolated_main(
    profile: ProjectProfile,
    task_id: str,
    expected_version: int,
    allowed_states: Iterable[str],
    fields: Dict[str, Any],
    message: str,
    *,
    remove_fields: Iterable[str] = (),
    permit_sync_blocker_task_id: str = "",
) -> tuple[PublishResult, TaskFileChange]:
    """在一次性 detached worktree 中变更一个任务，再快进用户 main。"""

    repo = profile.root_dir.resolve()
    _require_git_repo(repo)
    _require_main_branch(repo)
    _require_remote(repo, TASK_REMOTE)
    _require_sync_blocker_clear_or_permitted(profile, permit_sync_blocker_task_id)
    if not isinstance(message, str) or not message.strip():
        raise TaskGitError("invalid_commit_message", "任务发布提交信息不能为空")

    cached_remote = _revision(repo, f"refs/remotes/{TASK_REMOTE}/{TASK_TARGET_BRANCH}")

    worktree = Path(tempfile.mkdtemp(prefix=f"loopforge-publish-{task_id}-"))
    worktree.rmdir()
    added = False
    publish_error: Optional[Exception] = None
    published: Optional[PublishResult] = None
    change: Optional[TaskFileChange] = None
    try:
        _git_or_raise(
            repo,
            "publish_worktree_create_failed",
            "无法创建临时任务发布 worktree",
            "worktree",
            "add",
            "--detach",
            str(worktree),
            cached_remote,
        )
        added = True
        isolated_profile = replace(
            profile,
            root_dir=worktree,
            state_dir=profile.loopforge_dir.resolve(),
            report_dir=profile.reports_dir.resolve(),
        )
        change = mutate_task_file(
            isolated_profile,
            task_id,
            expected_version,
            allowed_states,
            fields,
            remove_fields=remove_fields,
        )
        published = _publish_main_change(
            isolated_profile,
            change,
            message,
            require_main=False,
            permit_sync_blocker_task_id=permit_sync_blocker_task_id,
        )
    except Exception as exc:
        publish_error = exc
    finally:
        cleanup_error = _remove_publish_worktree(repo, worktree, added=added)

    if cleanup_error is not None:
        if publish_error is not None:
            raise cleanup_error from publish_error
        raise cleanup_error
    if publish_error is not None:
        raise publish_error
    if published is None or change is None:
        raise TaskGitError("publish_result_missing", "临时任务发布没有返回结果")

    if published.status == "published" and _revision(repo, "HEAD") == cached_remote:
        _git_or_raise(
            repo,
            "main_fast_forward_failed",
            "任务已发布，但用户 main 无法安全快进",
            "merge",
            "--ff-only",
            published.remote_revision,
        )
    return published, change


def _publish_main_change(
    profile: ProjectProfile,
    change: TaskFileChange,
    message: str,
    *,
    require_main: bool,
    permit_sync_blocker_task_id: str = "",
) -> PublishResult:
    """在指定 Git worktree 中执行精确的任务文件提交与推送。"""

    repo = profile.root_dir.resolve()
    _require_git_repo(repo)
    if require_main:
        _require_main_branch(repo)
    _require_remote(repo, TASK_REMOTE)
    _require_sync_blocker_clear_or_permitted(profile, permit_sync_blocker_task_id)
    if not isinstance(message, str) or not message.strip():
        raise TaskGitError("invalid_commit_message", "任务发布提交信息不能为空")

    allowed_paths = _normalize_allowed_paths(repo, change.paths)
    actual_paths = _working_change_paths(repo)
    if actual_paths != allowed_paths:
        missing = sorted(allowed_paths - actual_paths)
        tracked_unrelated = sorted(_tracked_change_paths(repo).difference(allowed_paths))
        detail_parts = []
        if tracked_unrelated:
            detail_parts.append(f"无关已跟踪路径 {tracked_unrelated}")
        if missing:
            detail_parts.append(f"缺少预期路径 {missing}")
        if tracked_unrelated or missing:
            raise TaskGitError("dirty_worktree", "工作区包含未批准变化", "；".join(detail_parts))

    base_revision = _revision(repo, "HEAD")
    cached_remote = _revision(repo, f"refs/remotes/{TASK_REMOTE}/{TASK_TARGET_BRANCH}")
    if base_revision != cached_remote:
        raise TaskGitError(
            "main_not_synced",
            "main 存在未发布提交或远端基线未同步",
            f"HEAD={base_revision} origin/main={cached_remote}",
        )

    _git_or_raise(repo, "stage_failed", "无法暂存任务文件", "add", "--", *sorted(allowed_paths))
    _git_or_raise(
        repo,
        "commit_failed",
        "无法创建任务发布提交",
        "commit",
        "--only",
        "-m",
        message.strip(),
        "--",
        *sorted(allowed_paths),
    )
    local_revision = _revision(repo, "HEAD")
    _verify_commit_paths(repo, local_revision, allowed_paths)

    first_push = _git(repo, "push", TASK_REMOTE, f"HEAD:refs/heads/{TASK_TARGET_BRANCH}")
    if first_push.returncode == 0:
        return PublishResult("published", local_revision, local_revision, False)

    fetch = _git(repo, "fetch", "--no-tags", TASK_REMOTE, TASK_TARGET_BRANCH)
    if fetch.returncode != 0:
        blocker = _write_sync_blocker(
            profile,
            change,
            base_revision,
            local_revision,
            "",
            allowed_paths,
            set(),
            "fetch_failed",
            _command_detail(fetch),
            replayed=False,
        )
        return PublishResult("sync_blocked", local_revision, "", False, blocker)
    remote_revision = _revision(repo, f"refs/remotes/{TASK_REMOTE}/{TASK_TARGET_BRANCH}")

    # push 结果可能丢失，但远端已经包含本地提交；此时视为发布成功。
    if _is_ancestor(repo, local_revision, remote_revision):
        return PublishResult("published", local_revision, remote_revision, False)
    if not _is_ancestor(repo, base_revision, remote_revision):
        blocker = _write_sync_blocker(
            profile,
            change,
            base_revision,
            local_revision,
            remote_revision,
            allowed_paths,
            set(),
            "remote_diverged",
            _command_detail(first_push),
            replayed=False,
        )
        return PublishResult("sync_blocked", local_revision, remote_revision, False, blocker)

    remote_paths = _changed_paths(repo, base_revision, remote_revision)
    conflict_paths = allowed_paths.intersection(remote_paths)
    if conflict_paths:
        blocker = _write_sync_blocker(
            profile,
            change,
            base_revision,
            local_revision,
            remote_revision,
            allowed_paths,
            conflict_paths,
            "path_conflict",
            _command_detail(first_push),
            replayed=False,
        )
        return PublishResult("sync_blocked", local_revision, remote_revision, False, blocker)

    rebase = _git(repo, "rebase", f"{TASK_REMOTE}/{TASK_TARGET_BRANCH}")
    if rebase.returncode != 0:
        _git(repo, "rebase", "--abort")
        blocker = _write_sync_blocker(
            profile,
            change,
            base_revision,
            local_revision,
            remote_revision,
            allowed_paths,
            set(),
            "replay_failed",
            _command_detail(rebase),
            replayed=True,
        )
        return PublishResult("sync_blocked", local_revision, remote_revision, True, blocker)

    replayed_revision = _revision(repo, "HEAD")
    _verify_single_replayed_commit(repo, replayed_revision, allowed_paths)
    second_push = _git(repo, "push", TASK_REMOTE, f"HEAD:refs/heads/{TASK_TARGET_BRANCH}")
    if second_push.returncode == 0:
        return PublishResult("published", replayed_revision, replayed_revision, True)

    _git(repo, "fetch", "--no-tags", TASK_REMOTE, TASK_TARGET_BRANCH)
    second_remote = _optional_revision(repo, f"refs/remotes/{TASK_REMOTE}/{TASK_TARGET_BRANCH}") or remote_revision
    if _is_ancestor(repo, replayed_revision, second_remote):
        return PublishResult("published", replayed_revision, second_remote, True)
    blocker = _write_sync_blocker(
        profile,
        change,
        base_revision,
        replayed_revision,
        second_remote,
        allowed_paths,
        set(),
        "second_push_failed",
        _command_detail(second_push),
        replayed=True,
    )
    return PublishResult("sync_blocked", replayed_revision, second_remote, True, blocker)


def push_managed_feature_revision(
    profile: ProjectProfile,
    binding: Dict[str, Any],
    revision: str,
) -> FeaturePushResult:
    """验证并推送受管 feature branch 的确定 revision。"""

    repo = Path(str(binding.get("repo_path") or profile.root_dir)).resolve()
    remote = str(binding.get("remote") or TASK_REMOTE).strip()
    branch = str(binding.get("feature_branch") or binding.get("branch") or "").strip()
    if remote != TASK_REMOTE:
        raise TaskGitError("unmanaged_remote", "任务只允许发布到 origin", remote)
    if not is_managed_feature_branch(branch):
        raise TaskGitError("unmanaged_feature_branch", "feature branch 不是 LoopForge 受管分支", branch)
    _require_git_repo(repo)
    _require_remote(repo, remote)
    if _git(repo, "check-ref-format", "--branch", branch).returncode != 0:
        raise TaskGitError("invalid_feature_branch", "feature branch 名称非法", branch)
    if not isinstance(revision, str) or not _FULL_REVISION.fullmatch(revision):
        raise TaskGitError("invalid_feature_revision", "feature revision 必须是完整 commit SHA", str(revision))
    branch_revision = _optional_revision(repo, f"refs/heads/{branch}")
    if branch_revision != revision:
        raise TaskGitError(
            "feature_revision_mismatch",
            "feature branch tip 与待发布 revision 不一致",
            f"branch={branch_revision or 'missing'} revision={revision}",
        )
    if _git(repo, "cat-file", "-e", f"{revision}^{{commit}}").returncode != 0:
        raise TaskGitError("feature_revision_missing", "feature revision 不是本地 commit", revision)

    pushed = _git(repo, "push", remote, f"refs/heads/{branch}:refs/heads/{branch}")
    if pushed.returncode != 0:
        raise TaskGitError("feature_push_failed", "受管 feature branch 推送失败", _command_detail(pushed))
    advertised = _git(repo, "ls-remote", remote, f"refs/heads/{branch}")
    if advertised.returncode != 0:
        raise TaskGitError("feature_verify_failed", "无法验证远端 feature revision", _command_detail(advertised))
    remote_revision = advertised.stdout.split()[0] if advertised.stdout.split() else ""
    if remote_revision != revision:
        raise TaskGitError(
            "feature_remote_mismatch",
            "远端 feature revision 与引用不一致",
            f"remote={remote_revision or 'missing'} revision={revision}",
        )
    return FeaturePushResult("pushed", branch, revision, remote)


def read_sync_blocker(profile: ProjectProfile, task_id: str = "") -> Optional[Dict[str, Any]]:
    """读取指定任务或项目当前最新的同步阻塞。"""

    directory = _sync_blocker_dir(profile)
    if task_id:
        path = directory / f"{_validated_task_id(task_id)}.json"
        return _read_json_object(path)
    if not directory.exists():
        return None
    blockers = [payload for path in sorted(directory.glob("*.json")) if (payload := _read_json_object(path))]
    if not blockers:
        return None
    blockers.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    return blockers[0]


def _require_sync_blocker_clear_or_permitted(profile: ProjectProfile, permitted_task_id: str = "") -> None:
    existing_blocker = read_sync_blocker(profile)
    if existing_blocker is None:
        return
    blocker_task_id = str(existing_blocker.get("task_id") or "")
    if permitted_task_id and blocker_task_id == permitted_task_id:
        return
    raise TaskGitError(
        "sync_blocked",
        "项目存在尚未人工解决的 Git 同步阻塞",
        blocker_task_id,
    )


def clear_sync_blocker(
    profile: ProjectProfile,
    task_id: str,
    *,
    resolved_by: str,
    resolution: str,
) -> Dict[str, Any]:
    """在人工提供解决证据后清理本地同步阻塞标记。"""

    if not str(resolved_by).strip() or not str(resolution).strip():
        raise TaskGitError("missing_resolution_evidence", "清理同步阻塞必须提供处理人和解决说明")
    path = _sync_blocker_dir(profile) / f"{_validated_task_id(task_id)}.json"
    blocker = _read_json_object(path)
    if blocker is None:
        raise TaskGitError("sync_blocker_not_found", "同步阻塞记录不存在", task_id)
    path.unlink()
    return {
        "status": "cleared",
        "task_id": task_id,
        "resolved_by": str(resolved_by).strip(),
        "resolution": str(resolution).strip(),
        "resolved_at": utc_now(),
        "blocker": blocker,
    }


def _require_git_repo(repo: Path) -> None:
    result = _git(repo, "rev-parse", "--show-toplevel")
    if result.returncode != 0 or Path(result.stdout.strip()).resolve() != repo:
        raise TaskGitError("not_git_root", "项目根目录不是可用 Git 主工作树", str(repo))


def _require_main_branch(repo: Path) -> None:
    branch = _git(repo, "branch", "--show-current")
    current = branch.stdout.strip() if branch.returncode == 0 else ""
    if current != TASK_TARGET_BRANCH:
        raise TaskGitError("wrong_target_branch", "任务账本只能从 main 分支发布", current or "detached")


def _require_remote(repo: Path, remote: str) -> None:
    result = _git(repo, "remote", "get-url", remote)
    if result.returncode != 0 or not result.stdout.strip():
        raise TaskGitError("task_remote_missing", f"缺少任务发布远端 {remote}", _command_detail(result))


def _normalize_allowed_paths(repo: Path, paths: Iterable[Path]) -> set[str]:
    normalized: set[str] = set()
    for raw in paths:
        path = Path(raw)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise TaskGitError("unsafe_publish_path", "任务发布路径必须是仓库内相对路径", str(path))
        candidate = (repo / path).resolve(strict=False)
        try:
            relative = candidate.relative_to(repo)
        except ValueError as exc:
            raise TaskGitError("unsafe_publish_path", "任务发布路径越过仓库边界", str(path)) from exc
        normalized.add(relative.as_posix())
    if not normalized:
        raise TaskGitError("empty_publish_paths", "任务发布至少需要一个文件路径")
    return normalized


def _working_change_paths(repo: Path) -> set[str]:
    unmerged = _git_or_raise(repo, "git_status_failed", "无法检查 Git 冲突", "diff", "--name-only", "--diff-filter=U")
    if unmerged.stdout.strip():
        raise TaskGitError("unmerged_worktree", "main 工作区存在未解决冲突", unmerged.stdout.strip())
    tracked = _git_or_raise(repo, "git_status_failed", "无法读取已跟踪变化", "diff", "HEAD", "--name-only", "--no-renames")
    untracked = _git_or_raise(repo, "git_status_failed", "无法读取未跟踪变化", "ls-files", "--others", "--exclude-standard")
    return {line.strip() for line in (tracked.stdout + "\n" + untracked.stdout).splitlines() if line.strip()}


def _tracked_change_paths(repo: Path) -> set[str]:
    tracked = _git_or_raise(repo, "git_status_failed", "无法读取已跟踪变化", "diff", "HEAD", "--name-only", "--no-renames")
    return {line.strip() for line in tracked.stdout.splitlines() if line.strip()}


def _remove_publish_worktree(repo: Path, worktree: Path, *, added: bool) -> Optional[TaskGitError]:
    """尽最大努力清除临时 worktree 的目录与 Git 注册记录。"""

    detail = ""
    if added:
        removed = _git(repo, "worktree", "remove", "--force", str(worktree))
        if removed.returncode != 0:
            detail = _command_detail(removed)
    if worktree.exists():
        shutil.rmtree(worktree, ignore_errors=True)
    pruned = _git(repo, "worktree", "prune")
    if pruned.returncode != 0 and not detail:
        detail = _command_detail(pruned)
    registered = str(worktree.resolve()) in _git(repo, "worktree", "list", "--porcelain").stdout
    if worktree.exists() or registered:
        suffix = f"；{detail}" if detail else ""
        return TaskGitError(
            "publish_worktree_cleanup_failed",
            "临时任务发布 worktree 未能立即清理",
            f"{worktree}{suffix}",
        )
    return None


def _verify_commit_paths(repo: Path, revision: str, allowed_paths: set[str]) -> None:
    result = _git_or_raise(
        repo,
        "commit_verify_failed",
        "无法验证任务发布提交路径",
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "--no-renames",
        "-r",
        revision,
    )
    actual = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if actual != allowed_paths:
        raise TaskGitError(
            "commit_path_pollution",
            "任务发布提交包含未批准路径",
            f"expected={sorted(allowed_paths)} actual={sorted(actual)}",
        )


def _verify_single_replayed_commit(repo: Path, revision: str, allowed_paths: set[str]) -> None:
    count = _git_or_raise(
        repo,
        "replay_verify_failed",
        "无法验证自动重放提交数量",
        "rev-list",
        "--count",
        f"{TASK_REMOTE}/{TASK_TARGET_BRANCH}..HEAD",
    )
    if count.stdout.strip() != "1":
        raise TaskGitError("replay_commit_count", "自动重放只允许唯一 LoopForge 提交", count.stdout.strip())
    _verify_commit_paths(repo, revision, allowed_paths)


def _changed_paths(repo: Path, base_revision: str, remote_revision: str) -> set[str]:
    result = _git_or_raise(
        repo,
        "remote_diff_failed",
        "无法比较远端 main 变化",
        "diff",
        "--name-only",
        "--no-renames",
        f"{base_revision}..{remote_revision}",
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _write_sync_blocker(
    profile: ProjectProfile,
    change: TaskFileChange,
    base_revision: str,
    local_revision: str,
    remote_revision: str,
    allowed_paths: set[str],
    conflict_paths: set[str],
    reason: str,
    detail: str,
    *,
    replayed: bool,
) -> Dict[str, Any]:
    task_id = _validated_task_id(change.task_id)
    payload = {
        "schema_version": 1,
        "project_id": profile.project_id,
        "task_id": task_id,
        "status": "sync_blocked",
        "reason": reason,
        "detail": detail,
        "base_revision": base_revision,
        "local_revision": local_revision,
        "remote_revision": remote_revision,
        "allowed_paths": sorted(allowed_paths),
        "conflict_paths": sorted(conflict_paths),
        "operation": change.operation,
        "replayed": replayed,
        "created_at": utc_now(),
    }
    path = _sync_blocker_dir(profile) / f"{task_id}.json"
    _write_json_atomic(path, payload)
    return payload


def _sync_blocker_dir(profile: ProjectProfile) -> Path:
    return profile.loopforge_dir / "sync-blocked"


def _validated_task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not _SAFE_TASK_ID.fullmatch(task_id):
        raise TaskGitError("invalid_task_id", "同步阻塞 task_id 非法", str(task_id))
    return task_id


def _read_json_object(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskGitError("invalid_sync_blocker", "同步阻塞记录无法读取", f"{path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise TaskGitError("invalid_sync_blocker", "同步阻塞记录必须是 JSON object", str(path))
    return payload


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _revision(repo: Path, ref: str) -> str:
    value = _optional_revision(repo, ref)
    if not value:
        raise TaskGitError("git_revision_missing", "Git revision 不存在", ref)
    return value


def _optional_revision(repo: Path, ref: str) -> str:
    result = _git(repo, "rev-parse", "--verify", ref)
    return result.stdout.strip() if result.returncode == 0 else ""


def _is_ancestor(repo: Path, ancestor: str, descendant: str) -> bool:
    if not ancestor or not descendant:
        return False
    return _git(repo, "merge-base", "--is-ancestor", ancestor, descendant).returncode == 0


def _git_or_raise(repo: Path, code: str, summary: str, *args: str) -> subprocess.CompletedProcess[str]:
    result = _git(repo, *args)
    if result.returncode != 0:
        raise TaskGitError(code, summary, _command_detail(result))
    return result


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True)


def _command_detail(result: subprocess.CompletedProcess[str]) -> str:
    return result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
