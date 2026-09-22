"""单任务 JSON 与 Git 发布完整生命周期 E2E。"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from loopforge.config import ProjectProfile
from loopforge.dev_tasks import choose_item, load_dev_tasks, update_item
from loopforge.task_actions import abandon_task
from loopforge.task_publisher import publish_new_task, reserve_task
from loopforge.task_store import load_history_tasks, load_task


class TaskGitLifecycleE2ETests(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

    def _setup(self, base: Path) -> tuple[Path, ProjectProfile]:
        origin = base / "origin.git"
        repo = base / "main"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        self._git(repo, "config", "user.email", "loopforge@example.test")
        self._git(repo, "config", "user.name", "LoopForge Test")
        (repo / ".gitignore").write_text(".loopforge/\n", encoding="utf-8")
        (repo / "data" / "tasks").mkdir(parents=True)
        (repo / "data" / "tasks" / ".gitkeep").write_text("", encoding="utf-8")
        self._git(repo, "add", ".")
        self._git(repo, "commit", "-q", "-m", "initial")
        self._git(repo, "remote", "add", "origin", str(origin))
        self._git(repo, "push", "-q", "-u", "origin", "main")
        subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        return repo, ProjectProfile(project_id="demo", name="Demo", root_dir=repo, worktree_managed=True)

    def test_task_lifecycle_publishes_feature_before_status_and_archives(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, profile = self._setup(Path(tmp))
            reservation = reserve_task(profile, {"title": "完整生命周期", "source": "manual"})
            published = publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "完整生命周期",
                    "description": "验证单任务 Git 发布闭环。",
                    "acceptance": ["最终进入历史"],
                    "source": "manual",
                    "planning_level": "complex",
                    "docs": {},
                    "targets": [],
                },
            )
            task_id = published["task"]["id"]

            claimed = update_item(profile, task_id, status="claimed", agent_status="running")
            self.assertEqual(claimed["version"], 2)
            worktree = Path(reservation.bindings[0]["worktree_path"])
            (worktree / "feature.txt").write_text("done\n", encoding="utf-8")
            self._git(worktree, "add", "feature.txt")
            self._git(worktree, "commit", "-q", "-m", "implement")
            feature_revision = self._git(worktree, "rev-parse", "HEAD").stdout.strip()

            review = update_item(profile, task_id, status="ready_for_review", agent_status="completed")
            self.assertEqual(review["branch_revision"], feature_revision)
            remote_feature = self._git(repo, "ls-remote", "origin", f"refs/heads/{review['feature_branch']}").stdout.split()[0]
            self.assertEqual(remote_feature, feature_revision)
            update_item(profile, task_id, status="accepted", accepted_at="2026-08-27T10:00:00+00:00")
            update_item(profile, task_id, status="merged", merged_at="2026-08-27T10:01:00+00:00")
            completed = update_item(profile, task_id, status="completed", completed_at="2026-08-27T10:02:00+00:00")

            self.assertEqual(completed["status"], "completed")
            with self.assertRaisesRegex(Exception, "历史任务不可修改"):
                load_task(profile, task_id)
            history = load_history_tasks(profile, limit=20)
            self.assertEqual([item["id"] for item in history["items"]], [task_id])
            self.assertEqual(history["issues"], [])
            self.assertEqual(self._git(repo, "rev-parse", "HEAD").stdout.strip(), self._git(repo, "rev-parse", "origin/main").stdout.strip())

    def test_abandon_stops_scheduling_then_requires_discard_before_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, profile = self._setup(Path(tmp))
            reservation = reserve_task(profile, {"title": "放弃任务", "source": "manual"})
            published = publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "放弃任务",
                    "description": "验证两段式清理。",
                    "acceptance": ["本地资源清理后归档"],
                    "source": "manual",
                    "planning_level": "complex",
                    "docs": {},
                    "targets": [],
                },
            )
            task_id = published["task"]["id"]
            binding = reservation.bindings[0]
            worktree = Path(binding["worktree_path"])
            marker = Path(binding["ownership_marker"])
            (worktree / "draft.md").write_text("未提交\n", encoding="utf-8")
            profile = replace(profile, worktree_managed=False)

            blocked = abandon_task(profile, task_id, "不再实施")

            self.assertEqual(blocked["status"], "failed", blocked)
            pending = load_task(profile, task_id)
            self.assertEqual(pending["status"], "open")
            self.assertEqual(pending["cleanup_pending"], "abandon")
            self.assertIsNone(choose_item(load_dev_tasks(profile)))
            self.assertTrue(worktree.exists())

            abandoned = abandon_task(profile, task_id, discard_changes=True)

            self.assertEqual(abandoned["status"], "completed", abandoned)
            self.assertFalse(worktree.exists())
            self.assertFalse(marker.exists())
            self.assertEqual(self._git(repo, "branch", "--list", binding["feature_branch"]).stdout.strip(), "")
            self.assertTrue(self._git(repo, "ls-remote", "origin", f"refs/heads/{binding['feature_branch']}").stdout.strip())
            history = load_history_tasks(profile, limit=20)
            archived = next(item for item in history["items"] if item["id"] == task_id)
            self.assertEqual(archived["status"], "abandoned")
            self.assertEqual(archived["abandoned_reason"], "不再实施")
            self.assertTrue(archived["discard_confirmed"])


if __name__ == "__main__":
    unittest.main()
