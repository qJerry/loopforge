from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loopforge.config import ProjectProfile
from loopforge.history import append_jsonl, count_consecutive_timeouts, read_jsonl
from loopforge.lock import acquire_lock, lock_record, release_lock
from loopforge.scheduler import choose_project, choose_project_loop, is_due, next_due_at


def profile(root: Path, project_id: str = "fake", priority: int = 10) -> ProjectProfile:
    return ProjectProfile(
        project_id=project_id,
        name=project_id,
        root_dir=root,
        priority=priority,
        notification_channel="none",
    )


class HistoryLockSchedulerTests(unittest.TestCase):
    def test_jsonl_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "index.jsonl"
            append_jsonl(path, {"status": "completed", "task_id": "a"})
            append_jsonl(path, {"status": "timeout_continue", "task_id": "a"})
            records = read_jsonl(path)
            self.assertEqual(len(records), 2)
            self.assertEqual(count_consecutive_timeouts(records, "a"), 1)

    def test_lock_conflict_and_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = profile(Path(tmp))
            conflict = acquire_lock(p, "run-1")
            self.assertIsNone(conflict)
            conflict = acquire_lock(p, "run-2")
            self.assertIsNotNone(conflict)
            record = lock_record(p, "run-2", "manual")
            self.assertEqual(record["status"], "skipped_already_running")
            release_lock(p, "run-1")
            self.assertFalse(p.lock_path.exists())

    def test_scheduler_prefers_active_task_then_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            active = profile(root, "active", priority=100)
            backlog = profile(root, "backlog", priority=1)
            chosen = choose_project(
                [
                    (backlog, {"project_status": "idle", "has_open_backlog": True}),
                    (active, {"project_status": "idle", "active_task": {"can_continue": True}}),
                ]
            )
            self.assertEqual(chosen.project_id, "active")


    def test_loop_scheduler_scans_without_backlog(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = profile(Path(tmp), "scan-only")
            chosen = choose_project_loop([(p, {"project_status": "completed", "has_open_backlog": False, "active_task": None})])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen[1], "scan")

    def test_loop_scheduler_respects_project_automation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status_open = {"project_status": "idle", "active_task": {"status": "open", "can_continue": True}}
            status_ready = {"project_status": "idle", "active_task": {"status": "spec_ready", "can_continue": True}}

            observe = profile(root, "observe")
            observe.automation_mode = "observe"
            chosen = choose_project_loop([(observe, status_open)])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen[1], "scan")

            execute = profile(root, "execute")
            execute.automation_mode = "execute"
            chosen = choose_project_loop([(execute, status_ready)])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen[1], "dev")

            chosen = choose_project_loop([(execute, status_open)])
            self.assertIsNotNone(chosen)
            self.assertEqual(chosen[1], "dev")

            off = profile(root, "off")
            off.automation_mode = "off"
            off.schedule_enabled = False
            self.assertIsNone(choose_project_loop([(off, status_open)]))

    def test_scheduler_skips_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = profile(Path(tmp), "blocked")
            chosen = choose_project([(p, {"project_status": "blocked", "has_open_backlog": True})])
            self.assertIsNone(chosen)

    def test_scheduler_uses_schedule_started_at_before_first_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started_at = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
            p = profile(Path(tmp), "scheduled")
            p.schedule_started_at = started_at.isoformat()
            p.schedule_frequency = "hourly"
            status = {"project_status": "idle", "has_open_backlog": True}

            self.assertFalse(is_due(p, status, now=started_at + timedelta(minutes=59)))
            self.assertTrue(is_due(p, status, now=started_at + timedelta(hours=1)))
            self.assertEqual(next_due_at(p, status, now=started_at), "2026-07-01T09:00:00+00:00")

    def test_scheduler_supports_half_hourly_frequency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started_at = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
            p = profile(Path(tmp), "scheduled")
            p.schedule_started_at = started_at.isoformat()
            p.schedule_frequency = "half_hourly"
            status = {"project_status": "idle", "has_open_backlog": True}

            self.assertFalse(is_due(p, status, now=started_at + timedelta(minutes=29)))
            self.assertTrue(is_due(p, status, now=started_at + timedelta(minutes=30)))
            self.assertEqual(next_due_at(p, status, now=started_at), "2026-07-01T08:30:00+00:00")

    def test_scheduler_uses_later_of_last_run_and_schedule_started_at(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            started_at = datetime(2026, 7, 1, 8, 0, tzinfo=timezone.utc)
            p = profile(Path(tmp), "scheduled")
            p.schedule_started_at = started_at.isoformat()
            p.schedule_frequency = "hourly"
            status = {
                "project_status": "idle",
                "has_open_backlog": True,
                "last_run": {"ended_at": "2026-07-01T07:00:00+00:00"},
            }

            self.assertFalse(is_due(p, status, now=started_at + timedelta(minutes=30)))
            self.assertEqual(next_due_at(p, status, now=started_at), "2026-07-01T09:00:00+00:00")


if __name__ == "__main__":
    unittest.main()
