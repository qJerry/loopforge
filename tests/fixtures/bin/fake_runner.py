#!/usr/bin/env python3
"""测试用项目 runner。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def emit(payload):
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    base = {
        "schema_version": "1",
        "project_id": "fake",
        "command": command,
        "status": "completed",
        "observed_at": now(),
        "summary": "fake runner ok",
    }
    if command == "status":
        base.update(
            {
                "project_status": "idle",
                "active_task": None,
                "has_open_backlog": True,
                "last_run": None,
                "latest_report": None,
            }
        )
    elif command == "run-once":
        base.update(
            {
                "status": "skipped_not_implemented",
                "run_id": "runner-run-id",
                "trigger": "manual",
                "started_at": now(),
                "ended_at": now(),
                "duration_seconds": 0,
                "task_id": None,
                "required_action": "首版 fake runner 不执行任务。",
                "report_path": None,
                "notification_intent": "none",
            }
        )
    elif command in {"pause", "resume", "notify-test"}:
        pass
    elif command == "latest-report":
        base.update({"exists": False, "report_path": None})
    else:
        base.update({"status": "failed", "summary": "未知命令"})
    emit(base)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
