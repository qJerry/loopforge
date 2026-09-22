#!/usr/bin/env python3
"""导入项目测试 runner。"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone


PROJECT_ID = "import-project"


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def base(command: str) -> dict:
    return {
        "schema_version": "1",
        "project_id": PROJECT_ID,
        "command": command,
        "status": "completed",
        "observed_at": now(),
        "summary": "import project runner ok",
    }


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    payload = base(command)
    if command == "status":
        payload.update(
            {
                "project_status": "idle",
                "active_task": {
                    "id": "import-task-001",
                    "title": "导入后读取任务清单",
                    "state": "open",
                    "state_label": "待领取",
                    "next_action": "从任务管理区确认 item 可见",
                },
                "last_run": None,
                "latest_report": None,
            }
        )
    elif command == "tasks":
        payload.update(
            {
                "tasks": [
                    {
                        "id": "import-task-001",
                        "title": "导入后读取任务清单",
                        "description": "导入项目后，控制台应立即能从 runner 读取非终态 item。",
                        "state": "open",
                        "state_label": "待领取",
                        "source": "seed",
                        "created_at": "2026-06-30T00:00:00+00:00",
                        "updated_at": "2026-06-30T00:00:00+00:00",
                        "acceptance_criteria": [
                            "项目出现在全部项目列表",
                            "选中新项目后任务管理展示该 item",
                        ],
                    }
                ],
                "capabilities": {"task_add": False, "task_ai_create": False, "task_reorder": False},
            }
        )
    elif command == "latest-report":
        payload.update({"exists": False, "report_path": None})
    elif command == "run-once":
        payload.update(
            {
                "run_id": "import-run-001",
                "trigger": "manual",
                "ended_at": now(),
                "duration_seconds": 0,
                "task_id": "import-task-001",
            }
        )
    else:
        payload.update({"status": "skipped_not_implemented", "summary": f"未实现命令：{command}"})
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
