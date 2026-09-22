from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopforge.config import ProjectProfile
from loopforge.commands import project_run_once, project_task_cleanup, project_task_merge, project_task_review
from loopforge.worktrees import WorktreePreparationError, prepare_task_worktrees


class WorktreeManagerTests(unittest.TestCase):
    def test_single_repo_worktree_is_prepared_before_worker_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._init_repo(root)
            profile = ProjectProfile(project_id="demo", name="Demo", root_dir=root)
            item = {"id": "task-1", "title": "隔离执行", "target_branch": "main"}

            first = prepare_task_worktrees(profile, item, "run-1")
            second = prepare_task_worktrees(profile, item, "run-2")

            target = first["target_plan"]["targets"][0]
            self.assertFalse(first["target_plan"]["explicit"])
            self.assertEqual(target["branch"], "feature/demo/task-1")
            self.assertTrue(Path(target["worktree_path"]).is_dir())
            self.assertEqual(first["execution_root"], target["worktree_path"])
            self.assertEqual(second["target_plan"]["targets"][0]["worktree_path"], target["worktree_path"])
            self.assertTrue(Path(first["ownership_markers"][0]).is_file())

    def test_unknown_explicit_target_is_blocked_before_git_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._init_repo(root)
            profile = ProjectProfile(project_id="demo", name="Demo", root_dir=root)

            with self.assertRaises(WorktreePreparationError) as raised:
                prepare_task_worktrees(
                    profile,
                    {"id": "task-1", "targets": [{"id": "unknown", "project": "unknown"}]},
                    "run-1",
                )

            self.assertEqual(raised.exception.code, "target_repo_missing")
            branches = self._git(root, "branch", "--format=%(refname:short)").stdout.splitlines()
            self.assertEqual(branches, ["main"])

    def test_prefilled_worktree_must_stay_in_loopforge_managed_root(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._init_repo(root)
            profile = ProjectProfile(project_id="demo", name="Demo", root_dir=root)

            with self.assertRaises(WorktreePreparationError) as raised:
                prepare_task_worktrees(
                    profile,
                    {"id": "task-1", "worktree_path": str(Path(tmp) / "arbitrary-worktree")},
                    "run-1",
                )

            self.assertEqual(raised.exception.code, "worktree_path_unmanaged")

    def test_multi_target_worktrees_follow_after_order_and_keep_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "ad"
            root.mkdir()
            api = root / "ad-api-go"
            admin = root / "ad-admin-react"
            self._init_repo(api)
            self._init_repo(admin)
            profile = ProjectProfile(
                project_id="ad",
                name="AD",
                root_dir=root,
                project_type="project-group",
                project_group={
                    "children": [
                        {"key": "ad-api-go", "path": str(api)},
                        {"key": "ad-admin-react", "path": str(admin)},
                    ]
                },
            )
            item = {
                "id": "task-1",
                "targets": [
                    {"id": "admin", "project": "ad-admin-react", "scope": "完成页面", "after": ["api"]},
                    {"id": "api", "project": "ad-api-go", "scope": "完成接口", "after": []},
                ],
            }

            prepared = prepare_task_worktrees(profile, item, "run-1")

            self.assertTrue(prepared["target_plan"]["explicit"])
            self.assertEqual(prepared["target_plan"]["ordered_target_ids"], ["api", "admin"])
            by_id = {target["id"]: target for target in prepared["target_plan"]["targets"]}
            self.assertEqual(by_id["admin"]["scope"], "完成页面")
            self.assertEqual(by_id["api"]["branch"], "feature/ad/task-1/api")
            self.assertEqual(by_id["admin"]["branch"], "feature/ad/task-1/admin")
            self.assertTrue(Path(by_id["api"]["worktree_path"]).is_dir())
            self.assertTrue(Path(by_id["admin"]["worktree_path"]).is_dir())


    def test_managed_single_repo_closes_review_merge_cleanup_loop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            self._init_repo(root)
            (root / "go.mod").write_text("module example.com/demo\n\ngo 1.23\n", encoding="utf-8")
            (root / "data").mkdir()
            (root / "data" / "dev-task.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "items": [
                            {
                                "id": "task-1",
                                "title": "完整闭环",
                                "description": "验证隔离执行到清理。",
                                "acceptance": ["变更合入 main"],
                                "status": "open",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self._git(root, "add", "go.mod", "data/dev-task.json")
            self._git(root, "commit", "-m", "add runtime fixture")
            config = Path(tmp) / "projects.json"
            config.write_text(
                json.dumps(
                    {
                        "projects": [
                            {
                                "id": "demo",
                                "name": "Demo",
                                "root_dir": str(root),
                                "project_type": "go-backend",
                                "planning_adapter": "builtin",
                                "executor": "codex_cli",
                                "notification_channel": "none",
                                "automation_mode": "off",
                                "worktree_managed": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            def fake_worker(profile, item, run_id):
                worktree = Path(item["execution_root"])
                (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
                planning = profile.loopforge_dir / "task" / "task-1"
                for filename in ("prd.md", "design.md", "implement.md"):
                    (planning / filename).write_text(f"# {filename}\n", encoding="utf-8")
                self._git(worktree, "add", ".")
                self._git(worktree, "commit", "-m", "implement task")
                return {
                    "status": "completed",
                    "exit_code": 0,
                    "summary": "worker 完成",
                    "worker_result": {
                        "status": "completed",
                        "recommended_status": "ready_for_review",
                        "summary": "等待验收",
                        "validation": {"status": "passed", "commands": ["go test ./..."]},
                        "acceptance_results": [
                            {"index": 0, "status": "passed", "evidence": ["go test ./... 通过"]}
                        ],
                    },
                }

            with patch("loopforge.commands.run_executor", fake_worker):
                developed = project_run_once(config, "demo")
            self.assertEqual(developed["task"]["status"], "ready_for_review", developed)
            project_task_review(config, "demo", "task-1", "accept", "通过")
            merged = project_task_merge(config, "demo", "task-1")
            self.assertEqual(merged["status"], "completed", merged)
            worktree_path = Path(developed["task"]["worktree_path"])
            cleaned = project_task_cleanup(config, "demo", "task-1")

            self.assertEqual(cleaned["task"]["status"], "completed", cleaned)
            self.assertTrue((root / "feature.txt").is_file())
            self.assertFalse(worktree_path.exists())

    def _init_repo(self, root: Path) -> None:
        root.mkdir()
        self._git(root, "init", "-b", "main")
        self._git(root, "config", "user.email", "loopforge@example.invalid")
        self._git(root, "config", "user.name", "LoopForge Test")
        (root / "README.md").write_text("# Demo\n", encoding="utf-8")
        self._git(root, "add", "README.md")
        self._git(root, "commit", "-m", "init")

    def _git(self, root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
