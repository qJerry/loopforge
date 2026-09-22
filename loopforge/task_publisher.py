"""任务预留、受管 worktree 与首次发布编排。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ProjectProfile
from .domain import utc_now
from .managed_branches import (
    is_legacy_managed_feature_branch,
    managed_feature_branch,
    readable_feature_branch,
    readable_task_name,
)
from .task_contract import task_uses_document_contract
from .task_git import publish_new_task_from_isolated_main, push_managed_feature_revision
from .task_store import canonical_task_payload
from .targets import TargetPlanError, task_target_plan
from .worktrees import (
    WorktreePreparationError,
    cleanup_managed_worktree,
    readable_worktree_path,
    reserve_task_worktrees,
)


_RESERVATION_ID = re.compile(r"^reservation-[a-z0-9]{10,32}$")
_FORBIDDEN_REQUEST_FIELDS = {
    "id",
    "task_id",
    "version",
    "schema_version",
    "branch",
    "feature_branch",
    "worktree",
    "worktree_path",
    "git",
    "base_revision",
    "branch_revision",
    "_managed_name",
}
_TARGET_FORBIDDEN_REQUEST_FIELDS = _FORBIDDEN_REQUEST_FIELDS - {"id"}


class TaskPublisherError(RuntimeError):
    def __init__(self, code: str, summary: str, detail: str = "") -> None:
        self.code = code
        self.summary = summary
        self.detail = detail
        message = f"{summary}：{detail}" if detail else summary
        super().__init__(message)


@dataclass(frozen=True)
class Reservation:
    schema_version: int
    reservation_id: str
    project_id: str
    task_id: str
    status: str
    created_at: str
    updated_at: str
    draft: Dict[str, Any]
    bindings: list[Dict[str, Any]]
    cleanup: Dict[str, Any] = field(default_factory=dict)


def reserve_task(profile: ProjectProfile, request: Dict[str, Any]) -> Reservation:
    """分配稳定任务 ID，并立即创建受管 feature branch/worktree。"""

    token = uuid.uuid4().hex[:10]
    return _reserve_task(profile, request, task_id=f"task-{token}", reservation_id=f"reservation-{token}")


def reserve_migrated_task(profile: ProjectProfile, task_id: str, request: Dict[str, Any]) -> Reservation:
    """为 legacy 非终态任务保留原 id，并创建确定性的迁移 reservation。"""

    if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$", task_id):
        raise TaskPublisherError("task_id_invalid", "迁移任务 id 非法", task_id)
    token = hashlib.sha256(f"{profile.project_id}:{task_id}".encode("utf-8")).hexdigest()[:10]
    reservation_id = f"reservation-{token}"
    marker = reservation_marker_for(profile, reservation_id)
    if marker.exists():
        restored = load_reservation(profile, reservation_id, recover_worktrees=True)
        if restored.task_id != task_id:
            raise TaskPublisherError("reservation_collision", "迁移 reservation 与任务 id 不一致", task_id)
        return restored
    return _reserve_task(profile, request, task_id=task_id, reservation_id=reservation_id)


def _reserve_task(
    profile: ProjectProfile,
    request: Dict[str, Any],
    *,
    task_id: str,
    reservation_id: str,
) -> Reservation:
    draft = _validated_draft(request)
    _require_clean_main(profile.root_dir)
    created_at = utc_now()

    try:
        base_name = readable_task_name(str(draft.get("title") or "task"), _local_date_prefix())
        draft["_managed_name"] = _available_managed_name(profile, draft, base_name)
        bindings = _prepare_bindings(profile, task_id, reservation_id, draft)
    except WorktreePreparationError as exc:
        raise TaskPublisherError(exc.code, exc.summary, json.dumps(exc.detail, ensure_ascii=False)) from exc
    reservation = Reservation(
        schema_version=1,
        reservation_id=reservation_id,
        project_id=profile.project_id,
        task_id=task_id,
        status="reserved",
        created_at=created_at,
        updated_at=created_at,
        draft=draft,
        bindings=bindings,
    )
    _write_reservation(profile, reservation)
    return reservation


def _local_date_prefix() -> str:
    return datetime.now().astimezone().strftime("%m%d")


def _available_managed_name(profile: ProjectProfile, draft: Dict[str, Any], base_name: str) -> str:
    locations: list[tuple[Path, str]] = [(profile.root_dir.resolve(), "")]
    raw_targets = draft.get("targets")
    if isinstance(raw_targets, list) and raw_targets:
        try:
            plan = task_target_plan(
                profile,
                {"id": "task", "targets": raw_targets},
                require_repo_paths=True,
            )
        except TargetPlanError as exc:
            raise WorktreePreparationError(exc.code, exc.summary, exc.detail) from exc
        locations.extend((target.repo_path.resolve(), target.id) for target in plan.ordered)

    index = 1
    while True:
        candidate = base_name if index == 1 else f"{base_name}-{index}"
        occupied = any(
            _readable_location_exists(repo, candidate, target_id)
            for repo, target_id in locations
        )
        if not occupied:
            return candidate
        index += 1


def _readable_location_exists(repo: Path, managed_name: str, target_id: str) -> bool:
    branch = readable_feature_branch(managed_name, target_id)
    branch_exists = any(
        _git(repo, "show-ref", "--verify", "--quiet", ref).returncode == 0
        for ref in (f"refs/heads/{branch}", f"refs/remotes/origin/{branch}")
    )
    return branch_exists or readable_worktree_path(repo, managed_name, target_id).exists()


def load_reservation(
    profile: ProjectProfile,
    reservation_id: str,
    *,
    recover_worktrees: bool = False,
) -> Reservation:
    """读取预留；需要时依据 ownership marker 重建丢失 worktree。"""

    path = reservation_marker_for(profile, reservation_id)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TaskPublisherError("reservation_not_found", "任务预留不存在", reservation_id) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise TaskPublisherError("reservation_invalid", "任务预留无法读取", f"{path}: {exc}") from exc
    reservation = _reservation_from_payload(payload)
    if reservation.project_id != profile.project_id:
        raise TaskPublisherError("reservation_project_mismatch", "任务预留不属于当前项目", reservation.project_id)
    if recover_worktrees and reservation.status == "reserved":
        try:
            bindings = _prepare_bindings(
                profile,
                reservation.task_id,
                reservation.reservation_id,
                reservation.draft,
            )
        except WorktreePreparationError as exc:
            raise TaskPublisherError(exc.code, exc.summary, json.dumps(exc.detail, ensure_ascii=False)) from exc
        reservation = Reservation(**{**asdict(reservation), "bindings": bindings, "updated_at": utc_now()})
        _write_reservation(profile, reservation)
    return reservation


def load_published_reservation_for_task(profile: ProjectProfile, task_id: str) -> Reservation:
    """按任务 ID 定位唯一的已发布 reservation。"""

    marker_dir = reservation_marker_for(profile, "reservation-0000000000").parent
    matches: list[Reservation] = []
    if marker_dir.is_dir():
        for path in sorted(marker_dir.glob("reservation-*.json")):
            try:
                reservation = load_reservation(profile, path.stem)
            except TaskPublisherError:
                continue
            if reservation.task_id == task_id and reservation.status == "published":
                matches.append(reservation)
    if not matches:
        raise TaskPublisherError("task_reservation_not_found", "任务缺少可用于文档校验的已发布 reservation", task_id)
    if len(matches) != 1:
        raise TaskPublisherError("task_reservation_ambiguous", "任务存在多个已发布 reservation", task_id)
    return matches[0]


def publish_new_task(
    profile: ProjectProfile,
    reservation_id: str,
    fields: Dict[str, Any],
) -> Dict[str, Any]:
    """先发布 feature refs，再创建并发布 main 上的任务 JSON。"""

    task, reservation, bindings = materialize_reserved_task(profile, reservation_id, fields)
    published = publish_new_task_from_isolated_main(
        profile,
        task,
        f"loopforge: 创建任务 {reservation.task_id}",
    )

    status = "published" if published.status == "published" else "sync_blocked"
    updated = Reservation(
        **{
            **asdict(reservation),
            "status": status,
            "updated_at": utc_now(),
            "bindings": bindings,
        }
    )
    _write_reservation(profile, updated)
    return {
        "status": status,
        "task": task,
        "reservation": asdict(updated),
        "publish": asdict(published),
    }


def cancel_reservation(
    profile: ProjectProfile,
    reservation_id: str,
    *,
    discard_changes: bool = False,
) -> Dict[str, Any]:
    """显式取消未发布预留，并保留可幂等读取的 cancelled tombstone。"""

    reservation = load_reservation(profile, reservation_id)
    if reservation.status == "cancelled":
        return {
            "status": "completed",
            "summary": "任务预留此前已取消。",
            "reservation": asdict(reservation),
            "cleanup_result": reservation.cleanup,
            "discard_confirmed": bool(discard_changes),
            "already_cancelled": True,
        }
    if reservation.status == "published":
        raise TaskPublisherError("reservation_published", "已发布任务不能取消预留，请改用 abandon", reservation.task_id)
    if reservation.status == "sync_blocked" and (profile.root_dir / "data" / "tasks" / f"{reservation.task_id}.json").exists():
        raise TaskPublisherError("reservation_has_task", "发布失败预留已生成任务文件，不能直接取消", reservation.task_id)
    if reservation.status not in {"reserved", "sync_blocked", "cancel_blocked"}:
        raise TaskPublisherError("reservation_not_cancellable", "任务预留当前不可取消", reservation.status)

    cleanup_results: list[Dict[str, Any]] = []
    for binding in reservation.bindings:
        target_result = cleanup_managed_worktree(
            profile,
            task_id=reservation.task_id,
            target_id=str(binding.get("target_id") or ""),
            repo_path=Path(str(binding.get("repo_path") or "")),
            branch=str(binding.get("feature_branch") or ""),
            worktree_path=str(binding.get("worktree_path") or ""),
            discard_changes=discard_changes,
        )
        target_result["kind"] = str(binding.get("kind") or "")
        cleanup_results.append(target_result)
        if target_result["status"] == "failed":
            cleanup = {
                "status": "failed",
                "failed_target": target_result.get("target"),
                "targets": cleanup_results,
                "discard_confirmed": bool(discard_changes),
                "updated_at": utc_now(),
            }
            blocked = Reservation(
                **{
                    **asdict(reservation),
                    "status": "cancel_blocked",
                    "updated_at": utc_now(),
                    "cleanup": cleanup,
                }
            )
            _write_reservation(profile, blocked)
            return {
                "status": "failed",
                "summary": f"预留取消清理失败：{target_result['summary']}",
                "reservation": asdict(blocked),
                "cleanup_result": cleanup,
                "required_action": target_result.get("required_action") or "处理失败资源后重试取消。",
                "discard_confirmed": bool(discard_changes),
            }

    cleanup = {
        "status": "completed",
        "targets": cleanup_results,
        "discard_confirmed": bool(discard_changes),
        "completed_at": utc_now(),
        "remote_branches_preserved": True,
    }
    cancelled = Reservation(
        **{
            **asdict(reservation),
            "status": "cancelled",
            "updated_at": utc_now(),
            "cleanup": cleanup,
        }
    )
    _write_reservation(profile, cancelled)
    return {
        "status": "completed",
        "summary": "任务预留已取消，本地受管资源已清理。",
        "reservation": asdict(cancelled),
        "cleanup_result": cleanup,
        "discard_confirmed": bool(discard_changes),
    }


def migrate_reservation_feature_branches(profile: ProjectProfile, reservation_id: str) -> Reservation:
    """把未发布 reservation 的历史 `loopforge/` 分支迁移为 `feature/`。"""

    reservation = load_reservation(profile, reservation_id)
    if reservation.status != "reserved":
        raise TaskPublisherError(
            "reservation_not_migratable",
            "只能迁移尚未发布的任务预留",
            reservation.status,
        )

    plans: list[Dict[str, Any]] = []
    for index, raw_binding in enumerate(reservation.bindings):
        binding = dict(raw_binding)
        old_branch = str(binding.get("feature_branch") or "").strip()
        target_id = str(binding.get("target_id") or "").strip()
        target_suffix = target_id if str(binding.get("kind") or "") == "target" else ""
        new_branch = managed_feature_branch(profile.project_id, reservation.task_id, target_suffix)
        if old_branch == new_branch:
            continue
        if not is_legacy_managed_feature_branch(old_branch):
            raise TaskPublisherError(
                "reservation_branch_not_legacy",
                "reservation 分支既不是当前命名，也不是可迁移的历史命名",
                old_branch,
            )

        repo = Path(str(binding.get("repo_path") or "")).resolve()
        worktree = Path(str(binding.get("worktree_path") or "")).resolve()
        marker_path = Path(str(binding.get("ownership_marker") or "")).resolve()
        marker = _read_json_object(marker_path)
        expected_marker = {
            "project_id": profile.project_id,
            "task_id": reservation.task_id,
            "target_id": target_id,
            "branch": old_branch,
            "reservation_id": reservation.reservation_id,
            "worktree_path": str(worktree),
        }
        if any(str(marker.get(key) or "") != value for key, value in expected_marker.items()):
            raise TaskPublisherError(
                "reservation_ownership_mismatch",
                "ownership marker 与 reservation 绑定不一致",
                old_branch,
            )
        current = _git(worktree, "branch", "--show-current")
        if current.returncode != 0 or current.stdout.strip() != old_branch:
            raise TaskPublisherError(
                "reservation_worktree_branch_mismatch",
                "worktree 当前分支与 reservation 不一致",
                f"expected={old_branch} actual={current.stdout.strip()}",
            )
        status = _git(worktree, "status", "--porcelain", "--untracked-files=all")
        if status.returncode != 0 or status.stdout.strip():
            raise TaskPublisherError(
                "reservation_worktree_dirty",
                "迁移前 worktree 必须保持干净",
                status.stderr.strip() or status.stdout.strip(),
            )
        if _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{new_branch}").returncode == 0:
            raise TaskPublisherError(
                "reservation_target_branch_exists",
                "目标 feature 分支已存在，拒绝覆盖",
                new_branch,
            )
        revision = _git(worktree, "rev-parse", "HEAD")
        if revision.returncode != 0:
            raise TaskPublisherError("reservation_revision_missing", "无法读取待迁移分支 revision", old_branch)
        plans.append(
            {
                "index": index,
                "binding": binding,
                "repo": repo,
                "worktree": worktree,
                "marker_path": marker_path,
                "marker": marker,
                "old_branch": old_branch,
                "new_branch": new_branch,
                "revision": revision.stdout.strip(),
            }
        )

    if not plans:
        return reservation

    migrated: list[Dict[str, Any]] = []
    try:
        for plan in plans:
            renamed = _git(plan["worktree"], "branch", "-m", plan["new_branch"])
            if renamed.returncode != 0:
                raise TaskPublisherError(
                    "reservation_branch_migration_failed",
                    "feature 分支改名失败",
                    renamed.stderr.strip() or renamed.stdout.strip(),
                )
            migrated.append(plan)
            marker = dict(plan["marker"])
            marker["branch"] = plan["new_branch"]
            marker["updated_at"] = utc_now()
            plan["marker_path"].write_text(
                json.dumps(marker, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

        bindings = [dict(binding) for binding in reservation.bindings]
        for plan in plans:
            bindings[plan["index"]]["feature_branch"] = plan["new_branch"]
            bindings[plan["index"]]["branch_revision"] = plan["revision"]
        updated = Reservation(
            **{
                **asdict(reservation),
                "updated_at": utc_now(),
                "bindings": bindings,
            }
        )
        _write_reservation(profile, updated)
        return updated
    except Exception as exc:
        for plan in reversed(migrated):
            _git(plan["worktree"], "branch", "-m", plan["old_branch"])
            plan["marker_path"].write_text(
                json.dumps(plan["marker"], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
        if isinstance(exc, TaskPublisherError):
            raise
        raise TaskPublisherError("reservation_branch_migration_failed", "feature 分支迁移失败", str(exc)) from exc


def materialize_reserved_task(
    profile: ProjectProfile,
    reservation_id: str,
    fields: Dict[str, Any],
) -> tuple[Dict[str, Any], Reservation, list[Dict[str, Any]]]:
    """推送 feature refs 并生成待写入 main 的任务对象，但不修改任务文件。"""

    publish_fields = _validated_draft(fields)
    reservation = load_reservation(profile, reservation_id, recover_worktrees=True)
    if reservation.status != "reserved":
        raise TaskPublisherError("reservation_not_open", "任务预留已完成或不可发布", reservation.status)
    merged = dict(reservation.draft)
    merged.update(publish_fields)
    bindings = _refresh_and_push_bindings(profile, reservation.bindings)
    return _build_task(reservation, merged, bindings), reservation, bindings


def reservation_marker_for(profile: ProjectProfile, reservation_id: str) -> Path:
    """返回位于 Git common dir 的 reservation marker。"""

    _validate_reservation_id(reservation_id)
    common = _git(profile.root_dir, "rev-parse", "--git-common-dir")
    if common.returncode != 0:
        raise TaskPublisherError("project_not_git", "项目不是可用 Git 仓库", str(profile.root_dir))
    raw = Path(common.stdout.strip())
    common_dir = raw if raw.is_absolute() else (profile.root_dir / raw).resolve()
    return common_dir / "loopforge-reservations" / f"{reservation_id}.json"


def _prepare_bindings(
    profile: ProjectProfile,
    task_id: str,
    reservation_id: str,
    draft: Dict[str, Any],
) -> list[Dict[str, Any]]:
    raw_targets = draft.get("targets")
    explicit = isinstance(raw_targets, list) and bool(raw_targets)
    prepared_groups: list[tuple[str, Dict[str, Any]]] = []
    if explicit:
        ledger_item = {
            "id": task_id,
            "title": str(draft.get("title") or task_id),
            "_managed_name": str(draft.get("_managed_name") or ""),
            "target_branch": "main",
        }
        prepared_groups.append(("ledger", reserve_task_worktrees(profile, ledger_item, reservation_id)))
        target_item = {
            "id": task_id,
            "title": str(draft.get("title") or task_id),
            "_managed_name": str(draft.get("_managed_name") or ""),
            "targets": raw_targets,
        }
        prepared_groups.append(("target", reserve_task_worktrees(profile, target_item, reservation_id)))
    else:
        item = {
            "id": task_id,
            "title": str(draft.get("title") or task_id),
            "_managed_name": str(draft.get("_managed_name") or ""),
            "target_branch": "main",
        }
        prepared_groups.append(("ledger", reserve_task_worktrees(profile, item, reservation_id)))

    bindings: list[Dict[str, Any]] = []
    for kind, prepared in prepared_groups:
        for target in prepared["target_plan"]["targets"]:
            repo = Path(str(target["repo_path"])).resolve()
            branch = str(target["branch"])
            revision = _git_revision(repo, f"refs/heads/{branch}")
            bindings.append(
                {
                    "kind": kind,
                    "target_id": str(target["id"]),
                    "project": str(target["project"]),
                    "repo_path": str(repo),
                    "worktree_path": str(target["worktree_path"]),
                    "ownership_marker": str(target["ownership_marker"]),
                    "remote": "origin",
                    "target_branch": str(target.get("target_branch") or "main"),
                    "base_revision": revision,
                    "feature_branch": branch,
                    "branch_revision": revision,
                }
            )
    return bindings


def _refresh_and_push_bindings(
    profile: ProjectProfile,
    bindings: list[Dict[str, Any]],
) -> list[Dict[str, Any]]:
    refreshed: list[Dict[str, Any]] = []
    for raw in bindings:
        binding = dict(raw)
        repo = Path(str(binding["repo_path"])).resolve()
        branch = str(binding["feature_branch"])
        revision = _git_revision(repo, f"refs/heads/{branch}")
        push_managed_feature_revision(profile, binding, revision)
        binding["branch_revision"] = revision
        refreshed.append(binding)
    return refreshed


def _build_task(
    reservation: Reservation,
    fields: Dict[str, Any],
    bindings: list[Dict[str, Any]],
) -> Dict[str, Any]:
    ledger = next((binding for binding in bindings if binding["kind"] == "ledger"), None)
    if ledger is None:
        raise TaskPublisherError("ledger_binding_missing", "任务预留缺少主账本 binding")
    now = utc_now()
    raw_targets = fields.get("targets") if isinstance(fields.get("targets"), list) else []
    target_bindings = {binding["target_id"]: binding for binding in bindings if binding["kind"] == "target"}
    targets: list[Dict[str, Any]] = []
    for index, raw in enumerate(raw_targets):
        payload = dict(raw) if isinstance(raw, dict) else {"project": str(raw)}
        target_id = str(payload.get("id") or payload.get("project") or f"target-{index + 1}")
        binding = target_bindings.get(target_id)
        if binding is None:
            raise TaskPublisherError("target_binding_missing", "目标仓库缺少预留 binding", target_id)
        payload["id"] = target_id
        payload["git"] = _git_contract(binding)
        for forbidden in ("branch", "feature_branch", "worktree", "worktree_path", "repo_path"):
            payload.pop(forbidden, None)
        targets.append(payload)

    task = {
        "schema_version": 2,
        "id": reservation.task_id,
        "version": 1,
        "title": str(fields.get("title") or "").strip(),
        "description": str(fields.get("description") or "").strip(),
        "status": "open",
        "priority": str(fields.get("priority") or "P2").upper(),
        "source": str(fields.get("source") or "manual").strip(),
        "planning_level": str(fields.get("planning_level") or "complex").strip(),
        "created_at": reservation.created_at,
        "updated_at": now,
        "git": _git_contract(ledger),
        "docs": dict(fields.get("docs") or {}),
        "targets": targets,
    }
    if "acceptance" in fields:
        task["acceptance"] = fields["acceptance"]
        task["acceptance_refs"] = fields.get("acceptance_refs", [])
    for optional in ("supersedes",):
        if optional in fields:
            task[optional] = fields[optional]
    if task_uses_document_contract(task):
        task.pop("acceptance", None)
        task.pop("acceptance_refs", None)
    else:
        acceptance = task.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance:
            raise TaskPublisherError("task_acceptance_missing", "普通任务必须提供非空 acceptance")
        if not isinstance(task.get("acceptance_refs"), list):
            raise TaskPublisherError("task_acceptance_refs_invalid", "普通任务 acceptance_refs 必须是数组")
    return canonical_task_payload(task)


def _git_contract(binding: Dict[str, Any]) -> Dict[str, str]:
    return {
        "remote": str(binding["remote"]),
        "target_branch": str(binding["target_branch"]),
        "base_revision": str(binding["base_revision"]),
        "feature_branch": str(binding["feature_branch"]),
        "branch_revision": str(binding["branch_revision"]),
    }


def _validated_draft(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise TaskPublisherError("request_invalid", "任务预留请求必须是 JSON object")
    forbidden = sorted(field for field in _FORBIDDEN_REQUEST_FIELDS if value.get(field) not in (None, ""))
    if forbidden:
        raise TaskPublisherError("managed_field_forbidden", f"调用方不能指定受管字段 {', '.join(forbidden)}")
    targets = value.get("targets")
    if targets is not None and not isinstance(targets, list):
        raise TaskPublisherError("targets_invalid", "targets 必须是数组")
    if isinstance(targets, list):
        for index, raw in enumerate(targets):
            if not isinstance(raw, dict):
                continue
            nested = sorted(
                field for field in _TARGET_FORBIDDEN_REQUEST_FIELDS if raw.get(field) not in (None, "")
            )
            if nested:
                raise TaskPublisherError(
                    "managed_field_forbidden",
                    f"targets[{index}] 不能指定受管字段 {', '.join(nested)}",
                )
    return json.loads(json.dumps(value, ensure_ascii=False))


def _reservation_from_payload(payload: Any) -> Reservation:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise TaskPublisherError("reservation_invalid", "任务预留 Schema 非法")
    try:
        reservation = Reservation(
            schema_version=1,
            reservation_id=str(payload["reservation_id"]),
            project_id=str(payload["project_id"]),
            task_id=str(payload["task_id"]),
            status=str(payload["status"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            draft=dict(payload["draft"]),
            bindings=[dict(binding) for binding in payload["bindings"]],
            cleanup=dict(payload.get("cleanup") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TaskPublisherError("reservation_invalid", "任务预留字段不完整", str(exc)) from exc
    _validate_reservation_id(reservation.reservation_id)
    return reservation


def _write_reservation(profile: ProjectProfile, reservation: Reservation) -> None:
    path = reservation_marker_for(profile, reservation.reservation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(reservation), handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _validate_reservation_id(value: str) -> str:
    if not isinstance(value, str) or not _RESERVATION_ID.fullmatch(value):
        raise TaskPublisherError("reservation_id_invalid", "reservation_id 非法", str(value))
    return value


def _require_clean_main(repo: Path) -> None:
    branch = _git(repo, "branch", "--show-current")
    if branch.returncode != 0 or branch.stdout.strip() != "main":
        raise TaskPublisherError("wrong_target_branch", "任务预留必须从 main 分支创建")
    status = _git(repo, "status", "--porcelain", "--untracked-files=all")
    if status.returncode != 0:
        raise TaskPublisherError("git_status_failed", "无法检查 main 工作区", status.stderr.strip())
    if status.stdout.strip():
        raise TaskPublisherError("dirty_main", "main 工作区不干净，不能创建任务预留", status.stdout.strip())


def _git_revision(repo: Path, ref: str) -> str:
    result = _git(repo, "rev-parse", "--verify", ref)
    if result.returncode != 0:
        raise TaskPublisherError("revision_missing", "无法解析预留分支 revision", ref)
    return result.stdout.strip()


def _read_json_object(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", "-C", str(repo), *args], check=False, capture_output=True, text=True)
