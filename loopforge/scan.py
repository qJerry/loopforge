"""项目巡检信号收集与游标状态。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .config import ProjectProfile
from .dev_tasks import uses_v2_storage


_WHEN_THEN_HEADER = re.compile(r"^\|\s*When\s*\|\s*Then\s*\|", re.IGNORECASE)
_TABLE_SEPARATOR = re.compile(r"^\|[\s:\-|]+\|$")


def specs_when_then_rule_count(text: str) -> int:
    """统计 Specs 文档中 When/Then 表格的有效场景规则行数。"""

    count = 0
    in_table = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if _WHEN_THEN_HEADER.match(line):
            in_table = True
            continue
        if not in_table:
            continue
        if not line.startswith("|"):
            in_table = False
            continue
        if _TABLE_SEPARATOR.match(line):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) >= 2 and cells[0] and cells[1]:
            count += 1
    return count


DEFAULT_DAILY_SCAN_SECONDS = 15 * 60
DEFAULT_INITIAL_SCAN_SECONDS = 45 * 60
DEFAULT_SCAN_FILE_BUDGET = 200
SCAN_STATE_FILENAME = "scan-state.json"
CODE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".jsx",
    ".kt",
    ".php",
    ".py",
    ".rb",
    ".rs",
    ".scss",
    ".sql",
    ".swift",
    ".ts",
    ".tsx",
}
IGNORED_PARTS = {
    ".git",
    ".loopforge",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
    "venv",
    ".venv",
}
REQUIRED_FRONT_MATTER_FIELDS = {"id", "status", "owner", "version", "updated_at"}
MODULE_DOC_FILES = {
    "requirements": "requirements.md",
    "design": "design.md",
    "specs": "specs.md",
}


@dataclass(frozen=True)
class ScanResult:
    signals: List[Dict[str, Any]]
    state: Dict[str, Any]
    base_ref: str
    head_ref: str
    scope: str
    scan_status: str
    budget_seconds: int
    file_budget: int
    files_scanned: int
    files_total: int


def scan_state_path(profile: ProjectProfile) -> Path:
    return profile.loopforge_dir / SCAN_STATE_FILENAME


def read_scan_state(profile: ProjectProfile) -> Dict[str, Any]:
    path = scan_state_path(profile)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def write_scan_state(profile: ProjectProfile, payload: Dict[str, Any]) -> Dict[str, Any]:
    path = scan_state_path(profile)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def collect_scan_signals(profile: ProjectProfile, run_id: str) -> ScanResult:
    state = read_scan_state(profile)
    active_scan = state.get("active_scan") if isinstance(state.get("active_scan"), dict) else None
    current_head = _current_head(profile.root_dir)
    if active_scan and active_scan.get("status") == "incomplete":
        scope = "resume"
        base_ref = str(active_scan.get("base_ref") or "")
        head_ref = str(active_scan.get("head_ref") or current_head)
        cursor = active_scan.get("cursor") if isinstance(active_scan.get("cursor"), dict) else {}
        start_offset = int(cursor.get("file_offset") or 0)
        budget_seconds = int(active_scan.get("budget_seconds") or DEFAULT_DAILY_SCAN_SECONDS)
    else:
        start_offset = 0
        if "last_completed_scan_ref" in state:
            scope = "daily"
            base_ref = str(state.get("last_completed_scan_ref") or "")
            budget_seconds = DEFAULT_DAILY_SCAN_SECONDS
        else:
            scope = "initial"
            base_ref = ""
            budget_seconds = DEFAULT_INITIAL_SCAN_SECONDS
        head_ref = current_head

    docs_context = _docs_context(profile)
    structural_signals = _structural_doc_signals(profile, docs_context) if start_offset == 0 else []
    diff_files = _git_diff_files(profile.root_dir, base_ref, head_ref) if scope != "initial" else []
    work_items = _scan_work_items(profile, docs_context, diff_files)
    file_budget = _scan_file_budget()
    end_offset = min(len(work_items), start_offset + file_budget)
    signals = list(structural_signals)
    for item in work_items[start_offset:end_offset]:
        signals.extend(_signals_for_work_item(profile, item, docs_context))

    files_scanned = max(0, end_offset - start_offset)
    if end_offset < len(work_items):
        state["active_scan"] = {
            "scan_id": run_id,
            "base_ref": base_ref,
            "head_ref": head_ref,
            "phase": "collect_signals",
            "cursor": {"file_offset": end_offset, "clue_offset": 0},
            "status": "incomplete",
            "budget_seconds": budget_seconds,
        }
        scan_status = "incomplete"
    else:
        state["last_completed_scan_ref"] = head_ref
        state["active_scan"] = None
        scan_status = "completed"
    state["last_scan_summary"] = {
        "scan_id": run_id,
        "scope": scope,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "status": scan_status,
        "files_scanned": files_scanned,
        "files_total": len(work_items),
    }
    write_scan_state(profile, state)
    return ScanResult(
        signals=signals,
        state=state,
        base_ref=base_ref,
        head_ref=head_ref,
        scope=scope,
        scan_status=scan_status,
        budget_seconds=budget_seconds,
        file_budget=file_budget,
        files_scanned=files_scanned,
        files_total=len(work_items),
    )


def _docs_context(profile: ProjectProfile) -> Dict[str, Any]:
    root = profile.root_dir
    docs_dir = root / "docs"
    modules = _doc_modules(docs_dir)
    requirements_files = _module_doc_files(modules, "requirements")
    design_files = _module_doc_files(modules, "design")
    specs_files = _module_doc_files(modules, "specs")
    spec_refs = _collect_spec_references(profile, specs_files)
    doc_text_index = _doc_text_index(profile, requirements_files + design_files + specs_files)
    return {
        "docs_dir": docs_dir,
        "modules": modules,
        "requirements_files": requirements_files,
        "design_files": design_files,
        "specs_files": specs_files,
        "spec_refs": spec_refs,
        "doc_text_index": doc_text_index,
    }


def _structural_doc_signals(profile: ProjectProfile, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    docs_dir: Path = context["docs_dir"]
    signals: List[Dict[str, Any]] = []
    if not docs_dir.exists():
        signals.append(
            _signal(
                "docs_dir_missing",
                "medium",
                "doc",
                "docs",
                "项目缺少 docs 目录，模块三件套文档入口不存在。",
                ["docs"],
            )
        )
        return signals

    modules: Dict[str, Dict[str, Path]] = context["modules"]
    if not modules:
        signals.append(
            _signal(
                "docs_modules_missing",
                "medium",
                "doc",
                "docs",
                "docs 目录存在，但没有发现任何模块三件套文档。",
                ["docs"],
            )
        )
        return signals
    signals.extend(_module_index_signals(profile, docs_dir, modules))
    for module_key, docs in sorted(modules.items()):
        signals.extend(_module_missing_signals(profile, docs_dir, module_key, docs))
    return signals


def _scan_work_items(profile: ProjectProfile, context: Dict[str, Any], diff_files: List[str]) -> List[Dict[str, str]]:
    items: List[Dict[str, str]] = []
    for path in context["requirements_files"]:
        items.append({"kind": "requirements_doc", "path": _relative_path(profile, path)})
    for path in context["design_files"]:
        items.append({"kind": "design_doc", "path": _relative_path(profile, path)})
    for path in context["specs_files"]:
        items.append({"kind": "specs_doc", "path": _relative_path(profile, path)})
    for rel in diff_files:
        if _is_code_path(rel):
            items.append({"kind": "diff_code", "path": rel})
    return items


def _signals_for_work_item(profile: ProjectProfile, item: Dict[str, str], context: Dict[str, Any]) -> List[Dict[str, Any]]:
    kind = item["kind"]
    rel = item["path"]
    path = profile.root_dir / rel
    if kind in {"requirements_doc", "design_doc"}:
        return [] if uses_v2_storage(profile) else _doc_front_matter_signals(profile, path, rel)
    if kind == "specs_doc":
        front_matter_signals = [] if uses_v2_storage(profile) else _doc_front_matter_signals(profile, path, rel)
        return front_matter_signals + _specs_doc_signals(profile, path, rel)
    if kind == "diff_code":
        return _diff_code_signals(profile, path, rel, context)
    return []


def _doc_front_matter_signals(profile: ProjectProfile, path: Path, rel: str) -> List[Dict[str, Any]]:
    text = _read_text(path, 20000)
    fields = _front_matter_fields(text)
    if not fields:
        return [
            _signal(
                "doc_front_matter_missing",
                "low",
                "doc",
                rel,
                f"文档 {rel} 缺少 front matter，无法稳定路由 owner、版本和状态。",
                [rel],
            )
        ]
    missing = sorted(REQUIRED_FRONT_MATTER_FIELDS - fields)
    if not missing:
        return []
    return [
        _signal(
            "doc_front_matter_missing_fields",
            "low",
            "doc",
            rel,
            f"文档 {rel} front matter 缺少字段：{', '.join(missing)}。",
            [rel],
        )
    ]


def _specs_doc_signals(profile: ProjectProfile, path: Path, rel: str) -> List[Dict[str, Any]]:
    text = _read_text(path, 40000)
    signals: List[Dict[str, Any]] = []
    if not specs_when_then_rule_count(text):
        signals.append(
            _signal(
                "spec_missing_when_then",
                "high",
                "doc",
                rel,
                f"Specs 文档 {rel} 缺少 When/Then 场景规则。",
                [rel],
            )
        )
    code_refs = _section_refs(text, "代码引用")
    test_refs = _section_refs(text, "验证引用")
    if not code_refs:
        signals.append(
            _signal(
                "spec_missing_code_reference",
                "medium",
                "doc",
                rel,
                f"Specs 文档 {rel} 缺少代码引用，无法做 specs -> code 对齐。",
                [rel],
            )
        )
    if not test_refs:
        signals.append(
            _signal(
                "spec_missing_test_reference",
                "medium",
                "doc",
                rel,
                f"Specs 文档 {rel} 缺少验证引用，无法确认规则是否被测试覆盖。",
                [rel],
            )
        )
    for ref in code_refs:
        if not (profile.root_dir / ref).exists():
            signals.append(
                _signal(
                    "spec_code_reference_missing",
                    "medium",
                    "doc",
                    f"{rel}#{ref}",
                    f"Specs 文档 {rel} 引用的代码文件不存在：{ref}。",
                    [rel, ref],
                )
            )
    for ref in test_refs:
        if not (profile.root_dir / ref).exists():
            signals.append(
                _signal(
                    "spec_test_reference_missing",
                    "medium",
                    "doc",
                    f"{rel}#{ref}",
                    f"Specs 文档 {rel} 引用的验证文件不存在：{ref}。",
                    [rel, ref],
                )
            )
    return signals


def _diff_code_signals(profile: ProjectProfile, path: Path, rel: str, context: Dict[str, Any]) -> List[Dict[str, Any]]:
    signals: List[Dict[str, Any]] = []
    text = _read_text(path, 60000)
    for line_no, line in _todo_lines(text):
        signals.append(
            _signal(
                "code_todo_marker",
                "low",
                "code",
                f"{rel}:{line_no}",
                f"代码 {rel}:{line_no} 包含 TODO/FIXME/XXX，可能需要任务化。",
                [f"{rel}:{line_no}", line.strip()[:180]],
            )
        )
    if not _code_path_has_doc_reference(rel, context):
        signals.append(
            _signal(
                "code_without_docs",
                "medium",
                "code",
                rel,
                f"代码文件 {rel} 在最近变更中出现，但没有找到对应 requirements/design/specs 引用。",
                [rel],
            )
        )
    return signals


def _module_index_signals(profile: ProjectProfile, docs_dir: Path, modules: Dict[str, Dict[str, Path]]) -> List[Dict[str, Any]]:
    if not modules:
        return []
    readme = docs_dir / "README.md"
    readme_rel = _relative_path(profile, readme)
    if not readme.exists():
        return [
            _signal(
                "docs_index_missing",
                "low",
                "doc",
                readme_rel,
                "docs/README.md 不存在，模块三件套文档缺少总索引。",
                [readme_rel],
            )
        ]
    text = _read_text(readme, 40000)
    signals: List[Dict[str, Any]] = []
    for module_key, docs in sorted(modules.items()):
        module_doc = docs.get("requirements") or docs.get("design") or docs.get("specs")
        evidence = _relative_path(profile, module_doc) if module_doc else f"docs/{module_key}"
        if module_key not in text and evidence not in text:
            signals.append(
                _signal(
                    "docs_index_missing_module",
                    "low",
                    "doc",
                    f"{readme_rel}#{module_key}",
                    f"docs/README.md 没有索引模块 {module_key}。",
                    [readme_rel, evidence],
                )
            )
    return signals


def _module_missing_signals(profile: ProjectProfile, docs_dir: Path, module_key: str, docs: Dict[str, Path]) -> List[Dict[str, Any]]:
    labels = {
        "requirements": ("requirements_missing", "requirements.md", "需求目标和验收入口"),
        "design": ("design_missing", "design.md", "流程、状态、职责、数据流和权限流入口"),
        "specs": ("specs_missing", "specs.md", "When/Then 规格入口"),
    }
    signals: List[Dict[str, Any]] = []
    for kind, (clue_type, filename, purpose) in labels.items():
        if kind in docs:
            continue
        expected = docs_dir / module_key / filename
        expected_rel = _relative_path(profile, expected)
        signals.append(
            _signal(
                clue_type,
                "medium",
                "doc",
                expected_rel,
                f"模块 {module_key} 缺少 {filename}，{purpose}不存在。",
                [expected_rel],
            )
        )
    return signals


def _collect_spec_references(profile: ProjectProfile, specs_files: List[Path]) -> Dict[str, Set[str]]:
    refs = {"code": set(), "test": set()}
    for path in specs_files:
        text = _read_text(path, 40000)
        refs["code"].update(_section_refs(text, "代码引用"))
        refs["test"].update(_section_refs(text, "验证引用"))
    return refs


def _doc_text_index(profile: ProjectProfile, docs: Iterable[Path]) -> str:
    chunks: List[str] = []
    for path in docs:
        chunks.append(_relative_path(profile, path))
        chunks.append(_read_text(path, 20000))
    return "\n".join(chunks)


def _code_path_has_doc_reference(rel: str, context: Dict[str, Any]) -> bool:
    spec_refs: Dict[str, Set[str]] = context["spec_refs"]
    if rel in spec_refs["code"] or rel in spec_refs["test"]:
        return True
    basename = Path(rel).name
    doc_text = context["doc_text_index"]
    return rel in doc_text or basename in doc_text


def _front_matter_fields(text: str) -> Set[str]:
    if not text.startswith("---"):
        return set()
    end = text.find("\n---", 3)
    if end < 0:
        return set()
    fields = set()
    for line in text[3:end].splitlines():
        if ":" not in line:
            continue
        key = line.split(":", 1)[0].strip()
        if key:
            fields.add(key)
    return fields


def _section_refs(text: str, title: str) -> Set[str]:
    match = re.search(rf"^##\s+{re.escape(title)}\s*$", text, flags=re.MULTILINE)
    if not match:
        return set()
    tail = text[match.end() :]
    next_section = re.search(r"^##\s+", tail, flags=re.MULTILINE)
    section = tail[: next_section.start()] if next_section else tail
    refs: Set[str] = set()
    for raw_line in section.splitlines():
        line = raw_line.strip()
        if not line.startswith(("-", "*")):
            continue
        value = line[1:].strip().strip("`")
        if value:
            refs.add(value)
    return refs


def _todo_lines(text: str) -> Iterable[Tuple[int, str]]:
    for index, line in enumerate(text.splitlines(), start=1):
        if re.search(r"\b(TODO|FIXME|XXX)\b", line):
            yield index, line


def _doc_modules(docs_dir: Path) -> Dict[str, Dict[str, Path]]:
    modules: Dict[str, Dict[str, Path]] = {}
    if not docs_dir.exists():
        return modules
    filename_to_kind = {filename: kind for kind, filename in MODULE_DOC_FILES.items()}
    for path in sorted(docs_dir.rglob("*.md")):
        if not path.is_file() or _is_ignored_path(path):
            continue
        kind = filename_to_kind.get(path.name.lower())
        if not kind:
            continue
        try:
            module_key = path.parent.relative_to(docs_dir).as_posix()
        except ValueError:
            continue
        if module_key in {"", "."}:
            continue
        modules.setdefault(module_key, {})[kind] = path
    return modules


def _module_doc_files(modules: Dict[str, Dict[str, Path]], kind: str) -> List[Path]:
    files: List[Path] = []
    for docs in modules.values():
        path = docs.get(kind)
        if path is not None:
            files.append(path)
    return sorted(files)


def _relative_path(profile: ProjectProfile, path: Path) -> str:
    try:
        return path.resolve().relative_to(profile.root_dir.resolve()).as_posix()
    except ValueError:
        return str(path)


def _read_text(path: Path, limit: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def _is_code_path(rel: str) -> bool:
    path = Path(rel)
    parts = set(path.parts)
    if parts & IGNORED_PARTS:
        return False
    if rel.startswith("docs/") or rel.startswith("data/") or "/test" in rel.lower() or path.name.startswith("."):
        return False
    return path.suffix.lower() in CODE_EXTENSIONS


def _is_ignored_path(path: Path) -> bool:
    return bool(set(path.parts) & IGNORED_PARTS)


def _current_head(root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _git_diff_files(root: Path, base_ref: str, head_ref: str) -> List[str]:
    if not base_ref or not head_ref or base_ref == head_ref:
        return []
    proc = subprocess.run(
        ["git", "-C", str(root), "diff", "--name-only", f"{base_ref}..{head_ref}"],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _scan_file_budget() -> int:
    raw = os.environ.get("LOOPFORGE_SCAN_FILE_BUDGET")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            return DEFAULT_SCAN_FILE_BUDGET
    return DEFAULT_SCAN_FILE_BUDGET


def _signal(
    clue_type: str,
    severity: str,
    subject_kind: str,
    subject_id: str,
    summary: str,
    evidence: List[str],
) -> Dict[str, Any]:
    return {
        "type": clue_type,
        "severity": severity,
        "subject": {"kind": subject_kind, "id": subject_id},
        "summary": summary,
        "evidence": evidence,
    }
