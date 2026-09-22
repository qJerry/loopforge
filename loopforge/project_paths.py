"""项目内路径解析与越界保护。"""

from __future__ import annotations

from pathlib import Path


class ProjectPathError(ValueError):
    def __init__(self, value: str, root: Path) -> None:
        super().__init__(f"路径必须位于项目目录内：{value}")
        self.value = value
        self.root = root


def resolve_project_path(root: Path, value: str) -> Path:
    """解析项目内路径，同时拦截绝对路径、`..` 与符号链接逃逸。"""

    clean = str(value or "").strip()
    candidate = Path(clean).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved_root = root.resolve()
    resolved = candidate.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ProjectPathError(clean, resolved_root)
    return resolved
