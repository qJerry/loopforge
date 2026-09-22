"""项目内运行历史和事件历史读写。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        handle.write("\n")


def read_jsonl(path: Path, limit: int = 50) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({"status": "misconfigured", "summary": "历史记录包含非法 JSON"})
    return records[-limit:]


def count_consecutive_timeouts(records: Iterable[Dict[str, Any]], task_id: str) -> int:
    count = 0
    for record in reversed(list(records)):
        if record.get("task_id") != task_id:
            break
        if record.get("status") in {"timeout_continue", "timeout_blocked"}:
            count += 1
            continue
        break
    return count
