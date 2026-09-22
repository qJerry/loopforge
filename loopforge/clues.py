"""项目巡检线索读写。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import ProjectProfile
from .domain import utc_now


CLUE_STATUSES = {"open", "needs_confirmation", "resolved", "false_positive", "task_created"}
ACTIVE_CLUE_STATUSES = {"open", "needs_confirmation"}
CLUE_SEVERITIES = {"low", "medium", "high", "critical"}
SUBJECT_KINDS = {"doc", "code", "task"}
DECISIONS = {"keep_open", "needs_confirmation", "resolve", "false_positive", "create_task"}
CLUE_ID_PATTERN = re.compile(r"^clue-\d{8}-\d{3,}$")


def clues_dir(profile: ProjectProfile) -> Path:
    return profile.loopforge_dir / "clues"


def list_clues(profile: ProjectProfile, limit: int = 100) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    root = clues_dir(profile)
    if not root.exists():
        return []
    for path in sorted(root.glob("*.json")):
        try:
            with path.open("r", encoding="utf-8") as handle:
                clue = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            clue = {
                "id": path.stem,
                "status": "misconfigured",
                "summary": f"线索文件读取失败：{exc}",
                "path": str(path),
            }
        if isinstance(clue, dict):
            rows.append(clue)
    rows.sort(key=lambda item: str(item.get("last_seen_at") or item.get("created_at") or ""), reverse=True)
    return rows[: max(0, int(limit))]


def clue_counts(clues: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts = {status: 0 for status in CLUE_STATUSES}
    for clue in clues:
        status = str(clue.get("status") or "open")
        counts[status] = counts.get(status, 0) + 1
    return counts


def create_or_update_clue(profile: ProjectProfile, payload: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    normalized = _normalize_new_clue(profile, payload)
    existing = _find_dedupe_target(profile, normalized["dedupe_key"])
    if existing:
        clue = dict(existing)
        clue["status"] = "open" if clue.get("status") == "resolved" else clue.get("status", "open")
        clue["evidence"] = _merge_evidence(clue.get("evidence"), normalized.get("evidence"))
        clue["last_seen_at"] = normalized["last_seen_at"]
        clue["seen_count"] = int(clue.get("seen_count") or 0) + 1
        _write_clue(profile, clue)
        return clue, False

    _write_clue(profile, normalized)
    return normalized, True


def read_clue(profile: ProjectProfile, clue_id: str) -> Dict[str, Any]:
    clean_id = _safe_clue_id(clue_id)
    path = clues_dir(profile) / f"{clean_id}.json"
    if not path.exists():
        raise KeyError(f"未知线索：{clean_id}")
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"线索文件必须是 JSON object：{clean_id}")
    return payload


def decide_clue(
    profile: ProjectProfile,
    clue_id: str,
    decision: str,
    reason: str = "",
    task_id: Optional[str] = None,
) -> Dict[str, Any]:
    clean_decision = str(decision or "").strip()
    if clean_decision not in DECISIONS:
        raise ValueError(f"非法线索裁决：{clean_decision}")
    clue = read_clue(profile, clue_id)
    now = utc_now()
    clue["decision"] = clean_decision
    clue["decision_reason"] = reason.strip()
    clue["decided_at"] = now
    if clean_decision == "keep_open":
        clue["status"] = "open"
    elif clean_decision == "needs_confirmation":
        clue["status"] = "needs_confirmation"
    elif clean_decision == "resolve":
        clue["status"] = "resolved"
    elif clean_decision == "false_positive":
        clue["status"] = "false_positive"
    elif clean_decision == "create_task":
        if not task_id:
            raise ValueError("create_task 裁决必须提供 task_id")
        clue["status"] = "task_created"
        clue["task_id"] = task_id
    _write_clue(profile, clue)
    return clue


def _normalize_new_clue(profile: ProjectProfile, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("线索请求体必须是 JSON object")
    clue_type = _clean_slug(payload.get("type") or "manual")
    severity = str(payload.get("severity") or "medium").strip()
    if severity not in CLUE_SEVERITIES:
        raise ValueError(f"非法线索严重级别：{severity}")
    status = str(payload.get("status") or "open").strip()
    if status not in CLUE_STATUSES:
        raise ValueError(f"非法线索状态：{status}")
    subject = payload.get("subject")
    if not isinstance(subject, dict):
        raise ValueError("线索 subject 必须是 JSON object")
    subject_kind = str(subject.get("kind") or "").strip()
    if subject_kind not in SUBJECT_KINDS:
        raise ValueError(f"非法线索 subject.kind：{subject_kind}")
    subject_id = str(subject.get("id") or "").strip()
    if not subject_id:
        raise ValueError("线索 subject.id 不能为空")
    summary = str(payload.get("summary") or "").strip()
    if not summary:
        raise ValueError("线索 summary 不能为空")

    now = utc_now()
    clue_id = _next_clue_id(profile, now)
    dedupe_key = "|".join([profile.project_id, clue_type, subject_kind, subject_id])
    return {
        "id": clue_id,
        "project": profile.project_id,
        "type": clue_type,
        "severity": severity,
        "subject": {"kind": subject_kind, "id": subject_id},
        "dedupe_key": dedupe_key,
        "summary": summary,
        "evidence": _normalize_evidence(payload.get("evidence")),
        "status": status,
        "created_at": now,
        "last_seen_at": now,
        "seen_count": 1,
        "task_id": payload.get("task_id") or None,
    }


def _find_dedupe_target(profile: ProjectProfile, dedupe_key: str) -> Optional[Dict[str, Any]]:
    for clue in list_clues(profile, limit=1000):
        if clue.get("dedupe_key") != dedupe_key:
            continue
        if clue.get("status") in ACTIVE_CLUE_STATUSES or clue.get("status") in {"resolved", "false_positive"}:
            return clue
    return None


def _next_clue_id(profile: ProjectProfile, now: str) -> str:
    try:
        parsed = datetime.fromisoformat(now.replace("Z", "+00:00"))
        date_part = parsed.strftime("%Y%m%d")
    except ValueError:
        date_part = now[:10].replace("-", "") or "00000000"
    max_seq = 0
    root = clues_dir(profile)
    if root.exists():
        for path in root.glob(f"clue-{date_part}-*.json"):
            match = re.match(rf"^clue-{date_part}-(\d+)$", path.stem)
            if match:
                max_seq = max(max_seq, int(match.group(1)))
    return f"clue-{date_part}-{max_seq + 1:03d}"


def _safe_clue_id(value: str) -> str:
    clean = str(value or "").strip()
    if not CLUE_ID_PATTERN.match(clean):
        raise ValueError(f"非法线索 id：{clean}")
    return clean


def _clean_slug(value: Any) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or "").strip()).strip("_")
    return cleaned or "manual"


def _normalize_evidence(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = [str(item) for item in value if str(item).strip()]
    else:
        values = [str(value)]
    result: List[str] = []
    seen = set()
    for item in values:
        clean = item.strip()
        if clean and clean not in seen:
            result.append(clean)
            seen.add(clean)
    return result


def _merge_evidence(left: Any, right: Any) -> List[str]:
    return _normalize_evidence(_normalize_evidence(left) + _normalize_evidence(right))


def _write_clue(profile: ProjectProfile, clue: Dict[str, Any]) -> None:
    clue_id = _safe_clue_id(str(clue.get("id") or ""))
    path = clues_dir(profile) / f"{clue_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clue, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
