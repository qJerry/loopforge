"""运行取消请求读写。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ProjectProfile
from .domain import utc_now


RUN_ID_PATTERN = re.compile(r"^[a-zA-Z0-9_.:-]+$")


def cancel_requests_dir(profile: ProjectProfile) -> Path:
    return profile.loopforge_dir / "cancel-requests"


def cancel_request_path(profile: ProjectProfile, run_id: str) -> Path:
    return cancel_requests_dir(profile) / f"{_safe_run_id(run_id)}.json"


def write_cancel_request(
    profile: ProjectProfile,
    run_id: str,
    reason: str = "",
    requested_by: str = "user",
) -> Dict[str, Any]:
    payload = {
        "run_id": _safe_run_id(run_id),
        "project_id": profile.project_id,
        "reason": reason.strip() or "用户取消运行",
        "requested_at": utc_now(),
        "requested_by": requested_by.strip() or "user",
    }
    path = cancel_request_path(profile, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def read_cancel_request(profile: ProjectProfile, run_id: str) -> Optional[Dict[str, Any]]:
    path = cancel_request_path(profile, run_id)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"run_id": run_id, "project_id": profile.project_id, "invalid": True}
    return payload if isinstance(payload, dict) else {"run_id": run_id, "project_id": profile.project_id, "invalid": True}


def _safe_run_id(value: str) -> str:
    clean = str(value or "").strip()
    if not clean or not RUN_ID_PATTERN.match(clean) or ".." in clean or "/" in clean or "\\" in clean:
        raise ValueError(f"非法 run_id：{clean}")
    return clean
