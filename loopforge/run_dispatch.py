"""控制台与后台调度使用的独立运行分发状态。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ProjectProfile, default_config_path, load_projects
from .domain import result, utc_now
from .lock import is_lock_active, is_process_alive, read_lock


LAUNCH_GRACE_SECONDS = 30
ALLOWED_ACTIONS = {"start_run", "run_once", "resume_once", "resolve_and_resume"}


class DispatchAlreadyRunning(RuntimeError):
    """项目已经存在有效分发时拒绝重复占用。"""

    def __init__(self, dispatch: Dict[str, Any]) -> None:
        super().__init__("项目已有运行中的独立任务")
        self.dispatch = dispatch


def active_dispatch_path(profile: ProjectProfile) -> Path:
    return profile.loopforge_dir / "active-dispatch.json"


def dispatch_paths(profile: ProjectProfile, dispatch_id: str) -> Dict[str, Path]:
    root = profile.loopforge_dir / "dispatches" / dispatch_id
    return {
        "root": root,
        "request": root / "request.json",
        "log": root / "runner.log",
        "result": root / "result.json",
    }


def read_active_dispatch(profile: ProjectProfile) -> Optional[Dict[str, Any]]:
    path = active_dispatch_path(profile)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"invalid": True}
    return payload if isinstance(payload, dict) else {"invalid": True}


def active_dispatch_status(profile: ProjectProfile) -> Dict[str, Any]:
    dispatch = read_active_dispatch(profile)
    if dispatch is None:
        return {"active": False, "runtime_status": "none", "dispatch": None}
    if dispatch.get("invalid"):
        return {"active": False, "runtime_status": "stale", "dispatch": dispatch}

    state = dispatch.get("state")
    if state == "launching":
        created_at = _parse_time(dispatch.get("created_at"))
        if created_at is not None:
            age = (datetime.now(timezone.utc) - created_at).total_seconds()
            if age <= LAUNCH_GRACE_SECONDS:
                return {"active": True, "runtime_status": "launching", "dispatch": dispatch}
        return {"active": False, "runtime_status": "stale", "dispatch": dispatch}

    if state == "running" and is_process_alive(dispatch.get("pid")) is True:
        return {"active": True, "runtime_status": "running", "dispatch": dispatch}
    return {"active": False, "runtime_status": "stale", "dispatch": dispatch}


def claim_active_dispatch(profile: ProjectProfile, request: Dict[str, Any]) -> Dict[str, Any]:
    path = active_dispatch_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    status = active_dispatch_status(profile)
    if status["active"]:
        raise DispatchAlreadyRunning(status["dispatch"])
    if path.exists():
        path.unlink()

    payload = {
        "version": 1,
        "dispatch_id": request["dispatch_id"],
        "project_id": request["project_id"],
        "action": request["action"],
        "state": "launching",
        "pid": None,
        "created_at": request.get("created_at") or utc_now(),
        "started_at": None,
    }
    try:
        _write_json_exclusive(path, payload)
    except FileExistsError as exc:
        current = read_active_dispatch(profile) or {}
        raise DispatchAlreadyRunning(current) from exc
    return payload


def mark_dispatch_running(
    profile: ProjectProfile,
    dispatch_id: str,
    pid: int,
) -> Dict[str, Any]:
    current = read_active_dispatch(profile)
    if not current or current.get("dispatch_id") != dispatch_id:
        raise RuntimeError("active dispatch 已被其他运行接管")
    payload = {
        **current,
        "state": "running",
        "pid": int(pid),
        "started_at": utc_now(),
    }
    _write_json_atomic(active_dispatch_path(profile), payload)
    return payload


def clear_active_dispatch(profile: ProjectProfile, dispatch_id: str) -> bool:
    current = read_active_dispatch(profile)
    if not current or current.get("dispatch_id") != dispatch_id:
        return False
    active_dispatch_path(profile).unlink(missing_ok=True)
    return True


def write_dispatch_request(profile: ProjectProfile, request: Dict[str, Any]) -> Path:
    dispatch_id = str(request.get("dispatch_id") or "").strip()
    if not dispatch_id or Path(dispatch_id).name != dispatch_id:
        raise ValueError("dispatch_id 非法")
    paths = dispatch_paths(profile, dispatch_id)
    paths["root"].mkdir(parents=True, exist_ok=True, mode=0o700)
    paths["root"].chmod(0o700)
    _write_json_exclusive(paths["request"], request)
    return paths["request"]


def execute_dispatch_request(request_path: Path) -> Dict[str, Any]:
    request_path = request_path.expanduser().resolve()
    request: Dict[str, Any] = {}
    profile: Optional[ProjectProfile] = None
    dispatch_id = request_path.parent.name
    started_at = utc_now()
    try:
        request = _read_json_object(request_path)
        dispatch_id = str(request.get("dispatch_id") or dispatch_id)
        profile = _profile_for_request(request)
        action = str(request.get("action") or "")
        if action not in ALLOWED_ACTIONS:
            return _finish_dispatch(
                request_path,
                request,
                "failed",
                {"status": "failed", "summary": f"不支持的内部运行 action：{action}"},
                started_at=started_at,
                code="invalid_action",
            )

        config_path = Path(str(request["config_path"])).expanduser().resolve()
        project_id = str(request["project_id"])
        loop_type = str(request.get("loop_type") or "dev")
        trigger = str(request.get("trigger") or "manual")
        reason = str(request.get("reason") or "")

        from .commands import (
            project_resolve_and_resume,
            project_resume_once,
            project_run_once,
            project_start_run,
        )

        handlers = {
            "start_run": lambda: project_start_run(config_path, project_id),
            "run_once": lambda: project_run_once(
                config_path,
                project_id,
                trigger=trigger,
                loop_type=loop_type,
            ),
            "resume_once": lambda: project_resume_once(config_path, project_id, reason),
            "resolve_and_resume": lambda: project_resolve_and_resume(config_path, project_id, reason),
        }
        payload = handlers[action]()
        return _finish_dispatch(
            request_path,
            request,
            "completed",
            payload,
            started_at=started_at,
        )
    except BaseException as exc:
        return _finish_dispatch(
            request_path,
            request,
            "failed",
            {"status": "failed", "summary": str(exc)},
            started_at=started_at,
            code="worker_failed",
        )
    finally:
        if profile is not None:
            clear_active_dispatch(profile, dispatch_id)
        else:
            _clear_active_dispatch_from_request_path(request_path, dispatch_id)


def dispatch_project_action(
    config_path: Optional[Path],
    project_id: str,
    action: str,
    *,
    loop_type: str = "dev",
    reason: str = "",
    trigger: str = "manual",
) -> Dict[str, Any]:
    if action not in ALLOWED_ACTIONS:
        return result("failed", f"不支持的运行 action：{action}", code="invalid_action")

    resolved_config = (config_path or default_config_path()).expanduser().resolve()
    profile = _load_profile(resolved_config, project_id)
    if not profile.is_valid:
        return result("failed", "项目配置无效", code="project_invalid", errors=list(profile.errors))

    lock = read_lock(profile.lock_path)
    if lock and is_lock_active(lock):
        return result(
            "skipped_already_running",
            "项目已有运行中的任务，本次触发已跳过。",
            lock=lock,
        )

    dispatch_id = uuid.uuid4().hex
    created_at = utc_now()
    request = {
        "version": 1,
        "dispatch_id": dispatch_id,
        "project_id": profile.project_id,
        "action": action,
        "loop_type": loop_type,
        "reason": reason,
        "trigger": trigger,
        "config_path": str(resolved_config),
        "created_at": created_at,
    }
    try:
        claim_active_dispatch(profile, request)
    except DispatchAlreadyRunning as exc:
        return result(
            "skipped_already_running",
            "项目已有已受理的独立任务，本次触发已跳过。",
            dispatch=exc.dispatch,
        )

    request_path = dispatch_paths(profile, dispatch_id)["request"]
    try:
        write_dispatch_request(profile, request)
        command = [
            sys.executable,
            "-m",
            "loopforge.cli",
            "--config",
            str(resolved_config),
            "run-worker",
            "--request",
            str(request_path.resolve()),
        ]
        process = spawn_detached_process(command, dispatch_paths(profile, dispatch_id)["log"])
        try:
            mark_dispatch_running(profile, dispatch_id, process.pid)
        except RuntimeError:
            if not dispatch_paths(profile, dispatch_id)["result"].exists():
                raise
        accepted_fields: Dict[str, Any] = {"dispatch_id": dispatch_id, "pid": process.pid}
        return result("accepted", "运行已受理，将在后台继续执行。", **accepted_fields)
    except BaseException as exc:
        payload = {"status": "failed", "summary": f"独立运行进程启动失败：{exc}"}
        _finish_dispatch(
            request_path,
            request,
            "failed_to_start",
            payload,
            started_at=created_at,
            code="failed_to_start",
        )
        clear_active_dispatch(profile, dispatch_id)
        return result(
            "failed",
            payload["summary"],
            code="failed_to_start",
            dispatch_id=dispatch_id,
        )


def spawn_detached_process(command: list[str], log_path: Path) -> subprocess.Popen[Any]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            command,
            cwd=Path(__file__).resolve().parent.parent,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
        wait = getattr(process, "wait", None)
        if callable(wait):
            threading.Thread(
                target=wait,
                name=f"loopforge-dispatch-reaper-{process.pid}",
                daemon=True,
            ).start()
        return process
    finally:
        log_handle.close()


def schedule_dispatch_tick(
    config_path: Optional[Path],
    respect_frequency: bool = False,
) -> Dict[str, Any]:
    from .commands import schedule_tick

    return schedule_tick(
        config_path,
        respect_frequency=respect_frequency,
        run_project=lambda selected_config, project_id, trigger, loop_type: dispatch_project_action(
            selected_config,
            project_id,
            "run_once",
            loop_type=loop_type,
            trigger=trigger,
        ),
    )


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _read_json_object(path: Path) -> Dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("dispatch request 必须是 JSON 对象")
    return payload


def _profile_for_request(request: Dict[str, Any]) -> ProjectProfile:
    config_path = Path(str(request.get("config_path") or "")).expanduser().resolve()
    project_id = str(request.get("project_id") or "")
    return _load_profile(config_path, project_id)


def _load_profile(config_path: Path, project_id: str) -> ProjectProfile:
    for profile in load_projects(config_path):
        if profile.project_id == project_id:
            return profile
    raise ValueError(f"项目不存在：{project_id}")


def _clear_active_dispatch_from_request_path(request_path: Path, dispatch_id: str) -> bool:
    dispatches_dir = request_path.parent.parent
    if dispatches_dir.name != "dispatches":
        return False
    path = dispatches_dir.parent / "active-dispatch.json"
    try:
        current = _read_json_object(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if current.get("dispatch_id") != dispatch_id:
        return False
    path.unlink(missing_ok=True)
    return True


def _finish_dispatch(
    request_path: Path,
    request: Dict[str, Any],
    status: str,
    payload: Dict[str, Any],
    *,
    started_at: str,
    code: str = "",
) -> Dict[str, Any]:
    result = {
        "version": 1,
        "dispatch_id": request.get("dispatch_id"),
        "project_id": request.get("project_id"),
        "action": request.get("action"),
        "status": status,
        "started_at": started_at,
        "ended_at": utc_now(),
        "payload": payload,
    }
    if code:
        result["code"] = code
    _write_json_atomic(request_path.parent / "result.json", result)
    return result


def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_json_exclusive(path: Path, payload: Dict[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(payload))
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_json_bytes(payload))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
