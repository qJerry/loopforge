from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from loopforge.auto_scheduler import AutoScheduler, schedule_tick_due
from loopforge.run_dispatch import schedule_dispatch_tick


CONFIG = Path("tests/fixtures/projects.json")


class AutoSchedulerTests(unittest.TestCase):
    def test_schedule_dispatch_tick_returns_accepted(self) -> None:
        accepted = {"status": "accepted", "dispatch_id": "dispatch-1", "pid": 123, "summary": "运行已受理"}
        with patch("loopforge.run_dispatch.dispatch_project_action", return_value=accepted) as dispatch:
            payload = schedule_dispatch_tick(CONFIG, respect_frequency=False)

        self.assertEqual(payload["status"], "accepted")
        self.assertEqual(payload["run"], accepted)
        dispatch.assert_called_once_with(
            CONFIG,
            "demo",
            "run_once",
            loop_type="dev",
            trigger="schedule",
        )

    def test_auto_scheduler_due_uses_dispatch_tick(self) -> None:
        accepted = {"status": "accepted", "summary": "运行已受理"}
        with patch("loopforge.auto_scheduler.schedule_dispatch_tick", return_value=accepted) as dispatch:
            payload = schedule_tick_due(CONFIG)

        self.assertEqual(payload, accepted)
        dispatch.assert_called_once_with(CONFIG, respect_frequency=True)

    def test_tick_once_finishes_when_no_task(self) -> None:
        calls = []

        def tick(config_path: Path | None) -> dict:
            calls.append(config_path)
            return {"status": "skipped_no_task", "summary": "没有可调度项目"}

        scheduler = AutoScheduler(Path("projects.json"), interval_seconds=1, tick_fn=tick)
        result = scheduler.tick_once()

        self.assertEqual(result["status"], "skipped_no_task")
        self.assertEqual(len(calls), 1)
        snapshot = scheduler.snapshot()
        self.assertEqual(snapshot["tick_count"], 1)
        self.assertEqual(snapshot["last_result"]["status"], "skipped_no_task")

    def test_tick_once_records_error_and_returns_failed_payload(self) -> None:
        def tick(config_path: Path | None) -> dict:
            raise RuntimeError("boom")

        scheduler = AutoScheduler(None, interval_seconds=1, tick_fn=tick)
        result = scheduler.tick_once()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["summary"], "boom")
        self.assertEqual(scheduler.snapshot()["last_error"], "boom")


if __name__ == "__main__":
    unittest.main()
