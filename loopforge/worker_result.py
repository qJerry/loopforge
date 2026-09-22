"""开发执行 worker-result 读取与裁决。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from .config import ProjectProfile


RECOMMENDED_STATES = {"ready_for_review", "spec_blocked", "dev_blocked", "coding"}
STATUS_TO_STATE = {
    "completed": "ready_for_review",
    "ready_for_review": "ready_for_review",
    "needs_spec": "spec_blocked",
    "spec_blocked": "spec_blocked",
    "dev_blocked": "dev_blocked",
    "failed": "dev_blocked",
}


def load_worker_result(executor_result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    inline = executor_result.get("worker_result")
    if isinstance(inline, dict):
        return {"worker_result": inline, "worker_result_source": "inline"}
    path = _worker_result_path(executor_result)
    if not path:
        return None
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "worker_result": {
                "status": "failed",
                "summary": f"worker-result 读取失败：{exc}",
                "recommended_status": "dev_blocked",
                "blockers": [{"type": "worker_result_invalid", "summary": str(exc)}],
            },
            "worker_result_path": str(path),
            "worker_result_source": "file_error",
        }
    if not isinstance(payload, dict):
        return {
            "worker_result": {
                "status": "failed",
                "summary": "worker-result 必须是 JSON object。",
                "recommended_status": "dev_blocked",
                "blockers": [{"type": "worker_result_invalid", "summary": "worker-result 必须是 JSON object。"}],
            },
            "worker_result_path": str(path),
            "worker_result_source": "file_error",
        }
    return {"worker_result": payload, "worker_result_path": str(path), "worker_result_source": "file"}


def worker_result_decision(
    loaded: Optional[Dict[str, Any]],
    task: Optional[Dict[str, Any]] = None,
    *,
    execution_root: Path | str = "",
    profile: ProjectProfile | None = None,
) -> Optional[Dict[str, Any]]:
    if not loaded:
        return None
    payload = loaded.get("worker_result")
    if not isinstance(payload, dict):
        return None
    recommended = str(payload.get("recommended_status") or payload.get("next_state") or "").strip()
    if not recommended:
        recommended = STATUS_TO_STATE.get(str(payload.get("status") or "").strip(), "")
    if recommended not in RECOMMENDED_STATES:
        recommended = "dev_blocked"
        blockers = [{"type": "worker_result_invalid", "summary": "worker-result recommended_status 非法或缺失。"}]
    else:
        blockers = _blockers(payload)
    if payload.get("contract_change_required") is True:
        recommended = "spec_blocked"
        blockers = [
            {
                "type": "contract_change_required",
                "code": "contract_change_required",
                "summary": str(payload.get("summary") or "新信息使当前任务合同无法继续满足。"),
            }
        ]
    target_results = _target_results(payload)
    if recommended == "ready_for_review":
        target_errors = _target_result_errors(task or {}, target_results)
        acceptance_errors = _acceptance_result_errors(task or {}, payload)
        if acceptance_errors:
            recommended = "dev_blocked"
            blockers = [
                {
                    "type": "acceptance_validation_failed",
                    "code": "acceptance_validation_failed",
                    "summary": "任务验收证据不完整：" + "；".join(acceptance_errors),
                }
            ]
        elif target_errors:
            recommended = "dev_blocked"
            blockers = [
                {
                    "type": "worker_result_invalid",
                    "summary": "Target 执行结果不完整：" + "；".join(target_errors),
                }
            ]
    summary = str(payload.get("summary") or "").strip() or _summary_for_state(recommended)
    decision = {
        "next_state": recommended,
        "summary": summary,
        "required_action": str(payload.get("required_action") or "").strip(),
        "blockers": blockers,
        "validation": payload.get("validation") if isinstance(payload.get("validation"), dict) else {},
        "documentation": payload.get("documentation") if isinstance(payload.get("documentation"), dict) else {},
        "review_required": bool(
            payload.get("review_required") is True
            or (isinstance(payload.get("documentation"), dict) and payload["documentation"].get("review_required") is True)
        ),
        "contract_change_required": payload.get("contract_change_required") is True,
        "targets": target_results,
        "worker_result": payload,
        "worker_result_source": loaded.get("worker_result_source"),
        "worker_result_path": loaded.get("worker_result_path"),
    }
    if recommended in {"spec_blocked", "dev_blocked"} and not decision["required_action"]:
        decision["required_action"] = _blocker_summary(blockers) or summary
    return decision


def _acceptance_result_errors(task: Dict[str, Any], payload: Dict[str, Any]) -> list[str]:
    acceptance = task.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        return []
    raw_results = payload.get("acceptance_results")
    if not isinstance(raw_results, list):
        return [f"缺少验收项结果，期望 {len(acceptance)} 项"]

    results = [result for result in raw_results if isinstance(result, dict)]
    indexes = [result.get("index") for result in results]
    errors: list[str] = []
    invalid_indexes = [index for index in indexes if isinstance(index, bool) or not isinstance(index, int)]
    if invalid_indexes:
        errors.append("acceptance_results.index 必须是整数")
    duplicates = sorted({index for index in indexes if isinstance(index, int) and indexes.count(index) > 1})
    if duplicates:
        errors.append("重复验收项 index=" + ",".join(str(index) for index in duplicates))
    by_index = {
        result["index"]: result
        for result in results
        if isinstance(result.get("index"), int) and not isinstance(result.get("index"), bool)
    }
    missing = [index for index in range(len(acceptance)) if index not in by_index]
    if missing:
        errors.append("缺少验收项 index=" + ",".join(str(index) for index in missing))
    unknown = sorted(index for index in by_index if index < 0 or index >= len(acceptance))
    if unknown:
        errors.append("未知验收项 index=" + ",".join(str(index) for index in unknown))
    for index in range(len(acceptance)):
        result = by_index.get(index)
        if not result:
            continue
        if str(result.get("status") or "").strip() != "passed":
            errors.append(f"acceptance_results[{index}].status 必须为 passed")
        evidence = result.get("evidence")
        evidence_items = evidence if isinstance(evidence, list) else [evidence]
        if not any(isinstance(item, str) and item.strip() for item in evidence_items):
            errors.append(f"acceptance_results[{index}].evidence 必须包含非空证据")
    return errors


def merge_target_results(task: Dict[str, Any], target_results: list[Dict[str, Any]]) -> Optional[list[Any]]:
    raw_targets = task.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets or not target_results:
        return None
    by_id = {str(result.get("id") or "").strip(): result for result in target_results}
    updated: list[Any] = []
    for raw in raw_targets:
        if not isinstance(raw, dict):
            updated.append(raw)
            continue
        target = dict(raw)
        target_id = str(target.get("id") or target.get("project") or "").strip()
        result = by_id.get(target_id)
        if result:
            for key in ["branch", "worktree_path"]:
                value = str(result.get(key) or "").strip()
                if value:
                    target[key] = value
            validation = result.get("validation")
            if isinstance(validation, dict):
                target["validation"] = validation
            summary = str(result.get("summary") or "").strip()
            if summary:
                target["execution_summary"] = summary
        updated.append(target)
    return updated


def _worker_result_path(executor_result: Dict[str, Any]) -> Optional[Path]:
    raw = executor_result.get("worker_result_path")
    if raw:
        return Path(str(raw))
    artifacts = executor_result.get("artifacts")
    if isinstance(artifacts, dict) and artifacts.get("worker_result_path"):
        return Path(str(artifacts["worker_result_path"]))
    return None


def _blockers(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    blockers = payload.get("blockers")
    if isinstance(blockers, list):
        return [blocker for blocker in blockers if isinstance(blocker, dict)]
    blocker = payload.get("blocker")
    if isinstance(blocker, dict):
        return [blocker]
    return []


def _target_results(payload: Dict[str, Any]) -> list[Dict[str, Any]]:
    targets = payload.get("targets")
    if not isinstance(targets, list):
        return []
    return [dict(target) for target in targets if isinstance(target, dict)]


def _target_result_errors(task: Dict[str, Any], results: list[Dict[str, Any]]) -> list[str]:
    expected_targets = task.get("targets")
    if not isinstance(expected_targets, list) or not expected_targets:
        return []
    expected_ids = [
        str(target.get("id") or target.get("project") or "").strip()
        for target in expected_targets
        if isinstance(target, dict)
    ]
    result_ids = [str(result.get("id") or "").strip() for result in results]
    errors: list[str] = []
    duplicates = sorted({target_id for target_id in result_ids if target_id and result_ids.count(target_id) > 1})
    if duplicates:
        errors.append("重复 Target id=" + ",".join(duplicates))
    missing = [target_id for target_id in expected_ids if target_id and target_id not in result_ids]
    if missing:
        errors.append("缺少 Target=" + ",".join(missing))
    unknown = [target_id for target_id in result_ids if target_id and target_id not in expected_ids]
    if unknown:
        errors.append("未知 Target=" + ",".join(unknown))
    by_id = {str(result.get("id") or "").strip(): result for result in results}
    expected_by_id = {
        str(target.get("id") or target.get("project") or "").strip(): target
        for target in expected_targets
        if isinstance(target, dict)
    }
    for target_id in expected_ids:
        result = by_id.get(target_id)
        if not result:
            continue
        if str(result.get("status") or "").strip() != "completed":
            errors.append(f"{target_id}.status 必须为 completed")
        if not str(result.get("summary") or "").strip():
            errors.append(f"{target_id}.summary 缺失")
        if not str(result.get("branch") or "").strip():
            errors.append(f"{target_id}.branch 缺失")
        if not str(result.get("worktree_path") or "").strip():
            errors.append(f"{target_id}.worktree_path 缺失")
        expected = expected_by_id.get(target_id) or {}
        for field in ("branch", "worktree_path"):
            expected_value = str(expected.get(field) or "").strip()
            actual_value = str(result.get(field) or "").strip()
            if expected_value and actual_value and expected_value != actual_value:
                errors.append(f"{target_id}.{field} 与预分配值不一致")
        validation = result.get("validation")
        if not isinstance(validation, dict) or str(validation.get("status") or "").strip() != "passed":
            errors.append(f"{target_id}.validation.status 必须为 passed")
        elif not isinstance(validation.get("commands"), list) or not validation.get("commands"):
            errors.append(f"{target_id}.validation.commands 缺失")
    return errors


def _blocker_summary(blockers: list[Dict[str, Any]]) -> str:
    summaries = [str(blocker.get("summary") or blocker.get("message") or "").strip() for blocker in blockers]
    return "；".join(summary for summary in summaries if summary)


def _summary_for_state(state: str) -> str:
    if state == "ready_for_review":
        return "开发执行完成，等待用户验收。"
    if state == "spec_blocked":
        return "开发执行发现需求或规格问题，回到需求校准。"
    if state == "dev_blocked":
        return "开发执行遇到代码或验证问题，需要继续处理。"
    return "开发执行需要继续运行。"
