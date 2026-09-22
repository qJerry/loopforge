"""LoopForge Git publisher 真实临时仓库测试。"""

from __future__ import annotations

import json
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from loopforge.config import ProjectProfile
from loopforge.task_git import (
    TaskGitError,
    clear_sync_blocker,
    publish_main_change,
    publish_task_mutation_from_isolated_main,
    push_managed_feature_revision,
    read_sync_blocker,
)
from loopforge.task_store import TaskFileChange


class TaskGitTests(unittest.TestCase):
    def _git(self, repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            capture_output=True,
            text=True,
        )

    def _setup_repo(self, base: Path) -> tuple[Path, Path, ProjectProfile]:
        origin = base / "origin.git"
        repo = base / "main"
        subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
        self._git(repo, "config", "user.email", "loopforge@example.test")
        self._git(repo, "config", "user.name", "LoopForge Test")
        (repo / ".gitignore").write_text(".loopforge/\n", encoding="utf-8")
        self._git(repo, "add", ".gitignore")
        self._git(repo, "commit", "-q", "-m", "initial")
        self._git(repo, "remote", "add", "origin", str(origin))
        self._git(repo, "push", "-q", "-u", "origin", "main")
        subprocess.run(["git", "-C", str(origin), "symbolic-ref", "HEAD", "refs/heads/main"], check=True)
        return origin, repo, ProjectProfile(project_id="demo", name="Demo", root_dir=repo)

    def _clone_peer(self, base: Path, origin: Path, name: str = "peer") -> Path:
        peer = base / name
        subprocess.run(["git", "clone", "-q", str(origin), str(peer)], check=True)
        self._git(peer, "config", "user.email", "peer@example.test")
        self._git(peer, "config", "user.name", "Peer")
        return peer

    def _change(self, repo: Path, task_id: str = "task-1", content: str = "{}\n") -> TaskFileChange:
        relative = Path("data/tasks") / f"{task_id}.json"
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return TaskFileChange(task_id, "create", (relative,), None, {"id": task_id})

    def test_publish_commits_and_pushes_only_declared_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            change = self._change(repo)

            result = publish_main_change(profile, change, "loopforge: 创建 task-1")

            self.assertEqual(result.status, "published")
            self.assertFalse(result.replayed)
            self.assertEqual(self._git(repo, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD").stdout.splitlines(), ["data/tasks/task-1.json"])
            self.assertEqual(self._git(repo, "rev-parse", "HEAD").stdout.strip(), self._git(repo, "rev-parse", "origin/main").stdout.strip())

    def test_publish_rejects_dirty_tracked_path_and_non_main_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            change = self._change(repo)
            (repo / ".gitignore").write_text(".loopforge/\n用户改动\n", encoding="utf-8")
            with self.assertRaisesRegex(TaskGitError, "无关已跟踪路径"):
                publish_main_change(profile, change, "loopforge: 创建 task-1")
            self.assertEqual(self._git(repo, "log", "-1", "--pretty=%s").stdout.strip(), "initial")

            self._git(repo, "restore", ".gitignore")
            self._git(repo, "switch", "-q", "-c", "feature/manual")
            with self.assertRaisesRegex(TaskGitError, "main"):
                publish_main_change(profile, change, "loopforge: 创建 task-1")

    def test_managed_feature_revision_is_pushed_before_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            branch = "feature/demo/task-1"
            self._git(repo, "switch", "-q", "-c", branch)
            (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
            self._git(repo, "add", "feature.txt")
            self._git(repo, "commit", "-q", "-m", "feature")
            revision = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            self._git(repo, "switch", "-q", "main")

            result = push_managed_feature_revision(
                profile,
                {"remote": "origin", "feature_branch": branch, "repo_path": str(repo)},
                revision,
            )

            self.assertEqual(result.status, "pushed")
            remote = self._git(repo, "ls-remote", "origin", f"refs/heads/{branch}").stdout.split()[0]
            self.assertEqual(remote, revision)
            with self.assertRaisesRegex(TaskGitError, "受管"):
                push_managed_feature_revision(
                    profile,
                    {"remote": "origin", "feature_branch": "manual/task-1", "repo_path": str(repo)},
                    revision,
                )

    def test_different_remote_path_is_replayed_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, repo, profile = self._setup_repo(base)
            peer = self._clone_peer(base, origin)
            (peer / "peer.txt").write_text("remote\n", encoding="utf-8")
            self._git(peer, "add", "peer.txt")
            self._git(peer, "commit", "-q", "-m", "peer")
            self._git(peer, "push", "-q", "origin", "main")
            change = self._change(repo)

            result = publish_main_change(profile, change, "loopforge: 创建 task-1")

            self.assertEqual(result.status, "published")
            self.assertTrue(result.replayed)
            self.assertTrue((repo / "peer.txt").exists())
            self.assertEqual(self._git(repo, "rev-parse", "HEAD").stdout.strip(), self._git(repo, "rev-parse", "origin/main").stdout.strip())

    def test_same_task_path_race_writes_sync_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, repo, profile = self._setup_repo(base)
            peer = self._clone_peer(base, origin)
            remote_path = peer / "data" / "tasks" / "task-1.json"
            remote_path.parent.mkdir(parents=True)
            remote_path.write_text('{"owner":"peer"}\n', encoding="utf-8")
            self._git(peer, "add", "data/tasks/task-1.json")
            self._git(peer, "commit", "-q", "-m", "peer task")
            self._git(peer, "push", "-q", "origin", "main")
            change = self._change(repo, content='{"owner":"loopforge"}\n')

            result = publish_main_change(profile, change, "loopforge: 创建 task-1")

            self.assertEqual(result.status, "sync_blocked")
            self.assertFalse(result.replayed)
            blocker = read_sync_blocker(profile, "task-1")
            self.assertEqual(blocker["task_id"], "task-1")
            self.assertEqual(blocker["conflict_paths"], ["data/tasks/task-1.json"])
            self.assertTrue(blocker["local_revision"])
            self.assertTrue(blocker["remote_revision"])
            cleared = clear_sync_blocker(profile, "task-1", resolved_by="tester", resolution="人工合并并确认")
            self.assertEqual(cleared["status"], "cleared")
            self.assertIsNone(read_sync_blocker(profile, "task-1"))

    def test_result_recovery_can_publish_only_through_matching_task_sync_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            task = {
                "schema_version": 2,
                "id": "task-1",
                "title": "任务 task-1",
                "description": "验证结果恢复发布。",
                "status": "spec_ready",
                "priority": "P2",
                "version": 1,
                "created_at": "2026-09-09T00:00:00+00:00",
                "updated_at": "2026-09-09T00:00:00+00:00",
                "acceptance": ["结果可恢复"],
                "acceptance_refs": [],
                "git": {
                    "base_revision": "a" * 40,
                    "feature_branch": "feature/demo/task-1",
                    "branch_revision": "b" * 40,
                },
                "docs": {},
                "targets": [],
            }
            initial = self._change(repo, content=json.dumps(task, ensure_ascii=False) + "\n")
            publish_main_change(profile, initial, "loopforge: 创建 task-1")
            blocker_path = profile.loopforge_dir / "sync-blocked" / "task-1.json"
            blocker_path.parent.mkdir(parents=True)
            blocker_path.write_text(
                json.dumps({"status": "sync_blocked", "task_id": "task-1", "created_at": "2026-09-09T00:00:00+00:00"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(TaskGitError, "同步阻塞"):
                publish_task_mutation_from_isolated_main(
                    profile,
                    "task-1",
                    1,
                    {"spec_ready"},
                    {"status": "ready_for_review"},
                    "loopforge: 恢复 task-1",
                )

            published, change = publish_task_mutation_from_isolated_main(
                profile,
                "task-1",
                1,
                {"spec_ready"},
                {"status": "ready_for_review"},
                "loopforge: 恢复 task-1",
                permit_sync_blocker_task_id="task-1",
            )

            self.assertEqual(published.status, "published")
            self.assertEqual(change.after["status"], "ready_for_review")
            self.assertEqual(change.after["version"], 2)
            self.assertIsNotNone(read_sync_blocker(profile, "task-1"))

    def test_remote_delete_of_same_task_path_is_not_auto_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, repo, profile = self._setup_repo(base)
            initial = self._change(repo, content='{"version":1}\n')
            publish_main_change(profile, initial, "loopforge: 创建 task-1")
            peer = self._clone_peer(base, origin)
            (peer / "data" / "tasks" / "task-1.json").unlink()
            self._git(peer, "add", "data/tasks/task-1.json")
            self._git(peer, "commit", "-q", "-m", "peer delete")
            self._git(peer, "push", "-q", "origin", "main")

            task_path = repo / "data" / "tasks" / "task-1.json"
            task_path.write_text('{"version":2}\n', encoding="utf-8")
            change = TaskFileChange(
                "task-1",
                "mutate",
                (Path("data/tasks/task-1.json"),),
                {"version": 1},
                {"version": 2},
            )
            result = publish_main_change(profile, change, "loopforge: 更新 task-1")

            self.assertEqual(result.status, "sync_blocked")
            self.assertEqual(result.blocker["reason"], "path_conflict")
            self.assertEqual(result.blocker["conflict_paths"], ["data/tasks/task-1.json"])

    def test_second_push_failure_stops_after_one_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            origin, repo, profile = self._setup_repo(base)
            peer = self._clone_peer(base, origin)
            (peer / "peer.txt").write_text("remote\n", encoding="utf-8")
            self._git(peer, "add", "peer.txt")
            self._git(peer, "commit", "-q", "-m", "peer")
            self._git(peer, "push", "-q", "origin", "main")
            hook = origin / "hooks" / "pre-receive"
            hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)
            change = self._change(repo)

            result = publish_main_change(profile, change, "loopforge: 创建 task-1")

            self.assertEqual(result.status, "sync_blocked")
            self.assertTrue(result.replayed)
            self.assertEqual(read_sync_blocker(profile, "task-1")["reason"], "second_push_failed")


if __name__ == "__main__":
    unittest.main()
