"""LoopForge 受管特性分支命名合同。"""

from __future__ import annotations

import re
import unicodedata


MANAGED_FEATURE_BRANCH_PREFIX = "feature/"
LEGACY_MANAGED_FEATURE_BRANCH_PREFIX = "loopforge/"


def managed_feature_branch(project_id: str, task_id: str, target_id: str = "") -> str:
    """生成新的受管特性分支名称。"""

    parts = ["feature", slug_branch_segment(project_id), slug_branch_segment(task_id)]
    if target_id:
        parts.append(slug_branch_segment(target_id))
    return "/".join(parts)


def readable_feature_branch(managed_name: str, target_id: str = "") -> str:
    """使用冻结的可读任务名生成受管特性分支。"""

    parts = ["feature", slug_branch_segment(managed_name)]
    if target_id:
        parts.append(slug_branch_segment(target_id))
    return "/".join(parts)


def readable_task_name(title: str, date_prefix: str) -> str:
    """生成适合分支和 worktree 的日期加任务短名。"""

    normalized = unicodedata.normalize("NFKC", str(title or "")).strip().lower()
    chars: list[str] = []
    separated = False
    for char in normalized:
        if char.isalnum():
            chars.append(char)
            separated = False
        elif chars and not separated:
            chars.append("-")
            separated = True
    slug = "".join(chars).strip("-")[:48].rstrip("-") or "task"
    return f"{date_prefix}-{slug}"


def is_managed_feature_branch(branch: str) -> bool:
    """识别当前和历史受管特性分支。"""

    return branch.startswith((MANAGED_FEATURE_BRANCH_PREFIX, LEGACY_MANAGED_FEATURE_BRANCH_PREFIX))


def is_legacy_managed_feature_branch(branch: str) -> bool:
    """识别需要显式迁移的旧前缀分支。"""

    return branch.startswith(LEGACY_MANAGED_FEATURE_BRANCH_PREFIX)


def slug_branch_segment(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(value)).strip().lower()
    clean = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE).strip("-.")
    return clean or "task"
