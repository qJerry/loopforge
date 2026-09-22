"""权威文档阶段的兼容投影与稳定拓扑排序。"""

from __future__ import annotations

import re
from typing import Any, Dict, List


_PHASE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_LEGACY_PHASE_FIELDS = (
    "module",
    "requirements",
    "design",
    "specs",
)


class DocumentPhaseError(ValueError):
    """文档阶段拓扑不满足稳定执行合同。"""

    def __init__(self, code: str, summary: str, detail: Dict[str, Any] | None = None) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary
        self.detail = detail or {}


def ordered_document_phases(document_set: Dict[str, Any]) -> List[Dict[str, Any]]:
    """返回显式 Phase 的稳定拓扑顺序，或把 legacy docs 投影为单阶段。"""

    raw_phases = document_set.get("phases")
    if raw_phases is not None:
        return _ordered_explicit_phases(raw_phases)

    module = str(document_set.get("module") or "").strip()
    if not module:
        return []
    phase = {
        key: document_set[key]
        for key in _LEGACY_PHASE_FIELDS
        if key in document_set
    }
    phase.update({"id": module, "title": module, "after": []})
    return [phase]


def _ordered_explicit_phases(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise DocumentPhaseError("document_phases_invalid", "docs.phases 必须是非空数组")

    phases: List[Dict[str, Any]] = []
    indexes: Dict[str, int] = {}
    for index, raw in enumerate(value):
        if not isinstance(raw, dict):
            raise DocumentPhaseError(
                "document_phase_invalid",
                f"docs.phases[{index}] 必须是 object",
                {"phase_index": index},
            )
        phase_id = str(raw.get("id") or "").strip()
        if not _PHASE_ID.fullmatch(phase_id):
            raise DocumentPhaseError(
                "document_phase_id_invalid",
                f"docs.phases[{index}].id 非法：{phase_id or '<empty>'}",
                {"phase_index": index, "phase_id": phase_id},
            )
        if phase_id in indexes:
            raise DocumentPhaseError(
                "document_phase_duplicate",
                f"docs.phases.id 重复：{phase_id}",
                {"phase_id": phase_id},
            )
        raw_after = raw.get("after", [])
        if not isinstance(raw_after, list) or any(not isinstance(item, str) or not item.strip() for item in raw_after):
            raise DocumentPhaseError(
                "document_phase_after_invalid",
                f"docs.phases[{phase_id}].after 必须是非空字符串数组或空数组",
                {"phase_id": phase_id},
            )
        after: List[str] = []
        for item in raw_after:
            dependency = item.strip()
            if dependency not in after:
                after.append(dependency)
        phase = dict(raw)
        phase["id"] = phase_id
        phase["title"] = str(raw.get("title") or phase_id).strip() or phase_id
        phase["after"] = after
        indexes[phase_id] = index
        phases.append(phase)

    known = set(indexes)
    for phase in phases:
        missing = [dependency for dependency in phase["after"] if dependency not in known]
        if missing:
            raise DocumentPhaseError(
                "document_phase_dependency_missing",
                f"文档阶段 {phase['id']} 引用了不存在的依赖：{', '.join(missing)}",
                {"phase_id": phase["id"], "missing": missing},
            )

    by_id = {phase["id"]: phase for phase in phases}
    indegree = {phase["id"]: len(phase["after"]) for phase in phases}
    dependents: Dict[str, List[str]] = {phase["id"]: [] for phase in phases}
    for phase in phases:
        for dependency in phase["after"]:
            dependents[dependency].append(phase["id"])

    ready = sorted((phase_id for phase_id, degree in indegree.items() if degree == 0), key=indexes.__getitem__)
    ordered: List[Dict[str, Any]] = []
    while ready:
        phase_id = ready.pop(0)
        ordered.append(by_id[phase_id])
        for dependent in sorted(dependents[phase_id], key=indexes.__getitem__):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=indexes.__getitem__)

    if len(ordered) != len(phases):
        cyclic = [phase["id"] for phase in phases if indegree[phase["id"]] > 0]
        raise DocumentPhaseError(
            "document_phase_cycle",
            f"文档阶段依赖存在环：{', '.join(cyclic)}",
            {"phase_ids": cyclic},
        )
    return ordered
