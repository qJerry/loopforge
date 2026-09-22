from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from loopforge.commands import project_cancel_run, project_status
from loopforge.config import load_projects
from loopforge.lock import is_process_alive, read_lock
from loopforge.run_dispatch import active_dispatch_path, dispatch_paths, dispatch_project_action


class RunDispatchProcessTests(unittest.TestCase):
    def test_detached_child_survives_launcher_exit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            completed_path = root / "completed.txt"
            log_path = root / "runner.log"
            launcher = """
import sys
from pathlib import Path
from loopforge.run_dispatch import spawn_detached_process

completed_path = Path(sys.argv[1])
log_path = Path(sys.argv[2])
spawn_detached_process(
    [sys.executable, "-c", "import pathlib,sys,time; time.sleep(0.4); pathlib.Path(sys.argv[1]).write_text('done')", str(completed_path)],
    log_path,
)
"""

            launched = subprocess.run(
                [sys.executable, "-c", launcher, str(completed_path), str(log_path)],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )

            self.assertEqual(launched.returncode, 0, launched.stderr)
            deadline = time.monotonic() + 5
            while not completed_path.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(completed_path.read_text(encoding="utf-8"), "done")

    def test_fake_project_dispatch_completes_in_independent_worker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config_path = self.write_project(root, executor="fake_codex")
            profile = load_projects(config_path)[0]

            accepted = dispatch_project_action(config_path, "fake", "run_once")

            self.assertEqual(accepted["status"], "accepted")
            result_path = dispatch_paths(profile, accepted["dispatch_id"])["result"]
            self.wait_for(result_path.exists)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["payload"]["status"], "completed")
            self.assertFalse(active_dispatch_path(profile).exists())

    def test_new_status_process_observes_worker_and_cancel_stops_codex(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            codex_path, codex_pid_path = self.write_sleeping_codex(root)
            config_path = self.write_project(root, executor="codex_cli")
            profile = load_projects(config_path)[0]
            with patch.dict(
                os.environ,
                {
                    "LOOPFORGE_CODEX_BIN": str(codex_path),
                    "LOOPFORGE_TEST_CODEX_PID": str(codex_pid_path),
                },
            ):
                accepted = dispatch_project_action(config_path, "fake", "run_once")

            self.assertEqual(accepted["status"], "accepted")
            self.wait_for(lambda: read_lock(profile.lock_path) is not None)
            self.wait_for(codex_pid_path.exists)

            observed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "loopforge.cli",
                    "--config",
                    str(config_path),
                    "--json",
                    "project",
                    "status",
                    "fake",
                ],
                cwd=Path(__file__).resolve().parents[1],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )

            self.assertEqual(observed.returncode, 0, observed.stderr)
            self.assertEqual(json.loads(observed.stdout)["runner"]["project_status"], "running")
            cancel = project_cancel_run(config_path, "fake", reason="进程级取消测试")
            self.assertEqual(cancel["status"], "cancel_requested")
            result_path = dispatch_paths(profile, accepted["dispatch_id"])["result"]
            self.wait_for(result_path.exists)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(result["payload"]["status"], "cancelled")
            codex_pid = int(codex_pid_path.read_text(encoding="utf-8"))
            self.wait_for(lambda: is_process_alive(codex_pid) is False)
            self.assertFalse(active_dispatch_path(profile).exists())

    def test_killed_worker_becomes_timeout_and_can_be_resumed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            codex_path, codex_pid_path = self.write_sleeping_codex(root)
            config_path = self.write_project(root, executor="codex_cli")
            profile = load_projects(config_path)[0]
            with patch.dict(
                os.environ,
                {
                    "LOOPFORGE_CODEX_BIN": str(codex_path),
                    "LOOPFORGE_TEST_CODEX_PID": str(codex_pid_path),
                },
            ):
                accepted = dispatch_project_action(config_path, "fake", "run_once")

            self.wait_for(lambda: read_lock(profile.lock_path) is not None)
            self.wait_for(codex_pid_path.exists)
            os.killpg(accepted["pid"], signal.SIGKILL)
            self.wait_for(lambda: is_process_alive(accepted["pid"]) is False)

            status = project_status(config_path, "fake")

            self.assertEqual(status["runner"]["project_status"], "timeout")
            self.assertEqual(status["runner"]["dispatch"]["runtime_status"], "stale")
            self.assertIn("继续", status["runner"]["required_action"])

    def write_project(self, root: Path, *, executor: str) -> Path:
        data_dir = root / "data"
        data_dir.mkdir(parents=True)
        (data_dir / "dev-task.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "items": [
                        {
                            "id": "task-1",
                            "title": "独立运行测试",
                            "description": "验证独立 worker 生命周期",
                            "status": "coding",
                            "priority": "P1",
                            "source": "test",
                            "agent": "codex",
                            "planning_level": "lightweight",
                            "acceptance": ["独立运行完成"],
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        config_path = root / "projects.json"
        config_path.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "fake",
                            "name": "Fake",
                            "root_dir": str(root),
                            "state_dir": str(root / ".loopforge"),
                            "report_dir": str(root / ".loopforge" / "reports"),
                            "schedule_enabled": False,
                            "executor": executor,
                            "auto_commit": False,
                            "notification_channel": "none",
                        }
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return config_path

    def write_sleeping_codex(self, root: Path) -> tuple[Path, Path]:
        script = root / "sleeping-codex"
        pid_path = root / "codex.pid"
        script.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "import pathlib\n"
            "import sys\n"
            "import time\n"
            "pathlib.Path(os.environ['LOOPFORGE_TEST_CODEX_PID']).write_text(str(os.getpid()))\n"
            "sys.stdin.read()\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script, pid_path

    def wait_for(self, predicate, timeout: float = 10) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.fail("等待进程状态变化超时")


if __name__ == "__main__":
    unittest.main()
