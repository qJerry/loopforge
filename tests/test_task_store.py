"""单任务 JSON Task Store 合同测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from loopforge.config import ProjectProfile
from loopforge.task_store import (
    TaskStoreError,
    archive_task_file,
    create_task_file,
    load_active_tasks,
    load_history_tasks,
    load_task,
    mutate_task_file,
)


class TaskStoreTests(unittest.TestCase):
    def _profile(self, root: Path) -> ProjectProfile:
        return ProjectProfile(project_id="demo", name="Demo", root_dir=root)

    def _task(self, task_id: str = "task-001", **overrides: object) -> dict:
        task = {
            "schema_version": 2,
            "id": task_id,
            "version": 1,
            "title": f"任务 {task_id}",
            "description": "验证单任务 JSON 存储。",
            "status": "open",
            "priority": "P2",
            "source": "manual",
            "planning_level": "complex",
            "created_at": "2026-08-27T01:00:00+00:00",
            "updated_at": "2026-08-27T01:00:00+00:00",
            "git": {
                "remote": "origin",
                "target_branch": "main",
                "base_revision": "a" * 40,
                "feature_branch": f"loopforge/demo/{task_id}",
                "branch_revision": "b" * 40,
            },
            "docs": {},
            "targets": [],
            "acceptance": ["任务行为符合说明"],
            "acceptance_refs": [],
        }
        task.update(overrides)
        return task

    def _documented_task(self, task_id: str = "task-docs", **overrides: object) -> dict:
        task = self._task(
            task_id,
            docs={
                "module": "demo",
                "requirements": ["docs/demo/requirements.md"],
                "design": ["docs/demo/design.md"],
                "specs": ["docs/demo/specs.md"],
            },
        )
        task.pop("acceptance")
        task.pop("acceptance_refs")
        task.update(overrides)
        return task

    def test_create_and_load_task_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))

            created = create_task_file(profile, self._task())

            self.assertEqual(created.after["id"], "task-001")
            self.assertEqual(load_task(profile, "task-001")["title"], "任务 task-001")
            self.assertEqual(created.paths, (Path("data/tasks/task-001.json"),))

    def test_create_omits_default_task_and_git_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)
            task = self._task()
            task.pop("source")
            task.pop("planning_level")
            task["git"].pop("remote")
            task["git"].pop("target_branch")

            created = create_task_file(profile, task)
            persisted = json.loads((root / "data" / "tasks" / "task-001.json").read_text(encoding="utf-8"))

            self.assertNotIn("source", created.after)
            self.assertNotIn("planning_level", created.after)
            self.assertNotIn("remote", persisted["git"])
            self.assertNotIn("target_branch", persisted["git"])

    def test_create_compacts_explicit_default_task_and_git_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)

            created = create_task_file(profile, self._task())
            persisted = json.loads((root / "data" / "tasks" / "task-001.json").read_text(encoding="utf-8"))

            self.assertNotIn("source", created.after)
            self.assertNotIn("planning_level", created.after)
            self.assertNotIn("remote", persisted["git"])
            self.assertNotIn("target_branch", persisted["git"])

    def test_create_removes_duplicate_task_and_target_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            task = self._task(
                intake={"mode": "continued", "source_type": "summary", "source_ref": ""},
                documentation_mode="referenced",
                targets=[
                    {
                        "id": "api",
                        "project": "api",
                        "repo": "api",
                        "scope": "实现接口",
                        "after": [],
                    }
                ],
            )

            created = create_task_file(profile, task)

            self.assertNotIn("intake", created.after)
            self.assertNotIn("documentation_mode", created.after)
            self.assertNotIn("repo", created.after["targets"][0])
            self.assertNotIn("after", created.after["targets"][0])


    def test_schema_requires_all_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            for field in (
                "schema_version",
                "id",
                "version",
                "title",
                "description",
                "status",
                "priority",
                "created_at",
                "updated_at",
                "git",
                "docs",
                "targets",
            ):
                with self.subTest(field=field):
                    task = self._task()
                    task.pop(field)
                    with self.assertRaisesRegex(TaskStoreError, field):
                        create_task_file(profile, task)

    def test_ordinary_task_requires_acceptance_and_acceptance_refs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            missing_acceptance = self._task()
            missing_acceptance.pop("acceptance")
            with self.assertRaisesRegex(TaskStoreError, "acceptance"):
                create_task_file(profile, missing_acceptance)

            missing_refs = self._task()
            missing_refs.pop("acceptance_refs")
            with self.assertRaisesRegex(TaskStoreError, "acceptance_refs"):
                create_task_file(profile, missing_refs)

    def test_documented_task_omits_duplicate_acceptance_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))

            created = create_task_file(profile, self._documented_task())

            self.assertNotIn("acceptance", created.after)
            self.assertNotIn("acceptance_refs", created.after)

    def test_task_json_uses_stable_semantic_field_order_for_create_mutate_and_archive(self) -> None:
        expected_prefix = [
            "schema_version",
            "id",
            "title",
            "description",
            "status",
            "priority",
            "version",
            "created_at",
            "updated_at",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)
            create_task_file(profile, self._task(status="merged", z_extension=1, a_extension=2))
            active_path = root / "data" / "tasks" / "task-001.json"
            active = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertEqual(list(active)[: len(expected_prefix)], expected_prefix)
            self.assertLess(list(active).index("acceptance"), list(active).index("git"))
            self.assertLess(list(active).index("a_extension"), list(active).index("z_extension"))
            self.assertEqual(
                list(active["git"]),
                ["base_revision", "feature_branch", "branch_revision"],
            )

            mutate_task_file(
                profile,
                "task-001",
                expected_version=1,
                allowed_states={"merged"},
                fields={"description": "顺序不因修改而变化"},
            )
            mutated = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertEqual(list(mutated)[: len(expected_prefix)], expected_prefix)

            archive_task_file(
                profile,
                "task-001",
                expected_version=2,
                allowed_states={"merged"},
                terminal_status="completed",
                at=datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc),
            )
            history_path = root / "data" / "tasks" / "history" / "2026" / "08" / "task-001.json"
            archived = json.loads(history_path.read_text(encoding="utf-8"))
            self.assertEqual(list(archived)[: len(expected_prefix)], expected_prefix)

    def test_history_remains_compatible_with_legacy_acceptance_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)
            task = self._task(status="merged")
            task["acceptance"] = ["旧验收项"]
            task["acceptance_refs"] = [{"acceptance_index": 0, "path": "docs/legacy/specs.md"}]
            create_task_file(profile, task)

            archive_task_file(
                profile,
                "task-001",
                expected_version=1,
                allowed_states={"merged"},
                terminal_status="completed",
                at=datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc),
            )

            history = load_history_tasks(profile)
            self.assertEqual(history["items"][0]["acceptance"], ["旧验收项"])
            self.assertEqual(history["items"][0]["acceptance_refs"][0]["acceptance_index"], 0)

    def test_rejects_duplicate_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            create_task_file(profile, self._task())

            with self.assertRaisesRegex(TaskStoreError, "已存在"):
                create_task_file(profile, self._task(title="重复任务"))

    def test_rejects_unsafe_id_and_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)
            with self.assertRaisesRegex(TaskStoreError, "id"):
                create_task_file(profile, self._task("../escape"))

            tasks_dir = root / "data" / "tasks"
            tasks_dir.mkdir(parents=True)
            outside = root / "outside.json"
            outside.write_text(json.dumps(self._task("linked")), encoding="utf-8")
            (tasks_dir / "linked.json").symlink_to(outside)
            with self.assertRaisesRegex(TaskStoreError, "符号链接"):
                load_task(profile, "linked")

    def test_active_tasks_have_deterministic_priority_fifo_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            tasks = [
                self._task("task-b", priority="P1", created_at="2026-08-27T02:00:00+00:00"),
                self._task("task-c", priority="P0", created_at="2026-08-27T03:00:00+00:00"),
                self._task("task-a", priority="P1", created_at="2026-08-27T02:00:00+00:00"),
                self._task("task-d", priority="P2", created_at="2026-08-27T00:00:00+00:00"),
            ]
            for task in tasks:
                create_task_file(profile, task)

            self.assertEqual(
                [task["id"] for task in load_active_tasks(profile)],
                ["task-c", "task-a", "task-b", "task-d"],
            )

    def test_mutation_checks_expected_version_and_allowed_source_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            create_task_file(profile, self._task())

            changed = mutate_task_file(
                profile,
                "task-001",
                expected_version=1,
                allowed_states={"open"},
                fields={"status": "claimed"},
            )
            self.assertEqual(changed.before["version"], 1)
            self.assertEqual(changed.after["version"], 2)
            self.assertEqual(changed.after["status"], "claimed")

            with self.assertRaisesRegex(TaskStoreError, "版本"):
                mutate_task_file(
                    profile,
                    "task-001",
                    expected_version=1,
                    allowed_states={"claimed"},
                    fields={"description": "陈旧写入"},
                )
            with self.assertRaisesRegex(TaskStoreError, "状态"):
                mutate_task_file(
                    profile,
                    "task-001",
                    expected_version=2,
                    allowed_states={"open"},
                    fields={"description": "非法源状态"},
                )

    def test_mutation_can_remove_legacy_acceptance_fields_without_touching_other_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            original = self._documented_task(
                "task-001",
                acceptance=["旧验收项"],
                acceptance_refs=[{"acceptance_index": 0, "path": "docs/review/specs.md"}],
                custom_field={"preserved": True},
            )
            create_task_file(profile, original)

            changed = mutate_task_file(
                profile,
                "task-001",
                expected_version=1,
                allowed_states={"open"},
                fields={},
                remove_fields={"acceptance", "acceptance_refs"},
            )

            self.assertNotIn("acceptance", changed.after)
            self.assertNotIn("acceptance_refs", changed.after)
            self.assertEqual(changed.after["custom_field"], {"preserved": True})
            self.assertEqual(changed.after["version"], 2)

    def test_rejects_illegal_status_transition_and_terminal_active_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            create_task_file(profile, self._task())

            with self.assertRaisesRegex(TaskStoreError, "跃迁"):
                mutate_task_file(
                    profile,
                    "task-001",
                    expected_version=1,
                    allowed_states={"open"},
                    fields={"status": "merged"},
                )
            with self.assertRaisesRegex(TaskStoreError, "终态"):
                create_task_file(profile, self._task(status="completed"))

    def test_allows_review_spec_rejection_to_return_to_spec_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            create_task_file(profile, self._task(status="ready_for_review"))

            changed = mutate_task_file(
                profile,
                "task-001",
                expected_version=1,
                allowed_states={"ready_for_review"},
                fields={
                    "status": "spec_blocked",
                    "review_decision": "rejected",
                    "review_feedback": "需求口径需要重新确认。",
                    "review_feedback_type": "spec",
                },
            )

            self.assertEqual(changed.after["status"], "spec_blocked")
            self.assertEqual(changed.after["review_feedback_type"], "spec")

    def test_allows_coding_worker_to_return_to_spec_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            create_task_file(profile, self._task(status="coding"))

            changed = mutate_task_file(
                profile,
                "task-001",
                expected_version=1,
                allowed_states={"coding"},
                fields={
                    "status": "spec_blocked",
                    "blocker_reason": "实现时发现权威合同缺口。",
                    "blockers": [
                        {
                            "type": "contract",
                            "code": "contract_missing",
                            "summary": "权威合同缺失。",
                        }
                    ],
                },
            )

            self.assertEqual(changed.after["status"], "spec_blocked")
            self.assertEqual(changed.after["blocker_reason"], "实现时发现权威合同缺口。")

    def test_archive_moves_terminal_task_to_year_month_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)
            create_task_file(profile, self._task(status="merged"))

            changed = archive_task_file(
                profile,
                "task-001",
                expected_version=1,
                allowed_states={"merged"},
                terminal_status="completed",
                at=datetime(2026, 8, 27, 8, 30, tzinfo=timezone.utc),
            )

            history_path = root / "data" / "tasks" / "history" / "2026" / "08" / "task-001.json"
            self.assertFalse((root / "data" / "tasks" / "task-001.json").exists())
            self.assertTrue(history_path.exists())
            self.assertEqual(changed.paths, (Path("data/tasks/task-001.json"), Path("data/tasks/history/2026/08/task-001.json")))
            self.assertEqual(changed.after["status"], "completed")
            self.assertEqual(changed.after["version"], 2)
            self.assertEqual(changed.after["archived_at"], "2026-08-27T08:30:00+00:00")

            with self.assertRaisesRegex(TaskStoreError, "历史任务不可修改"):
                mutate_task_file(
                    profile,
                    "task-001",
                    expected_version=2,
                    allowed_states={"completed"},
                    fields={"description": "不允许"},
                )

    def test_supersedes_must_reference_existing_history_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = self._profile(Path(tmp))
            with self.assertRaisesRegex(TaskStoreError, "supersedes"):
                create_task_file(profile, self._task("task-new", supersedes="task-old"))

            create_task_file(profile, self._task("task-old", status="merged"))
            archive_task_file(
                profile,
                "task-old",
                expected_version=1,
                allowed_states={"merged"},
                terminal_status="completed",
                at=datetime(2026, 7, 1, tzinfo=timezone.utc),
            )
            create_task_file(profile, self._task("task-new", supersedes="task-old"))
            self.assertEqual(load_task(profile, "task-new")["supersedes"], "task-old")

    def test_history_is_sorted_by_archived_at_and_ignores_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)
            observations = [
                ("task-old", datetime(2026, 7, 31, 23, 59, tzinfo=timezone.utc)),
                ("task-new", datetime(2026, 8, 1, 0, 1, tzinfo=timezone.utc)),
            ]
            for task_id, observed_at in observations:
                create_task_file(profile, self._task(task_id, status="merged"))
                archive_task_file(
                    profile,
                    task_id,
                    expected_version=1,
                    allowed_states={"merged"},
                    terminal_status="completed",
                    at=observed_at,
                )
            new_path = root / "data" / "tasks" / "history" / "2026" / "08" / "task-new.json"
            old_path = root / "data" / "tasks" / "history" / "2026" / "07" / "task-old.json"
            os.utime(new_path, (1, 1))
            os.utime(old_path, (2_000_000_000, 2_000_000_000))

            result = load_history_tasks(profile, limit=20)

            self.assertEqual([item["id"] for item in result["items"]], ["task-new", "task-old"])
            self.assertEqual(result["issues"], [])

    def test_history_returns_valid_items_and_reports_invalid_or_symlink_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)
            create_task_file(profile, self._task("task-valid", status="merged"))
            archive_task_file(
                profile,
                "task-valid",
                expected_version=1,
                allowed_states={"merged"},
                terminal_status="completed",
                at=datetime(2026, 8, 20, tzinfo=timezone.utc),
            )
            month_dir = root / "data" / "tasks" / "history" / "2026" / "08"
            (month_dir / "broken.json").write_text("{", encoding="utf-8")
            outside = root / "outside-history.json"
            outside.write_text(json.dumps(self._task("linked", status="completed")), encoding="utf-8")
            (month_dir / "linked.json").symlink_to(outside)

            result = load_history_tasks(profile, limit=20)

            self.assertEqual([item["id"] for item in result["items"]], ["task-valid"])
            self.assertEqual(len(result["issues"]), 2)
            self.assertEqual({issue["code"] for issue in result["issues"]}, {"invalid_task_file", "unsafe_task_path"})

    def test_history_stops_before_older_month_after_limit_is_satisfied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = self._profile(root)
            for index in range(21):
                task_id = f"task-{index:02d}"
                create_task_file(profile, self._task(task_id, status="merged"))
                archive_task_file(
                    profile,
                    task_id,
                    expected_version=1,
                    allowed_states={"merged"},
                    terminal_status="completed",
                    at=datetime(2026, 8, index + 1, tzinfo=timezone.utc),
                )
            older = root / "data" / "tasks" / "history" / "2026" / "07"
            older.mkdir(parents=True)
            (older / "not-scanned.json").write_text("{", encoding="utf-8")

            result = load_history_tasks(profile, limit=20)

            self.assertEqual(len(result["items"]), 20)
            self.assertEqual(result["items"][0]["id"], "task-20")
            self.assertEqual(result["issues"], [])


if __name__ == "__main__":
    unittest.main()
