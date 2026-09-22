"""Worker 运行 token 用量的统一解析与任务级投影。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .config import ProjectProfile


TOKEN_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)

API_PRICING_SOURCE = "https://developers.openai.com/api/docs/models/compare"
API_PRICING_VERSION = "2026-08-29"
LONG_CONTEXT_THRESHOLD = 272_000
API_PRICING_PER_MILLION: Dict[str, Dict[str, Any]] = {
    "gpt-5.6-sol": {"input": 4.0, "cached_input": 0.4, "output": 20.0, "long_context": True},
    "gpt-5.6-terra": {"input": 2.0, "cached_input": 0.2, "output": 12.0, "long_context": False},
    "gpt-5.6-luna": {"input": 0.2, "cached_input": 0.02, "output": 1.2, "long_context": True},
    "gpt-5.5": {"input": 5.0, "cached_input": 0.5, "output": 30.0, "long_context": True},
    "gpt-5.4": {"input": 2.5, "cached_input": 0.25, "output": 15.0, "long_context": True},
    "gpt-5.4-mini": {"input": 0.75, "cached_input": 0.075, "output": 4.5, "long_context": False},
}


def normalize_token_usage(value: Any) -> Optional[Dict[str, int]]:
    """把不可信 usage payload 归一化为稳定合同。"""
    if not isinstance(value, dict):
        return None
    normalized: Dict[str, int] = {}
    for field in TOKEN_USAGE_FIELDS:
        raw = value.get(field, 0 if field in {"cached_input_tokens", "reasoning_output_tokens"} else None)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None
        normalized[field] = raw
    if normalized["cached_input_tokens"] > normalized["input_tokens"]:
        return None
    if normalized["reasoning_output_tokens"] > normalized["output_tokens"]:
        return None
    normalized["total_tokens"] = normalized["input_tokens"] + normalized["output_tokens"]
    cache_creation = value.get("cache_creation_input_tokens")
    if cache_creation is not None:
        if isinstance(cache_creation, bool) or not isinstance(cache_creation, int) or cache_creation < 0:
            return None
        normalized["cache_creation_input_tokens"] = cache_creation
    return normalized


def normalize_claude_token_usage(value: Any) -> Optional[Dict[str, int]]:
    """把 Claude usage 投影到通用统计字段，同时保留 cache creation。"""
    if not isinstance(value, dict):
        return None
    fields: Dict[str, int] = {}
    for field in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
        raw = value.get(field, 0)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return None
        fields[field] = raw
    total_input = fields["input_tokens"] + fields["cache_creation_input_tokens"] + fields["cache_read_input_tokens"]
    return {
        "input_tokens": total_input,
        "cached_input_tokens": fields["cache_read_input_tokens"],
        "cache_creation_input_tokens": fields["cache_creation_input_tokens"],
        "output_tokens": fields["output_tokens"],
        "reasoning_output_tokens": 0,
        "total_tokens": total_input + fields["output_tokens"],
    }


def extract_token_usage(events_path: Path) -> Optional[Dict[str, int]]:
    """读取 Codex 或 Claude 事件流中最后一个合法 usage。"""
    if not events_path.is_file():
        return None
    latest: Optional[Dict[str, int]] = None
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for raw_line in handle:
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") == "turn.completed":
                    usage = normalize_token_usage(event.get("usage"))
                elif event.get("type") == "result":
                    usage = normalize_claude_token_usage(event.get("usage"))
                else:
                    continue
                if usage is not None:
                    latest = usage
    except (OSError, UnicodeError):
        return None
    return latest


def estimate_api_cost(model: str, usage: Dict[str, int]) -> Optional[Dict[str, Any]]:
    """按公开 API 单价估算一次运行的美元成本；未知模型不猜价。"""
    pricing = API_PRICING_PER_MILLION.get(str(model or "").strip())
    normalized = normalize_token_usage(usage)
    if pricing is None or normalized is None:
        return None
    uncached_input = normalized["input_tokens"] - normalized["cached_input_tokens"]
    minimum = (
        uncached_input * pricing["input"]
        + normalized["cached_input_tokens"] * pricing["cached_input"]
        + normalized["output_tokens"] * pricing["output"]
    ) / 1_000_000
    long_context = bool(pricing["long_context"] and normalized["input_tokens"] > LONG_CONTEXT_THRESHOLD)
    maximum = minimum
    if long_context:
        maximum = (
            uncached_input * pricing["input"] * 2
            + normalized["cached_input_tokens"] * pricing["cached_input"] * 2
            + normalized["output_tokens"] * pricing["output"] * 1.5
        ) / 1_000_000
    return {
        "currency": "USD",
        "minimum_usd": round(minimum, 6),
        "maximum_usd": round(maximum, 6),
        "long_context_pricing_possible": long_context,
    }


def task_associated_project_ids(owner_project_id: str, task: Dict[str, Any]) -> List[str]:
    """从任务事实中提取用于筛选的关联项目，不进行用量分摊。"""
    project_ids = {owner_project_id.strip()} if owner_project_id.strip() else set()
    targets = task.get("targets")
    if isinstance(targets, list):
        for target in targets:
            if not isinstance(target, dict):
                continue
            for field in ("project", "project_id"):
                value = target.get(field)
                if isinstance(value, str) and value.strip():
                    project_ids.add(value.strip())
    return sorted(project_ids)


def build_task_usage(
    profiles: Iterable[ProjectProfile],
    project_id: str = "",
    limit: int = 20,
) -> Dict[str, Any]:
    """从项目运行证据生成已完成任务的 token 趋势投影。"""
    issues: List[Dict[str, str]] = []
    points: List[Dict[str, Any]] = []
    safe_limit = max(1, min(100, int(limit)))
    for profile in profiles:
        records = _read_jsonl_objects(profile.history_path, profile.project_id, issues)
        events = _read_jsonl_objects(profile.events_path, profile.project_id, issues)
        points.extend(_project_task_usage(profile, records, events, issues))
    if project_id:
        points = [point for point in points if project_id in point["associated_project_ids"]]
    recent = sorted(points, key=_point_order, reverse=True)[:safe_limit]
    recent.sort(key=_point_order)
    return {"items": recent, "issues": issues}


def _read_jsonl_objects(path: Path, project_id: str, issues: List[Dict[str, str]]) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    records: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                if not raw_line.strip():
                    continue
                try:
                    value = json.loads(raw_line)
                except json.JSONDecodeError as exc:
                    _history_issue(issues, project_id, f"{path.name} 第 {line_number} 行不是合法 JSON：{exc.msg}")
                    continue
                if isinstance(value, dict):
                    records.append(value)
                else:
                    _history_issue(issues, project_id, f"{path.name} 第 {line_number} 行不是 JSON object")
    except (OSError, UnicodeError) as exc:
        _history_issue(issues, project_id, f"无法读取 {path.name}：{exc}")
    return records


def _history_issue(issues: List[Dict[str, str]], project_id: str, summary: str) -> None:
    issues.append(
        {
            "project_id": project_id,
            "kind": "token_usage_history_unreadable",
            "summary": summary,
        }
    )


def _project_task_usage(
    profile: ProjectProfile,
    records: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
    issues: List[Dict[str, str]],
) -> List[Dict[str, Any]]:
    completions: Dict[str, Dict[str, str]] = {}
    terminal_events: Dict[str, str] = {}
    for event in events:
        if event.get("event_type") != "task_transition":
            continue
        task_id = str(event.get("task_id") or "").strip()
        next_state = str(event.get("next_state") or "").strip()
        if not task_id or next_state not in {"completed", "abandoned"}:
            continue
        terminal_events[task_id] = next_state
        if next_state == "completed":
            completions[task_id] = {
                "completed_at": str(event.get("created_at") or ""),
                "title": str(event.get("task_title") or ""),
            }
        else:
            completions.pop(task_id, None)

    fallback_terminals: Dict[str, Dict[str, str]] = {}
    for record in records:
        task_id = str(record.get("task_id") or "").strip()
        if not task_id:
            continue
        terminal_state = str(record.get("next_state") or record.get("task_state") or "")
        if terminal_state in {"completed", "abandoned"}:
            fallback_terminals[task_id] = {
                "state": terminal_state,
                "completed_at": str(record.get("ended_at") or record.get("started_at") or ""),
                "title": str(record.get("task_title") or ""),
            }
    for task_id, fallback in fallback_terminals.items():
        if task_id in terminal_events:
            continue
        if fallback["state"] == "completed":
            completions[task_id] = {
                "completed_at": fallback["completed_at"],
                "title": fallback["title"],
            }
        else:
            completions.pop(task_id, None)

    grouped: Dict[str, Dict[str, Any]] = {}
    requests_by_run: Dict[str, Dict[str, Any]] = {}
    for record in records:
        task_id = str(record.get("task_id") or "").strip()
        run_id = str(record.get("run_id") or "").strip()
        if task_id not in completions or not run_id:
            continue
        group = grouped.setdefault(
            task_id,
            {
                "title": str(record.get("task_title") or completions[task_id].get("title") or task_id),
                "associated_project_ids": {profile.project_id},
                "runs": {},
            },
        )
        if record.get("task_title"):
            group["title"] = str(record["task_title"])
        if run_id not in requests_by_run:
            requests_by_run[run_id] = _read_run_request(profile, record, issues)
        request = requests_by_run[run_id]
        associated = _record_associated_project_ids(profile, record, request)
        group["associated_project_ids"].update(associated)
        usage = normalize_token_usage(record.get("token_usage"))
        events_path = _safe_run_artifact(profile, record, "events.jsonl", issues)
        if usage is None and events_path is not None:
            usage = extract_token_usage(events_path)
        started = _is_started_worker_run(record, events_path, usage)
        provider = _recorded_provider(record, request)
        model = _recorded_text(record, request, "worker_model") or _recorded_text(record, request, "codex_model")
        reasoning_effort = _recorded_text(record, request, "codex_reasoning_effort") if provider == "codex" else ""
        reported_cost_usd = _recorded_number(record, request, "reported_cost_usd")
        prior = group["runs"].get(run_id)
        if prior is None:
            group["runs"][run_id] = {
                "started": started,
                "usage": usage,
                "provider": provider,
                "model": model,
                "reasoning_effort": reasoning_effort,
                "reported_cost_usd": reported_cost_usd,
            }
        else:
            prior["started"] = prior["started"] or started
            if prior["usage"] is None and usage is not None:
                prior["usage"] = usage
            if not prior["model"] and model:
                prior["model"] = model
            if not prior["reasoning_effort"] and reasoning_effort:
                prior["reasoning_effort"] = reasoning_effort
            if prior.get("reported_cost_usd") is None and reported_cost_usd is not None:
                prior["reported_cost_usd"] = reported_cost_usd

    points: List[Dict[str, Any]] = []
    for task_id, completion in completions.items():
        group = grouped.get(task_id, {"title": completion.get("title") or task_id, "associated_project_ids": {profile.project_id}, "runs": {}})
        model_runs = [run for run in group["runs"].values() if run["started"]]
        known = [run["usage"] for run in model_runs if run["usage"] is not None]
        missing_count = len(model_runs) - len(known)
        usage_total: Optional[Dict[str, int]] = None
        if known:
            usage_total = {field: sum(usage[field] for usage in known) for field in TOKEN_USAGE_FIELDS}
            if any("cache_creation_input_tokens" in usage for usage in known):
                usage_total["cache_creation_input_tokens"] = sum(usage.get("cache_creation_input_tokens", 0) for usage in known)
            usage_total["total_tokens"] = usage_total["input_tokens"] + usage_total["output_tokens"]
        coverage = "unavailable" if not known else "partial" if missing_count else "complete"
        model_run_summaries, cost_estimate = _summarize_run_costs(model_runs)
        points.append(
            {
                "owner_project_id": profile.project_id,
                "task_id": task_id,
                "title": group["title"],
                "completed_at": completion.get("completed_at") or "",
                "associated_project_ids": sorted(group["associated_project_ids"]),
                "coverage": coverage,
                "run_count": len(model_runs),
                "missing_run_count": missing_count,
                "usage": usage_total,
                "model_runs": model_run_summaries,
                "cost_estimate": cost_estimate,
            }
        )
    return points


def _record_associated_project_ids(
    profile: ProjectProfile,
    record: Dict[str, Any],
    request: Dict[str, Any],
) -> List[str]:
    associated = {profile.project_id}
    values = record.get("associated_project_ids")
    if isinstance(values, list):
        associated.update(str(value).strip() for value in values if isinstance(value, str) and value.strip())
    request_project_id = request.get("project_id")
    if isinstance(request_project_id, str) and request_project_id.strip():
        associated.add(request_project_id.strip())
    item = request.get("item")
    if isinstance(item, dict):
        associated.update(task_associated_project_ids(profile.project_id, item))
    return sorted(associated)


def _read_run_request(
    profile: ProjectProfile,
    record: Dict[str, Any],
    issues: List[Dict[str, str]],
) -> Dict[str, Any]:
    request_path = _safe_run_artifact(profile, record, "request.json", issues)
    if request_path is None:
        return {}
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        _history_issue(issues, profile.project_id, f"无法读取 {request_path.name}：{exc}")
        return {}
    return request if isinstance(request, dict) else {}


def _recorded_text(record: Dict[str, Any], request: Dict[str, Any], field: str) -> str:
    for source in (record, request):
        value = source.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _recorded_number(record: Dict[str, Any], request: Dict[str, Any], field: str) -> float | None:
    for source in (record, request):
        value = source.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            return float(value)
    return None


def _recorded_provider(record: Dict[str, Any], request: Dict[str, Any]) -> str:
    provider = _recorded_text(record, request, "worker_provider")
    if provider:
        return provider
    executor = _recorded_text(record, request, "executor")
    if executor == "claude_cli":
        return "claude"
    if executor == "codex_cli":
        return "codex"
    return "unknown"


def _summarize_run_costs(runs: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    grouped: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    minimum = 0.0
    maximum = 0.0
    priced_count = 0
    long_context_count = 0
    for run in runs:
        key = (run["provider"], run["model"], run["reasoning_effort"])
        summary = grouped.setdefault(
            key,
            {
                "provider": run["provider"],
                "model": run["model"],
                "reasoning_effort": run["reasoning_effort"],
                "run_count": 0,
                "usage_run_count": 0,
                "priced_run_count": 0,
                "reported_cost_usd": None,
            },
        )
        summary["run_count"] += 1
        if run.get("reported_cost_usd") is not None:
            summary["reported_cost_usd"] = (summary["reported_cost_usd"] or 0.0) + run["reported_cost_usd"]
        if run["usage"] is None:
            continue
        summary["usage_run_count"] += 1
        estimate = estimate_api_cost(run["model"], run["usage"]) if run["provider"] == "codex" else None
        if estimate is None:
            continue
        summary["priced_run_count"] += 1
        priced_count += 1
        minimum += estimate["minimum_usd"]
        maximum += estimate["maximum_usd"]
        if estimate["long_context_pricing_possible"]:
            long_context_count += 1
    missing_count = len(runs) - priced_count
    coverage = "unavailable" if not priced_count else "partial" if missing_count else "complete"
    return (
        [
            {
                **grouped[key],
                "reported_cost_usd": round(grouped[key]["reported_cost_usd"], 6)
                if grouped[key]["reported_cost_usd"] is not None
                else None,
            }
            for key in sorted(grouped)
        ],
        {
            "basis": "api_equivalent",
            "currency": "USD",
            "pricing_version": API_PRICING_VERSION,
            "pricing_source": API_PRICING_SOURCE,
            "coverage": coverage,
            "minimum_usd": round(minimum, 6) if priced_count else None,
            "maximum_usd": round(maximum, 6) if priced_count else None,
            "priced_run_count": priced_count,
            "missing_run_count": missing_count,
            "long_context_run_count": long_context_count,
        },
    )


def _safe_run_artifact(
    profile: ProjectProfile,
    record: Dict[str, Any],
    filename: str,
    issues: List[Dict[str, str]],
) -> Optional[Path]:
    runs_root = (profile.loopforge_dir / "runs").resolve()
    candidates: List[Path] = []
    run_id = str(record.get("run_id") or "").strip()
    if run_id:
        candidates.append(profile.loopforge_dir / "runs" / run_id / filename)
    artifacts = record.get("artifacts")
    key = f"{filename.rsplit('.', 1)[0]}_path"
    if isinstance(artifacts, dict) and isinstance(artifacts.get(key), str):
        candidates.append(Path(artifacts[key]))
    legacy_key = "log_path" if filename == "events.jsonl" else key
    if isinstance(record.get(legacy_key), str):
        candidates.append(Path(record[legacy_key]))
    for candidate in candidates:
        try:
            resolved = candidate.expanduser().resolve()
            resolved.relative_to(runs_root)
        except (OSError, RuntimeError, ValueError):
            _history_issue(issues, profile.project_id, f"拒绝越界的 run artifact：{candidate}")
            continue
        if resolved.is_file():
            return resolved
    return None


def _is_started_worker_run(
    record: Dict[str, Any],
    events_path: Optional[Path],
    usage: Optional[Dict[str, int]],
) -> bool:
    if usage is not None:
        return True
    if record.get("executor") not in {"codex_cli", "claude_cli"} or events_path is None:
        return False
    exit_code = record.get("agent_exit_code", record.get("exit_code"))
    if exit_code == 127:
        return False
    return True


def _point_order(point: Dict[str, Any]) -> Tuple[str, str, str]:
    return (
        str(point.get("completed_at") or ""),
        str(point.get("owner_project_id") or ""),
        str(point.get("task_id") or ""),
    )
