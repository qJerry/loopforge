#!/usr/bin/env python3
"""可观察任务队列 demo runner。"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


STATES = [
    ("open", "待领取", "领取 backlog 中的第一个任务"),
    ("claimed", "已领取", "创建项目任务并挂接规格文档"),
    ("spec_ready", "规格就绪", "补齐需求文档并进入实现条件检查"),
    ("prd_ready", "文档就绪", "开始编码和验证"),
    ("coding", "实现中", "完成验证并归档任务"),
    ("completed", "已完成", "任务已完成，等待新的 backlog"),
]

DEMO_TASKS = [
    ("demo-task-001", "观察任务完成全流程"),
    ("demo-task-002", "补充运行报告入口"),
    ("demo-task-003", "验证通知重发链路"),
]

STATE_DIR = Path(".loopforge")
STATE_FILE = STATE_DIR / "demo_state.json"
RUNNING_STATES = {"claimed", "spec_ready", "prd_ready", "coding"}
TERMINAL_STATES = {"completed", "abandoned"}
STATE_LABELS = {state: label for state, label, _action in STATES}
EXTRA_STATE_LABELS = {"abandoned": "已放弃"}


def now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def read_input() -> dict:
    raw = sys.stdin.read().strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def stage_sleep_seconds() -> float:
    raw = os.environ.get("LOOPFORGE_DEMO_STAGE_SLEEP", "0.8")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 0.8


def state_index(value: str) -> int:
    for index, (state, _label, _action) in enumerate(STATES):
        if state == value:
            return index
    return 0


def state_label(value: str) -> str:
    return STATE_LABELS.get(value) or EXTRA_STATE_LABELS.get(value) or value


def normalize_state(value: str) -> str:
    if value in STATE_LABELS or value in EXTRA_STATE_LABELS:
        return value
    return "open"


def default_tasks() -> list[dict]:
    return [
        {
            "task_id": task_id,
            "title": title,
            "description": f"Demo backlog item：{title}",
            "acceptance_criteria": ["进入任务队列", "可被调度执行", "运行历史可观察"],
            "source": "seed",
            "created_at": now(),
            "current_state": "open",
            "completed_at": None,
            "updated_at": now(),
        }
        for task_id, title in DEMO_TASKS
    ]


def default_state() -> dict:
    return {"tasks": default_tasks(), "updated_at": now()}


def normalize_task(payload: dict, fallback: tuple[str, str]) -> dict:
    task_id, title = fallback
    current = str(payload.get("current_state") or "open")
    criteria = payload.get("acceptance_criteria")
    if not isinstance(criteria, list):
        criteria = []
    return {
        "task_id": str(payload.get("task_id") or task_id),
        "title": str(payload.get("title") or title),
        "description": str(payload.get("description") or ""),
        "acceptance_criteria": [str(item) for item in criteria],
        "source": str(payload.get("source") or "manual"),
        "created_at": str(payload.get("created_at") or now()),
        "current_state": normalize_state(current),
        "completed_at": payload.get("completed_at"),
        "updated_at": str(payload.get("updated_at") or now()),
    }


def load_state() -> dict:
    if not STATE_FILE.exists():
        return default_state()
    try:
        raw = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default_state()
    if not isinstance(raw, dict):
        return default_state()

    raw_tasks = raw.get("tasks")
    if not isinstance(raw_tasks, list):
        legacy = normalize_task(raw, DEMO_TASKS[0])
        raw_tasks = [legacy]

    tasks = []
    total = max(len(raw_tasks), len(DEMO_TASKS))
    for index in range(total):
        fallback = DEMO_TASKS[index] if index < len(DEMO_TASKS) else (f"demo-task-{index + 1:03d}", f"Demo 任务 {index + 1}")
        source = raw_tasks[index] if index < len(raw_tasks) and isinstance(raw_tasks[index], dict) else {}
        tasks.append(normalize_task(source, fallback))

    return {"tasks": tasks, "updated_at": str(raw.get("updated_at") or now())}


def save_state(payload: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def timeline(current_state: str) -> list[dict]:
    current_index = state_index(current_state)
    rows = []
    for index, (state, label, action) in enumerate(STATES):
        if index < current_index:
            phase = "done"
        elif index == current_index:
            phase = "current"
        else:
            phase = "waiting"
        rows.append(
            {
                "state": state,
                "label": label,
                "phase": phase,
                "action": action,
                "order": index + 1,
            }
        )
    return rows


def active_task(task: Optional[dict]) -> Optional[dict]:
    if task is None:
        return None
    current_state = str(task["current_state"])
    index = state_index(current_state)
    _state, label, action = STATES[index]
    return {
        "id": task["task_id"],
        "title": task["title"],
        "description": task.get("description") or "",
        "acceptance_criteria": task.get("acceptance_criteria") or [],
        "source": task.get("source") or "manual",
        "state": current_state,
        "state_label": label,
        "can_continue": current_state != "completed",
        "next_action": action,
        "timeline": timeline(current_state),
    }


def task_item(task: dict) -> dict:
    current_state = str(task["current_state"])
    return {
        "id": task["task_id"],
        "title": task["title"],
        "description": task.get("description") or "",
        "acceptance_criteria": task.get("acceptance_criteria") or [],
        "source": task.get("source") or "manual",
        "state": current_state,
        "state_label": state_label(current_state),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
        "completed_at": task.get("completed_at"),
    }


def first_open_task(state: dict) -> Optional[dict]:
    for task in state["tasks"]:
        if task["current_state"] not in TERMINAL_STATES:
            return task
    return None


def next_open_task(state: dict, completed_task_id: str) -> Optional[dict]:
    found_completed = False
    for task in state["tasks"]:
        if task["task_id"] == completed_task_id:
            found_completed = True
            continue
        if found_completed and task["current_state"] not in TERMINAL_STATES:
            return task
    return first_open_task(state)


def backlog_summary(state: dict) -> dict:
    total = len(state["tasks"])
    completed = sum(1 for task in state["tasks"] if task["current_state"] in TERMINAL_STATES)
    return {"total": total, "completed": completed, "open": total - completed}


def base(command: str, status: str = "completed") -> dict:
    return {
        "schema_version": "1",
        "project_id": "demo",
        "command": command,
        "status": status,
        "observed_at": now(),
        "summary": "demo runner ok",
    }


def command_status() -> dict:
    state = load_state()
    current_task = first_open_task(state)
    completed = current_task is None
    current_task_state = str(current_task["current_state"]) if current_task else None
    response = base("status")
    response.update(
        {
            "project_status": "completed" if completed else "running" if current_task_state in RUNNING_STATES else "idle",
            "active_task": active_task(current_task),
            "has_open_backlog": not completed,
            "backlog": backlog_summary(state),
            "last_run": None,
            "latest_report": None,
            "next_action": "demo backlog 已完成" if completed else active_task(current_task)["next_action"],
        }
    )
    return response


def completed_task_view(task: dict) -> dict:
    view = dict(task)
    view["current_state"] = "completed"
    return active_task(view)


def append_task(state: dict, title: str, description: str, source: str, acceptance_criteria: list[str]) -> dict:
    observed_at = now()
    task = {
        "task_id": f"demo-task-{uuid.uuid4().hex[:8]}",
        "title": title,
        "description": description,
        "acceptance_criteria": acceptance_criteria,
        "source": source,
        "created_at": observed_at,
        "current_state": "open",
        "completed_at": None,
        "updated_at": observed_at,
    }
    state["tasks"].append(task)
    state["updated_at"] = observed_at
    save_state(state)
    return task


def active_tasks(state: dict) -> list[dict]:
    return [task for task in state["tasks"] if task["current_state"] not in TERMINAL_STATES]


def command_tasks() -> dict:
    state = load_state()
    response = base("tasks")
    response.update(
        {
            "tasks": [task_item(task) for task in active_tasks(state)],
            "capabilities": {"task_add": True, "task_ai_create": True, "task_reorder": True},
            "backlog": backlog_summary(state),
            "summary": "demo 非终态任务列表已读取",
        }
    )
    return response


def command_task_add() -> dict:
    payload = read_input()
    title = str(payload.get("title") or "").strip()
    if not title:
        response = base("task-add", "failed")
        response.update({"summary": "任务标题不能为空"})
        return response
    description = str(payload.get("description") or "").strip()
    criteria = payload.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        criteria = ["任务 item 已进入 backlog", "下一轮调度可领取该任务", "完成后写入运行历史"]
    state = load_state()
    task = append_task(state, title, description, "manual", [str(item) for item in criteria])
    response = base("task-add")
    response.update({"summary": f"已新增任务：{title}", "task": task_item(task), "backlog": backlog_summary(state)})
    return response


def command_task_ai_create() -> dict:
    payload = read_input()
    prompt = " ".join(str(payload.get("prompt") or "").split())
    if not prompt:
        response = base("task-ai-create", "failed")
        response.update({"summary": "一句话需求不能为空"})
        return response
    title = prompt if len(prompt) <= 36 else prompt[:36].rstrip() + "..."
    description = f"基于一句话需求生成：{prompt}"
    criteria = [
        "需求已拆成可执行任务 item",
        "实现后能在控制台观察状态变化",
        "完成后运行历史包含该任务结果",
    ]
    state = load_state()
    task = append_task(state, title, description, "ai", criteria)
    response = base("task-ai-create")
    response.update(
        {
            "summary": f"AI 已创建任务：{title}",
            "prompt": prompt,
            "task": task_item(task),
            "backlog": backlog_summary(state),
        }
    )
    return response


def command_task_reorder() -> dict:
    payload = read_input()
    raw_ordered_ids = payload.get("ordered_ids")
    if not isinstance(raw_ordered_ids, list):
        response = base("task-reorder", "failed")
        response.update({"summary": "任务排序入参必须是 ordered_ids 数组"})
        return response

    ordered_ids = [str(item).strip() for item in raw_ordered_ids if str(item).strip()]
    state = load_state()
    active = active_tasks(state)
    if not ordered_ids or not active:
        response = base("task-reorder", "failed")
        response.update({"summary": "当前没有可排序的非终态任务"})
        return response

    by_id = {task["task_id"]: task for task in active}
    reordered: list[dict] = []
    seen: set[str] = set()
    for task_id in ordered_ids:
        task = by_id.get(task_id)
        if task is None or task_id in seen:
            continue
        reordered.append(task)
        seen.add(task_id)

    if not reordered:
        response = base("task-reorder", "failed")
        response.update({"summary": "排序列表没有匹配到非终态任务"})
        return response

    for task in active:
        task_id = str(task["task_id"])
        if task_id not in seen:
            reordered.append(task)

    terminal = [task for task in state["tasks"] if task["current_state"] in TERMINAL_STATES]
    observed_at = now()
    state["tasks"] = reordered + terminal
    state["updated_at"] = observed_at
    save_state(state)

    response = base("task-reorder")
    response.update(
        {
            "summary": "demo 任务顺序已更新",
            "tasks": [task_item(task) for task in reordered],
            "ordered_ids": [task["task_id"] for task in reordered],
            "backlog": backlog_summary(state),
        }
    )
    return response


def command_run_once() -> dict:
    state = load_state()
    task = first_open_task(state)
    if task is None:
        response = base("run-once", "skipped_no_task")
        response.update(
            {
                "run_id": uuid.uuid4().hex,
                "trigger": "manual",
                "started_at": now(),
                "ended_at": now(),
                "duration_seconds": 0,
                "task_id": None,
                "task_title": None,
                "task_state": "completed",
                "previous_state": "completed",
                "next_state": "completed",
                "required_action": "demo backlog 已全部完成，可执行 reset-demo 重新观察。",
                "report_path": None,
                "notification_intent": "none",
                "active_task": None,
                "backlog": backlog_summary(state),
            }
        )
        return response

    started_at_dt = utc_now()
    started_at = started_at_dt.isoformat()
    state_path = [state for state, _label, _action in STATES]
    state_path_labels = [label for _state, label, _action in STATES]
    sleep_seconds = stage_sleep_seconds()

    for state_name, _label, _action in STATES:
        observed_at = now()
        task["current_state"] = state_name
        task["updated_at"] = observed_at
        state["updated_at"] = observed_at
        if state_name == "completed":
            task["completed_at"] = observed_at
        save_state(state)
        if state_name != "completed" and sleep_seconds:
            time.sleep(sleep_seconds)

    ended_at_dt = utc_now()
    ended_at = ended_at_dt.isoformat()
    save_state(state)

    next_task = next_open_task(state, str(task["task_id"]))
    if next_task is None:
        required_action = "本任务已完成，demo backlog 已全部完成。"
    else:
        required_action = f"本任务已完成；下次调度将运行：{next_task['title']}。"

    response = base("run-once")
    response.update(
        {
            "run_id": uuid.uuid4().hex,
            "trigger": "manual",
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_seconds": int((ended_at_dt - started_at_dt).total_seconds()),
            "task_id": task["task_id"],
            "task_title": task["title"],
            "task_state": "completed",
            "previous_state": "open",
            "next_state": "completed",
            "state_path": state_path,
            "state_path_labels": state_path_labels,
            "required_action": required_action,
            "report_path": None,
            "notification_intent": "none",
            "summary": f"任务已完整执行：{task['title']}",
            "active_task": completed_task_view(task),
            "next_task": active_task(next_task),
            "backlog": backlog_summary(state),
        }
    )
    return response


def command_latest_report() -> dict:
    response = base("latest-report")
    response.update({"exists": False, "report_path": None})
    return response


def command_reset() -> dict:
    for path in [STATE_DIR / "index.jsonl", STATE_DIR / "events.jsonl", STATE_DIR / "lock.json"]:
        path.unlink(missing_ok=True)
    payload = default_state()
    save_state(payload)
    response = base("reset-demo")
    response.update({"active_task": active_task(first_open_task(payload)), "backlog": backlog_summary(payload)})
    return response


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "status"
    if command == "status":
        emit(command_status())
    elif command == "tasks":
        emit(command_tasks())
    elif command == "task-add":
        emit(command_task_add())
    elif command == "task-ai-create":
        emit(command_task_ai_create())
    elif command == "task-reorder":
        emit(command_task_reorder())
    elif command == "run-once":
        emit(command_run_once())
    elif command == "latest-report":
        emit(command_latest_report())
    elif command in {"pause", "resume", "notify-test"}:
        response = base(command)
        response.update({"summary": f"demo 项目已记录 {command}。"})
        emit(response)
    elif command == "reset-demo":
        emit(command_reset())
    else:
        response = base(command, "failed")
        response.update({"summary": "未知命令"})
        emit(response)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
