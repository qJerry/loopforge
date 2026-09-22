"""任务规划能力边界。外部系统只通过适配器元数据与 LoopForge 交互。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from .config import ProjectProfile


def planning_context(profile: ProjectProfile, item: Dict[str, Any]) -> Dict[str, Any]:
    provider = str(profile.planning_adapter or "builtin").strip() or "builtin"
    item_id = str(item.get("id") or "task").strip() or "task"
    if provider != "builtin":
        context: Dict[str, Any] = {"provider": provider, "mode": "external"}
        planning = item.get("planning")
        if isinstance(planning, dict):
            for key in ("status", "reference", "summary"):
                value = planning.get(key)
                if value not in (None, ""):
                    context[key] = value
        return context
    workspace = (profile.loopforge_dir / "task" / item_id).resolve()
    return {
        "provider": "builtin",
        "mode": "builtin",
        "workspace_path": str(workspace),
        "required_artifacts": ["prd.md", "design.md", "implement.md"],
    }


def builtin_planning_paths(profile: ProjectProfile, item: Dict[str, Any]) -> Dict[str, Path]:
    context = planning_context(profile, item)
    if context["mode"] != "builtin":
        return {}
    workspace = Path(str(context["workspace_path"]))
    return {name: workspace / name for name in context["required_artifacts"]}


def ensure_planning_workspace(profile: ProjectProfile, item: Dict[str, Any]) -> Dict[str, Any]:
    context = planning_context(profile, item)
    if context["mode"] != "builtin":
        return context
    workspace = Path(str(context["workspace_path"]))
    workspace.mkdir(parents=True, exist_ok=True)
    metadata = workspace / "planning.json"
    metadata.write_text(
        json.dumps(
            {
                "version": 1,
                "provider": "builtin",
                "task_id": str(item.get("id") or ""),
                "planning_level": str(item.get("planning_level") or "complex"),
                "required_artifacts": context["required_artifacts"],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return {**context, "metadata_path": str(metadata)}


def planning_gate(profile: ProjectProfile, item: Dict[str, Any], worker_result: Dict[str, Any]) -> Dict[str, Any]:
    context = planning_context(profile, item)
    if context["mode"] == "external":
        payload = worker_result.get("worker_result") if isinstance(worker_result.get("worker_result"), dict) else worker_result
        planning = payload.get("planning") if isinstance(payload, dict) and isinstance(payload.get("planning"), dict) else {}
        provider_matches = str(planning.get("provider") or "") == context["provider"]
        ready = provider_matches and str(planning.get("status") or "") == "passed" and bool(str(planning.get("reference") or "").strip())
        return {
            **context,
            "ready": ready,
            "summary": "外部规划适配器门禁通过。" if ready else "外部规划适配器必须返回匹配 provider、passed 状态和 reference。",
            "reference": planning.get("reference"),
        }
    paths = builtin_planning_paths(profile, item)
    planning_level = str(item.get("planning_level") or "complex")
    required_names = ("prd.md",) if planning_level == "lightweight" else ("prd.md", "design.md", "implement.md")
    missing = [name for name in required_names if not paths[name].is_file() or not paths[name].read_text(encoding="utf-8").strip()]
    return {
        **context,
        "ready": not missing,
        "missing": missing,
        "summary": "builtin planning 门禁通过。" if not missing else f"builtin planning 缺少有效工件：{', '.join(missing)}",
    }
