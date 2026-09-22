"""任务预留与首次发布合同测试。"""

from __future__ import annotations

import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from loopforge import task_publisher
from loopforge.cli import build_parser
from loopforge.config import ProjectProfile
from loopforge.dev_tasks import update_item
from loopforge.task_actions import cleanup_task, merge_task
from loopforge.task_publisher import (
    TaskPublisherError,
    cancel_reservation,
    load_reservation,
    publish_new_task,
    reservation_marker_for,
    reserve_task,
)
from loopforge.task_store import load_task


class TaskPublisherTests(unittest.TestCase):
    def _git(self, repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)

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

    def test_reservation_allocates_stable_id_branch_and_worktree_without_backlog_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            with (
                patch("loopforge.task_publisher._local_date_prefix", return_value="0909"),
                patch("loopforge.task_publisher.uuid.uuid4") as make_uuid,
            ):
                make_uuid.return_value.hex = "1234567890abcdef"
                reservation = reserve_task(profile, {"title": "新能力", "source": "manual"})

            self.assertEqual(reservation.task_id, "task-1234567890")
            self.assertEqual(reservation.bindings[0]["feature_branch"], "feature/0909-新能力")
            self.assertEqual(Path(reservation.bindings[0]["worktree_path"]).name, "0909-新能力")
            self.assertTrue(Path(reservation.bindings[0]["worktree_path"]).is_dir())
            self.assertFalse((repo / "data" / "tasks" / "task-1234567890.json").exists())
            self.assertTrue(reservation_marker_for(profile, reservation.reservation_id).is_file())
            restored = load_reservation(profile, reservation.reservation_id, recover_worktrees=True)
            self.assertEqual(restored.task_id, reservation.task_id)
            self.assertEqual(restored.bindings[0]["worktree_path"], reservation.bindings[0]["worktree_path"])

    def test_reservation_uses_date_and_readable_title_for_branch_and_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, profile = self._setup_repo(Path(tmp))
            with (
                patch("loopforge.task_publisher._local_date_prefix", return_value="0909"),
                patch("loopforge.task_publisher.uuid.uuid4") as make_uuid,
            ):
                make_uuid.return_value.hex = "1234567890abcdef"
                reservation = reserve_task(
                    profile,
                    {"title": "Readable Worktree Names", "source": "manual"},
                )

            binding = reservation.bindings[0]
            self.assertEqual(reservation.task_id, "task-1234567890")
            self.assertEqual(binding["feature_branch"], "feature/0909-readable-worktree-names")
            self.assertEqual(Path(binding["worktree_path"]).name, "0909-readable-worktree-names")
            self.assertNotIn("1234567890", binding["feature_branch"])
            self.assertNotIn("1234567890", binding["worktree_path"])

    def test_reservation_adds_numeric_suffix_when_readable_name_is_taken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, profile = self._setup_repo(Path(tmp))
            with patch("loopforge.task_publisher._local_date_prefix", return_value="0909"):
                first = reserve_task(profile, {"title": "Readable Worktree Names"})
                second = reserve_task(profile, {"title": "Readable Worktree Names"})

            self.assertEqual(first.bindings[0]["feature_branch"], "feature/0909-readable-worktree-names")
            self.assertEqual(second.bindings[0]["feature_branch"], "feature/0909-readable-worktree-names-2")
            self.assertEqual(Path(second.bindings[0]["worktree_path"]).name, "0909-readable-worktree-names-2")

    def test_reservation_does_not_reuse_preserved_remote_branch_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            with patch("loopforge.task_publisher._local_date_prefix", return_value="0909"):
                first = reserve_task(profile, {"title": "Remote Branch Collision"})
                branch = first.bindings[0]["feature_branch"]
                worktree = first.bindings[0]["worktree_path"]
                self._git(repo, "push", "-q", "origin", branch)
                self._git(repo, "worktree", "remove", "--force", worktree)
                self._git(repo, "branch", "-D", branch)

                second = reserve_task(profile, {"title": "Remote Branch Collision"})

            self.assertEqual(branch, "feature/0909-remote-branch-collision")
            self.assertEqual(second.bindings[0]["feature_branch"], f"{branch}-2")

    def test_multi_target_reservation_uses_one_suffix_when_child_name_is_taken(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _, _, parent_profile = self._setup_repo(base / "parent")
            _, child_repo, _ = self._setup_repo(base / "child")
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=parent_profile.root_dir,
                project_group={"children": [{"key": "api", "path": str(child_repo)}]},
            )
            child_branch = "feature/0909-multi-repo-collision/api"
            self._git(child_repo, "branch", child_branch)

            with patch("loopforge.task_publisher._local_date_prefix", return_value="0909"):
                reservation = reserve_task(
                    profile,
                    {"title": "Multi Repo Collision", "targets": [{"id": "api", "project": "api"}]},
                )

            ledger = next(binding for binding in reservation.bindings if binding["kind"] == "ledger")
            target = next(binding for binding in reservation.bindings if binding["kind"] == "target")
            self.assertEqual(ledger["feature_branch"], "feature/0909-multi-repo-collision-2")
            self.assertEqual(target["feature_branch"], "feature/0909-multi-repo-collision-2/api")
            self.assertEqual(
                self._git(profile.root_dir, "branch", "--list", "feature/0909-multi-repo-collision").stdout.strip(),
                "",
            )

    def test_publish_pushes_feature_revision_then_creates_main_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "发布任务", "source": "manual"})

            published = publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "发布任务",
                    "description": "先推 feature，再发布任务。",
                    "acceptance": ["远端引用存在"],
                    "planning_level": "complex",
                    "source": "manual",
                    "docs": {},
                    "targets": [],
                },
            )

            task = load_task(profile, reservation.task_id)
            self.assertEqual(published["status"], "published")
            self.assertEqual(list(published["task"])[:3], ["schema_version", "id", "title"])
            self.assertEqual(task["acceptance"], ["远端引用存在"])
            self.assertEqual(task["acceptance_refs"], [])
            self.assertEqual(task["git"]["branch_revision"], reservation.bindings[0]["branch_revision"])
            remote_feature = self._git(repo, "ls-remote", "origin", f"refs/heads/{task['git']['feature_branch']}").stdout.split()[0]
            self.assertEqual(remote_feature, task["git"]["branch_revision"])
            remote_main = self._git(repo, "rev-parse", "origin/main").stdout.strip()
            self.assertEqual(remote_main, self._git(repo, "rev-parse", "HEAD").stdout.strip())
            remote_json = subprocess.run(
                ["git", "--git-dir", str(origin), "show", f"main:data/tasks/{reservation.task_id}.json"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertEqual(json.loads(remote_json)["id"], reservation.task_id)

    def test_publish_documented_task_omits_duplicate_acceptance_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "文档任务", "source": "manual"})

            published = publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "文档任务",
                    "description": "由三件套定义完成条件。",
                    "acceptance": ["不应重复写入"],
                    "acceptance_refs": [{"acceptance_index": 0, "path": "docs/demo/specs.md"}],
                    "planning_level": "complex",
                    "source": "manual",
                    "docs": {
                        "module": "demo",
                        "requirements": ["docs/demo/requirements.md"],
                        "design": ["docs/demo/design.md"],
                        "specs": ["docs/demo/specs.md"],
                    },
                    "targets": [],
                },
            )

            task = load_task(profile, reservation.task_id)
            self.assertNotIn("acceptance", published["task"])
            self.assertNotIn("acceptance_refs", published["task"])
            self.assertNotIn("acceptance", task)
            self.assertNotIn("acceptance_refs", task)

    def test_publish_uses_ephemeral_worktree_and_preserves_untracked_main_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "隔离发布", "source": "manual"})
            unrelated = repo / "local-artifact.txt"
            unrelated.write_text("用户文件\n", encoding="utf-8")
            worktrees_before = {
                line for line in self._git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
                if line.startswith("worktree ")
            }

            published = publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "隔离发布",
                    "description": "任务发布不能接管用户 main 的无关文件。",
                    "acceptance": ["临时 worktree 已清理"],
                    "planning_level": "complex",
                    "source": "manual",
                    "docs": {},
                    "targets": [],
                },
            )

            task_path = f"data/tasks/{reservation.task_id}.json"
            self.assertEqual(published["status"], "published")
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "用户文件\n")
            self.assertEqual(self._git(repo, "status", "--short").stdout.strip(), "?? local-artifact.txt")
            worktrees_after = {
                line for line in self._git(repo, "worktree", "list", "--porcelain").stdout.splitlines()
                if line.startswith("worktree ")
            }
            self.assertEqual(worktrees_after, worktrees_before)
            self.assertFalse(any("loopforge-publish-" in line for line in worktrees_after))
            self.assertEqual(
                self._git(repo, "show", "--pretty=", "--name-only", "HEAD").stdout.strip(),
                task_path,
            )
            self.assertEqual(load_task(profile, reservation.task_id)["id"], reservation.task_id)

    def test_publish_from_ephemeral_worktree_does_not_push_local_main_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "隔离本地提交", "source": "manual"})
            local_file = repo / "local-only.txt"
            local_file.write_text("仅本地\n", encoding="utf-8")
            self._git(repo, "add", "local-only.txt")
            self._git(repo, "commit", "-q", "-m", "local only")
            local_revision = self._git(repo, "rev-parse", "HEAD").stdout.strip()

            published = publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "隔离本地提交",
                    "description": "发布任务时不得携带本地 main 的其他提交。",
                    "acceptance": ["远端只新增任务 JSON"],
                    "planning_level": "complex",
                    "source": "manual",
                    "docs": {},
                    "targets": [],
                },
            )

            self.assertEqual(published["status"], "published")
            self.assertEqual(self._git(repo, "rev-parse", "HEAD").stdout.strip(), local_revision)
            self.assertFalse((repo / "data" / "tasks" / f"{reservation.task_id}.json").exists())
            remote_files = subprocess.run(
                ["git", "--git-dir", str(origin), "ls-tree", "-r", "--name-only", "main"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
            self.assertIn(f"data/tasks/{reservation.task_id}.json", remote_files)
            self.assertNotIn("local-only.txt", remote_files)

    def test_failed_main_push_removes_ephemeral_worktree_without_touching_user_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            origin, repo, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "失败清理", "source": "manual"})
            base_revision = self._git(repo, "rev-parse", "HEAD").stdout.strip()
            worktrees_before = self._git(repo, "worktree", "list", "--porcelain").stdout
            hook = origin / "hooks" / "pre-receive"
            hook.write_text(
                "#!/bin/sh\n"
                "while read old new ref\n"
                "do\n"
                "  if [ \"$ref\" = \"refs/heads/main\" ]; then exit 1; fi\n"
                "done\n"
                "exit 0\n",
                encoding="utf-8",
            )
            hook.chmod(hook.stat().st_mode | stat.S_IXUSR)

            published = publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "失败清理",
                    "description": "推送失败也必须清理临时 worktree。",
                    "acceptance": ["用户 main 不产生任务提交"],
                    "planning_level": "complex",
                    "source": "manual",
                    "docs": {},
                    "targets": [],
                },
            )

            self.assertEqual(published["status"], "sync_blocked")
            self.assertEqual(self._git(repo, "rev-parse", "HEAD").stdout.strip(), base_revision)
            self.assertFalse((repo / "data" / "tasks" / f"{reservation.task_id}.json").exists())
            self.assertEqual(self._git(repo, "worktree", "list", "--porcelain").stdout, worktrees_before)

    def test_callers_cannot_inject_identity_branch_or_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, profile = self._setup_repo(Path(tmp))
            for field, value in (
                ("id", "manual-id"),
                ("feature_branch", "feature/manual"),
                ("branch", "feature/manual"),
                ("worktree_path", "/tmp/manual"),
                ("_managed_name", "0909-manual"),
            ):
                with self.subTest(field=field):
                    with self.assertRaisesRegex(TaskPublisherError, field):
                        reserve_task(profile, {"title": "非法请求", field: value})

    def test_missing_reserved_worktree_is_rebuilt_from_owned_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            with patch("loopforge.task_publisher._local_date_prefix", return_value="0909"):
                reservation = reserve_task(profile, {"title": "恢复 worktree"})
            worktree = Path(reservation.bindings[0]["worktree_path"])
            self._git(repo, "worktree", "remove", "--force", str(worktree))
            self.assertFalse(worktree.exists())

            with patch("loopforge.task_publisher._local_date_prefix", return_value="0910"):
                restored = load_reservation(profile, reservation.reservation_id, recover_worktrees=True)

            self.assertTrue(Path(restored.bindings[0]["worktree_path"]).is_dir())
            self.assertEqual(restored.bindings[0]["feature_branch"], "feature/0909-恢复-worktree")
            self.assertEqual(restored.bindings[0]["feature_branch"], reservation.bindings[0]["feature_branch"])

    def test_existing_manual_branch_is_not_taken_over_when_allocating_readable_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            branch = "feature/0909-不得接管人工分支"
            self._git(repo, "branch", branch)
            with patch("loopforge.task_publisher._local_date_prefix", return_value="0909"):
                reservation = reserve_task(profile, {"title": "不得接管人工分支"})

            self.assertEqual(reservation.bindings[0]["feature_branch"], f"{branch}-2")
            self.assertEqual(self._git(repo, "branch", "--list", branch).stdout.strip(), branch)

    def test_legacy_reserved_branch_is_migrated_without_losing_revision(self) -> None:
        migrate = getattr(task_publisher, "migrate_reservation_feature_branches", None)
        self.assertIsNotNone(migrate, "缺少 reservation 受管分支迁移入口")
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "迁移旧分支"})
            binding = reservation.bindings[0]
            worktree = Path(binding["worktree_path"])
            old_branch = f"loopforge/demo/{reservation.task_id}"
            new_branch = f"feature/demo/{reservation.task_id}"
            (worktree / "docs.md").write_text("文档\n", encoding="utf-8")
            self._git(worktree, "add", "docs.md")
            self._git(worktree, "commit", "-q", "-m", "docs")
            expected_revision = self._git(worktree, "rev-parse", "HEAD").stdout.strip()
            self._git(worktree, "branch", "-m", old_branch)

            ownership_path = Path(binding["ownership_marker"])
            ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
            ownership["branch"] = old_branch
            ownership_path.write_text(json.dumps(ownership), encoding="utf-8")
            reservation_path = reservation_marker_for(profile, reservation.reservation_id)
            reservation_payload = json.loads(reservation_path.read_text(encoding="utf-8"))
            reservation_payload["bindings"][0]["feature_branch"] = old_branch
            reservation_path.write_text(json.dumps(reservation_payload), encoding="utf-8")

            migrated = migrate(profile, reservation.reservation_id)

            self.assertEqual(migrated.bindings[0]["feature_branch"], new_branch)
            self.assertEqual(migrated.bindings[0]["branch_revision"], expected_revision)
            self.assertEqual(self._git(worktree, "branch", "--show-current").stdout.strip(), new_branch)
            self.assertEqual(self._git(repo, "branch", "--list", old_branch).stdout.strip(), "")
            self.assertEqual(json.loads(ownership_path.read_text(encoding="utf-8"))["branch"], new_branch)
            self.assertEqual(self._git(worktree, "status", "--porcelain").stdout.strip(), "")

    def test_reservation_branch_migration_rolls_back_when_marker_update_fails(self) -> None:
        migrate = getattr(task_publisher, "migrate_reservation_feature_branches", None)
        self.assertIsNotNone(migrate, "缺少 reservation 受管分支迁移入口")
        with tempfile.TemporaryDirectory() as tmp:
            _, _, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "迁移失败回滚"})
            binding = reservation.bindings[0]
            worktree = Path(binding["worktree_path"])
            old_branch = f"loopforge/demo/{reservation.task_id}"
            self._git(worktree, "branch", "-m", old_branch)

            ownership_path = Path(binding["ownership_marker"])
            ownership = json.loads(ownership_path.read_text(encoding="utf-8"))
            ownership["branch"] = old_branch
            ownership_path.write_text(json.dumps(ownership), encoding="utf-8")
            reservation_path = reservation_marker_for(profile, reservation.reservation_id)
            reservation_payload = json.loads(reservation_path.read_text(encoding="utf-8"))
            reservation_payload["bindings"][0]["feature_branch"] = old_branch
            reservation_path.write_text(json.dumps(reservation_payload), encoding="utf-8")

            original_write_text = Path.write_text
            failed = False

            def fail_first_marker_write(path: Path, *args: object, **kwargs: object) -> int:
                nonlocal failed
                if path == ownership_path and not failed:
                    failed = True
                    raise OSError("模拟 marker 写入失败")
                return original_write_text(path, *args, **kwargs)

            with patch.object(Path, "write_text", fail_first_marker_write):
                with self.assertRaisesRegex(TaskPublisherError, "迁移失败"):
                    migrate(profile, reservation.reservation_id)

            self.assertEqual(self._git(worktree, "branch", "--show-current").stdout.strip(), old_branch)
            self.assertEqual(json.loads(ownership_path.read_text(encoding="utf-8"))["branch"], old_branch)

    def test_cancel_reservation_cleans_local_resources_and_keeps_remote_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, repo, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "取消预留"})
            binding = reservation.bindings[0]
            branch = binding["feature_branch"]
            worktree = Path(binding["worktree_path"])
            marker = Path(binding["ownership_marker"])
            self._git(repo, "push", "-q", "origin", branch)

            cancelled = cancel_reservation(profile, reservation.reservation_id)

            self.assertEqual(cancelled["status"], "completed", cancelled)
            self.assertEqual(cancelled["reservation"]["status"], "cancelled")
            self.assertFalse(worktree.exists())
            self.assertFalse(marker.exists())
            self.assertEqual(self._git(repo, "branch", "--list", branch).stdout.strip(), "")
            remote = self._git(repo, "ls-remote", "origin", f"refs/heads/{branch}").stdout.strip()
            self.assertTrue(remote)

            repeated = cancel_reservation(profile, reservation.reservation_id)
            self.assertTrue(repeated["already_cancelled"])

    def test_cancel_reservation_requires_explicit_discard_for_dirty_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "脏目录取消"})
            worktree = Path(reservation.bindings[0]["worktree_path"])
            (worktree / "draft.md").write_text("未提交\n", encoding="utf-8")

            blocked = cancel_reservation(profile, reservation.reservation_id)

            self.assertEqual(blocked["status"], "failed")
            self.assertEqual(blocked["reservation"]["status"], "cancel_blocked")
            self.assertEqual(blocked["cleanup_result"]["targets"][0]["code"], "dirty_worktree_cleanup_requires_discard")
            self.assertTrue(worktree.exists())

            cancelled = cancel_reservation(profile, reservation.reservation_id, discard_changes=True)
            self.assertEqual(cancelled["status"], "completed", cancelled)
            self.assertTrue(cancelled["cleanup_result"]["targets"][0]["discarded_changes"])
            self.assertFalse(worktree.exists())

    def test_published_reservation_cannot_be_cancelled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, _, profile = self._setup_repo(Path(tmp))
            reservation = reserve_task(profile, {"title": "已发布任务"})
            publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "已发布任务",
                    "description": "必须改走 abandon。",
                    "acceptance": ["任务已发布"],
                    "source": "manual",
                    "planning_level": "complex",
                    "docs": {},
                    "targets": [],
                },
            )

            with self.assertRaisesRegex(TaskPublisherError, "abandon"):
                cancel_reservation(profile, reservation.reservation_id)

    def test_cancel_multi_target_reservation_cleans_every_owned_binding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _, _, profile = self._setup_repo(base / "parent")
            _, child_repo, _ = self._setup_repo(base / "child")
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=profile.root_dir,
                project_group={"children": [{"key": "api", "path": str(child_repo)}]},
            )
            reservation = reserve_task(
                profile,
                {"title": "多 Target 取消", "targets": [{"project": "api"}]},
            )
            target_binding = next(binding for binding in reservation.bindings if binding["kind"] == "target")
            (Path(target_binding["worktree_path"]) / "draft.md").write_text("未提交\n", encoding="utf-8")

            blocked = cancel_reservation(profile, reservation.reservation_id)
            self.assertEqual(blocked["status"], "failed", blocked)
            self.assertFalse(Path(reservation.bindings[0]["worktree_path"]).exists())
            self.assertTrue(Path(target_binding["worktree_path"]).exists())

            cancelled = cancel_reservation(profile, reservation.reservation_id, discard_changes=True)

            self.assertEqual(cancelled["status"], "completed", cancelled)
            self.assertEqual(len(cancelled["cleanup_result"]["targets"]), 2)
            self.assertTrue(cancelled["cleanup_result"]["targets"][0]["already_cleaned"])
            self.assertTrue(cancelled["cleanup_result"]["targets"][1]["discarded_changes"])
            for binding in reservation.bindings:
                self.assertFalse(Path(binding["worktree_path"]).exists())
                self.assertFalse(Path(binding["ownership_marker"]).exists())

    def test_single_target_group_merge_preserves_ledger_binding_and_cleanup_completes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _, _, parent_profile = self._setup_repo(base / "parent")
            _, child_repo, _ = self._setup_repo(base / "child")
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=parent_profile.root_dir,
                project_type="project-group",
                project_group={"children": [{"key": "api", "path": str(child_repo)}]},
            )
            reservation = reserve_task(
                profile,
                {"title": "单 Target 多仓任务", "targets": [{"id": "api", "project": "api"}]},
            )
            ledger = next(binding for binding in reservation.bindings if binding["kind"] == "ledger")
            target = next(binding for binding in reservation.bindings if binding["kind"] == "target")
            published = publish_new_task(
                profile,
                reservation.reservation_id,
                {
                    "title": "单 Target 多仓任务",
                    "description": "验证父账本与业务 Target 独立清理。",
                    "acceptance": ["父账本与业务 Target 均完成清理"],
                    "planning_level": "complex",
                    "source": "manual",
                    "docs": {},
                    "targets": [{"id": "api", "project": "api", "scope": "实现接口"}],
                },
            )
            task_id = published["task"]["id"]
            for status in ("claimed", "ready_for_review", "accepted"):
                update_item(profile, task_id, status=status)

            merged = merge_task(profile, task_id)

            self.assertEqual(merged["status"], "completed", merged)
            self.assertEqual(merged["task"]["git"]["feature_branch"], ledger["feature_branch"])
            corrupted = update_item(profile, task_id, branch=target["feature_branch"])
            self.assertEqual(corrupted["git"]["feature_branch"], target["feature_branch"])
            cleaned = cleanup_task(profile, task_id)
            self.assertEqual(cleaned["status"], "completed", cleaned)
            self.assertEqual(cleaned["task"]["status"], "completed")
            for binding in (target, ledger):
                self.assertFalse(Path(binding["worktree_path"]).exists())
                self.assertFalse(Path(binding["ownership_marker"]).exists())

    def test_cli_exposes_explicit_reservation_cancel_and_discard_flag(self) -> None:
        args = build_parser().parse_args(
            ["project", "task-reservation-cancel", "demo", "reservation-1234567890", "--discard-changes"]
        )

        self.assertEqual(args.project_command, "task-reservation-cancel")
        self.assertEqual(args.reservation_id, "reservation-1234567890")
        self.assertTrue(args.discard_changes)

    def test_cli_exposes_explicit_reservation_branch_migration(self) -> None:
        process = subprocess.run(
            [sys.executable, "-m", "loopforge.cli", "project", "task-reservation-migrate-branches", "--help"],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertEqual(process.returncode, 0, process.stderr)


if __name__ == "__main__":
    unittest.main()
