"""任务验收来源的共享判定合同。"""

from __future__ import annotations

from typing import Any, Dict

from .document_phases import DocumentPhaseError, ordered_document_phases


def task_uses_document_contract(task: Dict[str, Any]) -> bool:
    """完整三件套任务使用文档合同，否则使用任务级验收合同。"""

    root_docs = task.get("docs") if isinstance(task.get("docs"), dict) else {}
    document_sets: list[Dict[str, Any]] = []
    if root_docs:
        document_sets.append(root_docs)

    targets = task.get("targets") if isinstance(task.get("targets"), list) else []
    for target in targets:
        if not isinstance(target, dict):
            continue
        docs = target.get("docs") if isinstance(target.get("docs"), dict) else {}
        if docs:
            document_sets.append(docs)

    return bool(document_sets) and all(_has_complete_triplet(document_set) for document_set in document_sets)


def _has_complete_triplet(document_set: Dict[str, Any]) -> bool:
    try:
        phases = ordered_document_phases(document_set)
    except DocumentPhaseError:
        return False
    if not phases:
        return False
    return all(
        _non_empty_paths(phase.get("requirements"))
        and _non_empty_paths(phase.get("design"))
        and _non_empty_paths(phase.get("specs"))
        for phase in phases
    )


def _non_empty_paths(value: Any) -> bool:
    return isinstance(value, list) and any(isinstance(path, str) and path.strip() for path in value)
