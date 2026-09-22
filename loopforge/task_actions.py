"""任务人工动作、合入与清理合同。"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .config import ProjectProfile
from .dev_tasks import load_dev_tasks, update_item
from .domain import utc_now
from .task_publisher import TaskPublisherError, load_published_reservation_for_task
from .targets import TargetPlanError, TaskTarget, task_target_plan
from .worktrees import cleanup_managed_worktree


REVIEW_DECISIONS = {"accept", "reject_code", "reject_spec"}


def review_task(profile: ProjectProfile, item_id: str, decision: str, feedback: str = "") -> Dict[str, Any]:
    task = _raw_task(profile, item_id)
    previous_state = str(task.get("status") or "")
    clean_decision = decision.strip()
    clean_feedback = feedback.strip()
    if clean_decision not in REVIEW_DECISIONS:
        raise ValueError(f"非法验收动作：{clean_decision}")
    if previous_state != "ready_for_review":
        raise ValueError("只有 ready_for_review 任务允许验收或驳回。")

    now = utc_now()
    if clean_decision == "accept":
        fields = {
            "status": "accepted",
            "review_decision": "accepted",
            "review_feedback": clean_feedback,
            "review_feedback_type": "accept",
            "reviewed_at": now,
            "accepted_at": now,
        }
        if task.get("review_required") is True:
            fields["documentation_reviewed_at"] = now
        _clear_action_blockers(fields)
        updated = update_item(profile, item_id, **fields)
        return _action_result("completed", "验收通过，任务已进入 accepted。", updated, previous_state)

    if not clean_feedback:
        raise ValueError("驳回必须填写反馈。")

    if clean_decision == "reject_code":
        fields = {
            "status": "coding",
            "review_decision": "rejected",
            "review_feedback": clean_feedback,
            "review_feedback_type": "code",
            "reviewed_at": now,
        }
        _clear_action_blockers(fields)
        updated = update_item(profile, item_id, **fields)
        return _action_result("completed", "已驳回为代码问题，任务回到开发执行。", updated, previous_state)

    blocker = _blocker(
        "review_spec_change",
        clean_feedback,
        "请先在需求校准循环中更新模块三件套，再继续开发。",
    )
    updated = update_item(
        profile,
        item_id,
        status="spec_blocked",
        review_decision="rejected",
        review_feedback=clean_feedback,
        review_feedback_type="spec",
        reviewed_at=now,
        blockers=[blocker],
        blocker_reason=clean_feedback,
        required_action=blocker["resolution"],
    )
    return _action_result("completed", "已驳回为需求或规则问题，任务回到需求校准。", updated, previous_state)


def merge_task(
    profile: ProjectProfile,
    item_id: str,
    target_branch: str = "",
    merge_method: str = "",
) -> Dict[str, Any]:
    task = _raw_task(profile, item_id)
    previous_state = str(task.get("status") or "")
    if previous_state != "accepted":
        raise ValueError("只有 accepted 任务允许合入。")

    try:
        plan = task_target_plan(
            profile,
            task,
            target_branch_override=target_branch,
            merge_method_override=merge_method,
            require_repo_paths=True,
        )
    except TargetPlanError as exc:
        return _merge_failed(profile, item_id, previous_state, exc.summary, {"code": exc.code, **exc.detail})

    target_results: List[Dict[str, Any]] = []
    for target in plan.ordered:
        target_result = _merge_one_target(target, task, item_id, allow_loopforge_runtime=profile.worktree_managed)
        target_results.append(target_result)
        if target_result["status"] == "failed":
            summary = f"{target.id} 合入失败：{target_result['summary']}"
            return _merge_failed(
                profile,
                item_id,
                previous_state,
                summary,
                {"failed_target": target.id, "targets": target_results},
                target_id=target.id,
                partial_results=target_results,
            )

    merge_result = _combined_merge_result(target_results, plan.ordered)
    fields = {
        "status": "merged",
        "merged_at": utc_now(),
        "merge_method": merge_result.get("merge_method"),
        "merge_result": merge_result,
    }
    if not plan.explicit:
        fields["target_branch"] = merge_result.get("target_branch")
        fields["branch"] = merge_result.get("branch")
    updated_targets = _targets_with_action_results(task, target_results, "merge")
    if updated_targets is not None:
        fields["targets"] = updated_targets
    _clear_action_blockers(fields)
    updated = update_item(profile, item_id, **fields)
    summary = "本地 squash merge 已完成，任务进入 merged。" if merge_result["status"] == "merged" else "任务已标记为 merged，feature branch 此前已合入。"
    return _action_result("completed", summary, updated, previous_state, merge_result=merge_result)


def cleanup_task(profile: ProjectProfile, item_id: str, *, discard_changes: bool = False) -> Dict[str, Any]:
    task = _raw_task(profile, item_id)
    managed_resources = profile.worktree_managed or task.get("schema_version") == 2
    previous_state = str(task.get("status") or "")
    if previous_state != "merged":
        raise ValueError("只有 merged 任务允许清理。")

    try:
        cleanup_targets = _cleanup_target_order(profile, task)
    except TargetPlanError as exc:
        return _cleanup_failed(profile, item_id, previous_state, exc.summary, {"code": exc.code, **exc.detail})

    cleanup_results: List[Dict[str, Any]] = []
    for target in cleanup_targets:
        target_result = _cleanup_one_target(
            profile,
            target,
            item_id,
            discard_changes=discard_changes,
            managed_resources=managed_resources,
        )
        cleanup_results.append(target_result)
        if target_result["status"] == "failed":
            summary = f"{target.id} 清理失败：{target_result['summary']}"
            return _cleanup_failed(
                profile,
                item_id,
                previous_state,
                summary,
                {"failed_target": target.id, "targets": cleanup_results},
                target_id=target.id,
                required_action=str(target_result.get("required_action") or ""),
            )

    cleanup_result = _combined_cleanup_result(cleanup_results, cleanup_targets)

    fields = {
        "status": "completed",
        "completed_at": utc_now(),
        "cleanup_result": cleanup_result,
        "discard_confirmed": bool(discard_changes),
    }
    updated_targets = _targets_with_action_results(task, cleanup_results, "cleanup")
    if updated_targets is not None:
        fields["targets"] = updated_targets
    _clear_action_blockers(fields)
    updated = update_item(profile, item_id, **fields)
    return _action_result(
        "completed",
        "清理完成，任务已归档为 completed。",
        updated,
        previous_state,
        cleanup_result=cleanup_result,
        discard_confirmed=bool(discard_changes),
    )


def abandon_task(
    profile: ProjectProfile,
    item_id: str,
    reason: str = "",
    *,
    discard_changes: bool = False,
) -> Dict[str, Any]:
    task = _raw_task(profile, item_id)
    previous_state = str(task.get("status") or "")
    if previous_state in {"completed", "abandoned"}:
        raise ValueError("终态任务不允许再次放弃。")

    clean_reason = reason.strip() or str(task.get("abandoned_reason") or "").strip() or "用户放弃该任务。"
    managed_resources = profile.worktree_managed or task.get("schema_version") == 2
    if not managed_resources:
        updated = update_item(
            profile,
            item_id,
            status="abandoned",
            abandoned_reason=clean_reason,
            abandoned_at=utc_now(),
        )
        return _action_result("completed", "任务已标记为 abandoned。", updated, previous_state)

    if task.get("cleanup_pending") != "abandon":
        update_item(
            profile,
            item_id,
            cleanup_pending="abandon",
            abandon_requested_at=utc_now(),
            abandoned_reason=clean_reason,
            agent_status=None,
            agent_run_id=None,
        )
        task = _raw_task(profile, item_id)

    try:
        cleanup_targets = _cleanup_target_order(profile, task)
    except TargetPlanError as exc:
        return _abandon_cleanup_failed(
            profile,
            item_id,
            previous_state,
            exc.summary,
            {"code": exc.code, **exc.detail},
        )

    cleanup_results: List[Dict[str, Any]] = []
    for target in cleanup_targets:
        target_result = _cleanup_one_target(
            profile,
            target,
            item_id,
            discard_changes=discard_changes,
            managed_resources=managed_resources,
        )
        cleanup_results.append(target_result)
        if target_result["status"] == "failed":
            summary = f"{target.id} 放弃清理失败：{target_result['summary']}"
            return _abandon_cleanup_failed(
                profile,
                item_id,
                previous_state,
                summary,
                {"failed_target": target.id, "targets": cleanup_results},
                target_id=target.id,
                required_action=str(target_result.get("required_action") or ""),
            )

    cleanup_result = _combined_cleanup_result(cleanup_results, cleanup_targets)
    updated_targets = _targets_with_action_results(task, cleanup_results, "cleanup")
    fields: Dict[str, Any] = {
        "status": "abandoned",
        "cleanup_pending": None,
        "abandoned_reason": clean_reason,
        "abandoned_at": utc_now(),
        "cleanup_result": cleanup_result,
        "discard_confirmed": bool(discard_changes),
    }
    if updated_targets is not None:
        fields["targets"] = updated_targets
    _clear_action_blockers(fields)
    updated = update_item(
        profile,
        item_id,
        **fields,
    )
    return _action_result(
        "completed",
        "本地受管资源已清理，任务已归档为 abandoned。",
        updated,
        previous_state,
        cleanup_result=cleanup_result,
        discard_confirmed=bool(discard_changes),
    )


def _merge_one_target(
    target: TaskTarget,
    task: Dict[str, Any],
    item_id: str,
    *,
    allow_loopforge_runtime: bool = False,
) -> Dict[str, Any]:
    base = _target_result_base(target)
    if target.merge_method != "squash":
        return {**base, "status": "failed", "summary": f"当前只支持本地 squash merge，不支持 {target.merge_method}。"}
    if not target.branch:
        return {**base, "status": "failed", "summary": "任务缺少 feature branch，无法执行本地合入。"}
    ref_error = _validate_ref_pair(target.target_branch, target.branch)
    if ref_error:
        return {**base, "status": "failed", "summary": ref_error}

    checks = _merge_prechecks(
        target.repo_path,
        target.target_branch,
        target.branch,
        allow_loopforge_runtime=allow_loopforge_runtime,
    )
    if checks["status"] != "passed":
        return {**base, "status": "failed", "summary": checks["summary"], "checks": checks}

    branch_commit = _git(target.repo_path, "rev-parse", "--short", target.branch)
    target_commit = _git(target.repo_path, "rev-parse", "--short", target.target_branch)
    previous_merge = _previous_target_merge_result(task, target.id)
    previous_merge_commit = str(previous_merge.get("merge_commit") or "").strip()
    if previous_merge_commit:
        previous_ancestor = _git(
            target.repo_path,
            "merge-base",
            "--is-ancestor",
            previous_merge_commit,
            target.target_branch,
        )
        if previous_ancestor.returncode == 0:
            return {
                **base,
                "status": "already_merged",
                "summary": "Target 先前的 squash commit 仍包含在 target branch 中，已跳过重复合入。",
                "branch_commit": branch_commit.stdout.strip(),
                "merge_commit": previous_merge_commit,
                "checks": checks,
            }
    ancestor = _git(target.repo_path, "merge-base", "--is-ancestor", target.branch, target.target_branch)
    if ancestor.returncode == 0:
        return {
            **base,
            "status": "already_merged",
            "summary": "feature branch 已包含在 target branch 中，未创建新 squash commit。",
            "branch_commit": branch_commit.stdout.strip(),
            "merge_commit": target_commit.stdout.strip(),
            "checks": checks,
        }

    dry_run = _git(target.repo_path, "merge-tree", "--write-tree", target.target_branch, target.branch)
    if dry_run.returncode != 0:
        conflict_files = _merge_tree_conflict_files(dry_run)
        conflict_summary = f" 冲突文件：{'、'.join(conflict_files)}。" if conflict_files else ""
        return {
            **base,
            "status": "failed",
            "summary": f"合入前检查失败：feature branch 无法 clean merge 到 target branch。{conflict_summary}",
            "checks": {
                **checks,
                "merge_tree": _proc_detail(dry_run),
                "conflict_files": conflict_files,
            },
        }

    merge = _git(target.repo_path, "merge", "--squash", target.branch)
    if merge.returncode != 0:
        rollback = _rollback_squash(target.repo_path)
        return {
            **base,
            "status": "failed",
            "summary": "本地 squash merge 失败。",
            "checks": {**checks, "merge": _proc_detail(merge), "rollback": _proc_detail(rollback)},
        }

    staged = _git(target.repo_path, "diff", "--cached", "--quiet", "--")
    if staged.returncode == 0:
        return {
            **base,
            "status": "already_merged",
            "summary": "feature branch 的 squash 内容已存在于 target branch，未创建空提交。",
            "branch_commit": branch_commit.stdout.strip(),
            "merge_commit": target_commit.stdout.strip(),
            "checks": checks,
        }
    if staged.returncode != 1:
        rollback = _rollback_squash(target.repo_path)
        return {
            **base,
            "status": "failed",
            "summary": "读取 squash 暂存结果失败。",
            "checks": {**checks, "staged": _proc_detail(staged), "rollback": _proc_detail(rollback)},
        }

    title = str(task.get("title") or item_id).strip()
    message = f"loopforge: merge {title}\n\nTask: {item_id}\nTarget: {target.id}"
    commit = _git(target.repo_path, "commit", "-m", message)
    if commit.returncode != 0:
        rollback = _rollback_squash(target.repo_path)
        return {
            **base,
            "status": "failed",
            "summary": "本地 squash merge commit 失败。",
            "checks": {**checks, "commit": _proc_detail(commit), "rollback": _proc_detail(rollback)},
        }

    commit_sha = _git(target.repo_path, "rev-parse", "--short", "HEAD").stdout.strip()
    return {
        **base,
        "status": "merged",
        "summary": "本地 squash merge 已完成。",
        "branch_commit": branch_commit.stdout.strip(),
        "merge_commit": commit_sha,
        "checks": checks,
    }


def _cleanup_one_target(
    profile: ProjectProfile,
    target: TaskTarget,
    item_id: str,
    *,
    discard_changes: bool = False,
    managed_resources: Optional[bool] = None,
) -> Dict[str, Any]:
    cleanup_result: Dict[str, Any] = {
        **_target_result_base(target),
        "status": "completed",
        "summary": "本地 branch/worktree 清理完成。",
        "worktree_removed": False,
        "branch_deleted": False,
    }
    ref_error = _validate_ref(target.branch) if target.branch else ""
    if ref_error:
        return {**cleanup_result, "status": "failed", "summary": ref_error}

    use_managed_resources = profile.worktree_managed if managed_resources is None else managed_resources
    if use_managed_resources:
        managed = cleanup_managed_worktree(
            profile,
            task_id=item_id,
            target_id=target.id,
            repo_path=target.repo_path,
            branch=target.branch,
            worktree_path=target.worktree_path,
            discard_changes=discard_changes,
        )
        return {**cleanup_result, **managed}

    worktree_path = cleanup_result["worktree_path"]
    if worktree_path and Path(worktree_path).exists():
        status = _git(Path(worktree_path), "status", "--porcelain", "--untracked-files=all")
        dirty = status.returncode == 0 and bool(status.stdout.strip())
        if dirty and not discard_changes:
            return {
                **cleanup_result,
                "status": "failed",
                "code": "dirty_worktree_cleanup_requires_discard",
                "summary": "worktree 存在未提交变化，必须显式确认 discard_changes 才能清理。",
                "required_action": "确认允许丢弃未提交变化后重试清理。",
            }
        remove_args = ["worktree", "remove"]
        if dirty:
            remove_args.append("--force")
        remove_args.append(worktree_path)
        remove = _git(target.repo_path, *remove_args)
        if remove.returncode != 0:
            return {**cleanup_result, "status": "failed", "summary": "worktree 清理失败。", "worktree_error": _proc_detail(remove)}
        cleanup_result["worktree_removed"] = True

    if target.branch and _local_branch_exists(target.repo_path, target.branch):
        current_branch = _current_branch(target.repo_path)
        if current_branch == target.branch:
            return {**cleanup_result, "status": "failed", "summary": "不能删除当前正在使用的 feature branch。"}
        delete = _git(target.repo_path, "branch", "-D", target.branch)
        if delete.returncode != 0:
            return {**cleanup_result, "status": "failed", "summary": "feature branch 删除失败。", "branch_error": _proc_detail(delete)}
        cleanup_result["branch_deleted"] = True
    return cleanup_result


def _target_result_base(target: TaskTarget) -> Dict[str, Any]:
    return {
        "target": target.id,
        "project": target.project,
        "repo": target.repo,
        "repo_path": str(target.repo_path),
        "target_branch": target.target_branch,
        "branch": target.branch,
        "worktree_path": target.worktree_path,
        "merge_method": target.merge_method,
    }


def _cleanup_target_order(profile: ProjectProfile, task: Dict[str, Any]) -> List[TaskTarget]:
    """返回业务 Target 与父任务账本 worktree 的完整本地清理顺序。"""

    plan = task_target_plan(profile, task, require_repo_paths=True)
    ordered = list(plan.ordered)
    git = task.get("git") if isinstance(task.get("git"), dict) else {}
    ledger_fields = _published_ledger_cleanup_fields(profile, str(task.get("id") or "")) if plan.explicit else {}
    ledger_branch = str(
        ledger_fields.get("branch")
        or task.get("branch")
        or task.get("feature_branch")
        or git.get("feature_branch")
        or ""
    ).strip()
    if plan.explicit and ledger_branch:
        ledger_task = dict(task)
        ledger_task["targets"] = []
        ledger_task.update(ledger_fields)
        ledger_task["branch"] = ledger_branch
        ledger_task["feature_branch"] = ledger_branch
        ledger_plan = task_target_plan(profile, ledger_task, require_repo_paths=True)
        ordered.extend(ledger_plan.ordered)
    return ordered


def _published_ledger_cleanup_fields(profile: ProjectProfile, task_id: str) -> Dict[str, str]:
    """优先使用已发布 reservation 中不可变的父账本资源绑定。"""

    if not task_id:
        return {}
    try:
        reservation = load_published_reservation_for_task(profile, task_id)
    except TaskPublisherError:
        return {}
    ledger = next((binding for binding in reservation.bindings if binding.get("kind") == "ledger"), None)
    if not isinstance(ledger, dict):
        return {}
    return {
        "branch": str(ledger.get("feature_branch") or "").strip(),
        "worktree_path": str(ledger.get("worktree_path") or "").strip(),
        "target_branch": str(ledger.get("target_branch") or "main").strip() or "main",
    }


def _combined_merge_result(results: Sequence[Dict[str, Any]], ordered: Sequence[TaskTarget]) -> Dict[str, Any]:
    if len(results) == 1:
        result = dict(results[0])
        result["targets"] = list(results)
        result["ordered_target_ids"] = [target.id for target in ordered]
        return result
    status = "merged" if any(result.get("status") == "merged" for result in results) else "already_merged"
    return {
        "status": status,
        "summary": "多 target 本地 squash merge 已完成。" if status == "merged" else "所有 target 此前均已合入。",
        "merge_method": "squash",
        "targets": list(results),
        "ordered_target_ids": [target.id for target in ordered],
    }


def _combined_cleanup_result(results: Sequence[Dict[str, Any]], ordered: Sequence[TaskTarget]) -> Dict[str, Any]:
    if len(results) == 1:
        result = dict(results[0])
        result["targets"] = list(results)
        result["ordered_target_ids"] = [target.id for target in ordered]
        return result
    return {
        "status": "completed",
        "summary": "多 target 本地 branch/worktree 清理完成。",
        "targets": list(results),
        "ordered_target_ids": [target.id for target in ordered],
    }


def _targets_with_action_results(task: Dict[str, Any], results: Sequence[Dict[str, Any]], action: str) -> Optional[List[Any]]:
    raw_targets = task.get("targets")
    if not isinstance(raw_targets, list):
        return None
    by_id = {str(result.get("target") or ""): result for result in results}
    updated: List[Any] = []
    for index, raw in enumerate(raw_targets):
        target_id = _raw_target_id(raw, index)
        result = by_id.get(target_id)
        if isinstance(raw, dict):
            item = dict(raw)
        else:
            item = {"id": target_id, "project": target_id}
        if result:
            item["status"] = "merged" if action == "merge" else "cleaned"
            if action == "merge":
                item["merge_result"] = result
                for key in ["branch", "target_branch", "merge_method", "repo_path", "worktree_path", "merge_commit", "branch_commit"]:
                    if result.get(key):
                        item[key] = result[key]
            else:
                item["cleanup_result"] = result
                for key in ["branch", "repo_path", "worktree_path"]:
                    if result.get(key):
                        item[key] = result[key]
        updated.append(item)
    return updated


def _raw_target_id(raw: Any, index: int) -> str:
    if isinstance(raw, dict):
        for key in ["id", "project", "project_id", "target", "key"]:
            value = str(raw.get(key) or "").strip()
            if value:
                return value
    text = str(raw or "").strip()
    return text or f"target-{index + 1}"


def _previous_target_merge_result(task: Dict[str, Any], target_id: str) -> Dict[str, Any]:
    raw_targets = task.get("targets")
    if not isinstance(raw_targets, list):
        return {}
    for index, raw in enumerate(raw_targets):
        if _raw_target_id(raw, index) != target_id or not isinstance(raw, dict):
            continue
        result = raw.get("merge_result")
        if isinstance(result, dict) and result.get("status") in {"merged", "already_merged"}:
            return result
    return {}


def _raw_task(profile: ProjectProfile, item_id: str) -> Dict[str, Any]:
    clean_id = item_id.strip()
    if not clean_id:
        raise ValueError("任务 id 不能为空。")
    payload = load_dev_tasks(profile)
    for item in payload.get("items", []):
        if isinstance(item, dict) and item.get("id") == clean_id:
            return item
    raise KeyError(f"未知任务 item：{clean_id}")


def _action_result(status: str, summary: str, task: Dict[str, Any], previous_state: str, **extra: Any) -> Dict[str, Any]:
    payload = {
        "status": status,
        "summary": summary,
        "task": task,
        "previous_state": previous_state,
        "next_state": task.get("status") or task.get("state"),
    }
    payload.update(extra)
    return payload


def _task_branch(task: Dict[str, Any]) -> str:
    for key in ["branch", "feature_branch"]:
        value = str(task.get(key) or "").strip()
        if value:
            return value
    targets = task.get("targets")
    if isinstance(targets, list) and len(targets) == 1 and isinstance(targets[0], dict):
        return str(targets[0].get("branch") or "").strip()
    return ""


def _task_worktree_path(task: Dict[str, Any]) -> str:
    for key in ["worktree_path", "worktree"]:
        value = str(task.get(key) or "").strip()
        if value:
            return value
    targets = task.get("targets")
    if isinstance(targets, list) and len(targets) == 1 and isinstance(targets[0], dict):
        return str(targets[0].get("worktree_path") or targets[0].get("worktree") or "").strip()
    return ""


def _validate_ref_pair(target: str, branch: str) -> str:
    target_error = _validate_ref(target)
    if target_error:
        return f"target_branch 非法：{target_error}"
    branch_error = _validate_ref(branch)
    if branch_error:
        return f"feature branch 非法：{branch_error}"
    if target == branch:
        return "target_branch 不能和 feature branch 相同。"
    return ""


def _validate_ref(value: str) -> str:
    if not value:
        return "分支名不能为空。"
    if value.startswith("-") or ".." in value or "@{" in value:
        return f"不安全的分支名：{value}"
    if value.endswith("/") or value.endswith("."):
        return f"不安全的分支名：{value}"
    checked = subprocess.run(
        ["git", "check-ref-format", "--branch", value],
        check=False,
        capture_output=True,
        text=True,
    )
    if checked.returncode != 0:
        return f"不安全的分支名：{value}"
    return ""


def _merge_prechecks(
    repo: Path,
    target: str,
    branch: str,
    *,
    allow_loopforge_runtime: bool = False,
) -> Dict[str, Any]:
    inside = _git(repo, "rev-parse", "--is-inside-work-tree")
    if inside.returncode != 0:
        return {"status": "failed", "summary": "项目不是 git 工作区。", "detail": _proc_detail(inside)}
    current = _current_branch(repo)
    if current != target:
        return {
            "status": "failed",
            "summary": f"当前分支是 {current or 'detached HEAD'}，本地合入要求先切到 target_branch={target}。",
            "current_branch": current,
            "target_branch": target,
        }
    if not _local_branch_exists(repo, target):
        return {"status": "failed", "summary": f"target_branch 不存在：{target}"}
    if not _local_branch_exists(repo, branch):
        return {"status": "failed", "summary": f"feature branch 不存在：{branch}"}
    status = _git(repo, "status", "--porcelain")
    if status.returncode != 0:
        return {"status": "failed", "summary": "读取 git 工作区状态失败。", "detail": _proc_detail(status)}
    porcelain = status.stdout.rstrip()
    if porcelain and not (allow_loopforge_runtime and _only_loopforge_runtime_dirty(repo, porcelain)):
        return {"status": "failed", "summary": "target_branch 工作区不干净，已阻止合入。", "porcelain": porcelain}
    return {"status": "passed", "summary": "合入前检查通过。", "current_branch": current, "target_branch": target, "branch": branch}


def _only_loopforge_runtime_dirty(repo: Path, porcelain: str) -> bool:
    legacy_mode = (repo / "data" / "dev-task.json").exists() and not (repo / "data" / "tasks").is_dir()
    for line in porcelain.splitlines():
        path = line[3:].strip() if len(line) >= 4 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if legacy_mode and (
            path == "data/dev-task.json"
            or path.startswith("data/history/")
            or path.startswith("data/dev-task-history-")
        ):
            continue
        if line.startswith("?? ") and (path == ".loopforge/" or path.startswith(".loopforge/")):
            tracked = _git(repo, "ls-files", ".loopforge")
            if not tracked.stdout.strip():
                continue
        return False
    return True


def _merge_failed(
    profile: ProjectProfile,
    item_id: str,
    previous_state: str,
    summary: str,
    detail: Optional[Dict[str, Any]] = None,
    target_id: str = "",
    partial_results: Sequence[Dict[str, Any]] = (),
) -> Dict[str, Any]:
    merge_result = {
        "status": "failed",
        "summary": summary,
        "detail": detail or {},
    }
    if target_id:
        merge_result["failed_target"] = target_id
    blocker = _blocker("merge_failed", summary, "处理合入前检查、冲突或本地工作区状态后，再点击合入。")
    if target_id:
        blocker["target"] = target_id
    fields: Dict[str, Any] = {
        "status": "accepted",
        "merge_result": merge_result,
        "blockers": [blocker],
        "blocker_reason": summary,
        "required_action": blocker["resolution"],
    }
    completed_results = [
        result for result in partial_results if result.get("status") in {"merged", "already_merged"}
    ]
    if completed_results:
        updated_targets = _targets_with_action_results(_raw_task(profile, item_id), completed_results, "merge")
        if updated_targets is not None:
            fields["targets"] = updated_targets
    task = update_item(
        profile,
        item_id,
        **fields,
    )
    return _action_result("failed", summary, task, previous_state, merge_result=merge_result, required_action=blocker["resolution"])


def _cleanup_failed(
    profile: ProjectProfile,
    item_id: str,
    previous_state: str,
    summary: str,
    detail: Optional[Dict[str, Any]] = None,
    target_id: str = "",
    required_action: str = "",
) -> Dict[str, Any]:
    cleanup_result = {
        "status": "failed",
        "summary": summary,
        "detail": detail or {},
    }
    if target_id:
        cleanup_result["failed_target"] = target_id
    resolution = required_action or "处理本地 worktree 或 feature branch 后，再点击清理。"
    blocker = _blocker("cleanup_failed", summary, resolution)
    if target_id:
        blocker["target"] = target_id
    task = update_item(
        profile,
        item_id,
        status="merged",
        cleanup_result=cleanup_result,
        blockers=[blocker],
        blocker_reason=summary,
        required_action=blocker["resolution"],
    )
    return _action_result("failed", summary, task, previous_state, cleanup_result=cleanup_result, required_action=blocker["resolution"])


def _abandon_cleanup_failed(
    profile: ProjectProfile,
    item_id: str,
    previous_state: str,
    summary: str,
    detail: Optional[Dict[str, Any]] = None,
    target_id: str = "",
    required_action: str = "",
) -> Dict[str, Any]:
    cleanup_result = {
        "status": "failed",
        "summary": summary,
        "detail": detail or {},
    }
    if target_id:
        cleanup_result["failed_target"] = target_id
    resolution = required_action or "处理本地受管 worktree 或 feature branch 后，重试放弃任务。"
    blocker = _blocker("abandon_cleanup_failed", summary, resolution)
    if target_id:
        blocker["target"] = target_id
    task = update_item(
        profile,
        item_id,
        cleanup_pending="abandon",
        cleanup_result=cleanup_result,
        blockers=[blocker],
        blocker_reason=summary,
        required_action=resolution,
    )
    return _action_result(
        "failed",
        summary,
        task,
        previous_state,
        cleanup_result=cleanup_result,
        required_action=resolution,
    )


def _blocker(code: str, message: str, resolution: str) -> Dict[str, Any]:
    return {
        "id": code,
        "code": code,
        "type": code,
        "message": message,
        "summary": message,
        "resolution": resolution,
    }


def _clear_action_blockers(fields: Dict[str, Any]) -> None:
    for key in ["blocker_reason", "last_error", "required_action", "active_blocker", "blockers", "diagnostic"]:
        fields[key] = None if key != "blockers" else []


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _current_branch(repo: Path) -> str:
    proc = _git(repo, "branch", "--show-current")
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _local_branch_exists(repo: Path, branch: str) -> bool:
    proc = _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}")
    return proc.returncode == 0


def _rollback_squash(repo: Path) -> subprocess.CompletedProcess[str]:
    """撤销 squash 写入，同时保留合入前已有的非暂存运行状态。"""

    return _git(repo, "reset", "--merge", "HEAD")


def _merge_tree_conflict_files(proc: subprocess.CompletedProcess[str]) -> List[str]:
    """从 merge-tree 的冲突 stage 行提取稳定、去重的文件路径。"""

    paths: List[str] = []
    output = "\n".join(part for part in [proc.stdout, proc.stderr] if part)
    for line in output.splitlines():
        match = re.match(r"^\d{6} [0-9a-f]+ [123]\t(.+)$", line)
        if match and match.group(1) not in paths:
            paths.append(match.group(1))
    return paths


def _proc_detail(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stderr or proc.stdout or "").strip()
