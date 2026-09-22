"""项目级锁。"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from .config import ProjectProfile
from .domain import utc_now


LOCK_TTL_MINUTES = 100
LOCK_HEARTBEAT_INTERVAL_SECONDS = 30


def _parse_time(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def read_lock(path: Path) -> Optional[Dict[str, Any]]:
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"invalid": True}


def is_lock_active(lock: Dict[str, Any]) -> bool:
    expires_at = _parse_time(lock.get("expires_at", ""))
    if not expires_at or expires_at <= datetime.now(timezone.utc):
        return False
    pid_alive = is_process_alive(lock.get("pid"))
    if pid_alive is False:
        return False
    return True


def is_process_alive(pid: Any) -> Optional[bool]:
    try:
        parsed_pid = int(pid)
    except (TypeError, ValueError):
        return None
    if parsed_pid <= 0:
        return None
    try:
        os.kill(parsed_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def stale_lock_reason(lock: Optional[Dict[str, Any]]) -> str:
    if not lock or lock.get("invalid"):
        return ""
    expires_at = _parse_time(lock.get("expires_at", ""))
    if not expires_at:
        return "锁文件时间格式无效"
    if expires_at <= datetime.now(timezone.utc):
        return "锁已过期"
    if is_process_alive(lock.get("pid")) is False:
        return "锁记录的后台进程已退出"
    return ""


def acquire_lock(profile: ProjectProfile, run_id: str, loop_type: str = "dev") -> Optional[Dict[str, Any]]:
    existing = read_lock(profile.lock_path)
    if existing and is_lock_active(existing):
        return existing
    profile.lock_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "project_id": profile.project_id,
        "pid": os.getpid(),
        "started_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=LOCK_TTL_MINUTES)).isoformat(),
        "run_id": run_id,
        "loop_type": loop_type,
    }
    with profile.lock_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
    return None


def refresh_lock(profile: ProjectProfile, run_id: str) -> bool:
    lock = read_lock(profile.lock_path)
    if not lock or lock.get("invalid") or lock.get("run_id") != run_id:
        return False
    now = datetime.now(timezone.utc).replace(microsecond=0)
    lock["heartbeat_at"] = now.isoformat()
    lock["expires_at"] = (now + timedelta(minutes=LOCK_TTL_MINUTES)).isoformat()
    with profile.lock_path.open("w", encoding="utf-8") as handle:
        json.dump(lock, handle, ensure_ascii=False, sort_keys=True)
    return True


def release_lock(profile: ProjectProfile, run_id: str) -> None:
    lock = read_lock(profile.lock_path)
    if lock and lock.get("run_id") == run_id:
        profile.lock_path.unlink(missing_ok=True)


@contextmanager
def project_lock(profile: ProjectProfile, run_id: str, loop_type: str = "dev") -> Iterator[Optional[Dict[str, Any]]]:
    conflict = acquire_lock(profile, run_id, loop_type)
    try:
        yield conflict
    finally:
        if conflict is None:
            release_lock(profile, run_id)


@contextmanager
def lock_heartbeat(
    profile: ProjectProfile,
    run_id: str,
    interval_seconds: Optional[float] = None,
) -> Iterator[None]:
    interval = LOCK_HEARTBEAT_INTERVAL_SECONDS if interval_seconds is None else interval_seconds
    stop = threading.Event()

    def heartbeat() -> None:
        while not stop.wait(interval):
            if not refresh_lock(profile, run_id):
                return

    refresh_lock(profile, run_id)
    thread = threading.Thread(target=heartbeat, name=f"loopforge-lock-heartbeat-{run_id}", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def lock_record(profile: ProjectProfile, run_id: str, trigger: str, loop_type: str = "dev") -> Dict[str, Any]:
    now = utc_now()
    return {
        "run_id": run_id,
        "project_id": profile.project_id,
        "trigger": trigger,
        "loop_type": loop_type,
        "status": "skipped_already_running",
        "started_at": now,
        "ended_at": now,
        "duration_seconds": 0,
        "summary": "项目已有运行中的任务，本次触发已跳过。",
        "required_action": "",
        "notification": {"status": "skipped"},
    }
