from __future__ import annotations

import os
import io
import json
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from loopforge.cli import main as cli_main
from loopforge.config import ProjectProfile
from loopforge.domain import utc_now
from loopforge.run_dispatch import (
    DispatchAlreadyRunning,
    active_dispatch_path,
    active_dispatch_status,
    claim_active_dispatch,
    clear_active_dispatch,
    dispatch_project_action,
    dispatch_paths,
    execute_dispatch_request,
    mark_dispatch_running,
    write_dispatch_request,
)
from loopforge.lock import acquire_lock, release_lock


class RunDispatchStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.profile = ProjectProfile(
            project_id="demo",
            name="Demo",
            root_dir=self.root,
            state_dir=self.root / ".loopforge",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def request(self, dispatch_id: str) -> dict[str, object]:
        return {
            "version": 1,
            "dispatch_id": dispatch_id,
            "project_id": self.profile.project_id,
            "action": "run_once",
            "loop_type": "dev",
            "reason": "",
            "trigger": "manual",
            "config_path": str((self.root / "projects.json").resolve()),
            "created_at": utc_now(),
        }

    def test_active_dispatch_rejects_live_owner_and_replaces_stale_owner(self) -> None:
        claim_active_dispatch(self.profile, self.request("dispatch-live"))
        mark_dispatch_running(self.profile, "dispatch-live", os.getpid())

        self.assertEqual(active_dispatch_status(self.profile)["runtime_status"], "running")
        with self.assertRaises(DispatchAlreadyRunning):
            claim_active_dispatch(self.profile, self.request("dispatch-next"))

        mark_dispatch_running(self.profile, "dispatch-live", 99_999_999)
        claimed = claim_active_dispatch(self.profile, self.request("dispatch-next"))

        self.assertEqual(claimed["dispatch_id"], "dispatch-next")
        self.assertFalse(clear_active_dispatch(self.profile, "dispatch-live"))
        self.assertTrue(clear_active_dispatch(self.profile, "dispatch-next"))

    def test_launching_dispatch_is_temporarily_active(self) -> None:
        claim_active_dispatch(self.profile, self.request("dispatch-launching"))

        status_payload = active_dispatch_status(self.profile)

        self.assertTrue(status_payload["active"])
        self.assertEqual(status_payload["runtime_status"], "launching")

    def test_invalid_dispatch_file_is_stale(self) -> None:
        path = active_dispatch_path(self.profile)
        path.parent.mkdir(parents=True)
        path.write_text("not-json", encoding="utf-8")

        status_payload = active_dispatch_status(self.profile)

        self.assertFalse(status_payload["active"])
        self.assertEqual(status_payload["runtime_status"], "stale")

    def test_active_dispatch_file_is_private(self) -> None:
        claim_active_dispatch(self.profile, self.request("dispatch-private"))

        mode = stat.S_IMODE(active_dispatch_path(self.profile).stat().st_mode)

        self.assertEqual(mode, 0o600)


class RunDispatchWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "projects.json"
        self.profile = ProjectProfile(
            project_id="demo",
            name="Demo",
            root_dir=self.root,
            state_dir=self.root / ".loopforge",
        )
        self.config_path.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "demo",
                            "name": "Demo",
                            "root_dir": str(self.root),
                            "state_dir": str(self.profile.loopforge_dir),
                            "notification_channel": "none",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def request(self, action: str = "resume_once") -> dict[str, object]:
        return {
            "version": 1,
            "dispatch_id": f"dispatch-{action}",
            "project_id": "demo",
            "action": action,
            "loop_type": "dev",
            "reason": "继续当前实现",
            "trigger": "manual",
            "config_path": str(self.config_path.resolve()),
            "created_at": utc_now(),
        }

    def test_worker_executes_resume_request_and_writes_result(self) -> None:
        request = self.request()
        claim_active_dispatch(self.profile, request)
        request_path = write_dispatch_request(self.profile, request)

        with patch(
            "loopforge.commands.project_resume_once",
            return_value={"status": "completed", "summary": "已继续"},
        ) as resume:
            result = execute_dispatch_request(request_path)

        resume.assert_called_once_with(self.config_path.resolve(), "demo", "继续当前实现")
        self.assertEqual(result["status"], "completed")
        persisted = json.loads(
            dispatch_paths(self.profile, "dispatch-resume_once")["result"].read_text(encoding="utf-8")
        )
        self.assertEqual(persisted["payload"]["summary"], "已继续")
        self.assertEqual(stat.S_IMODE(request_path.stat().st_mode), 0o600)
        self.assertFalse(active_dispatch_path(self.profile).exists())


    def test_worker_rejects_action_outside_whitelist(self) -> None:
        request = self.request("arbitrary_shell")
        claim_active_dispatch(self.profile, request)
        request_path = write_dispatch_request(self.profile, request)

        with patch("loopforge.commands.project_run_once") as run_once:
            result = execute_dispatch_request(request_path)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "invalid_action")
        run_once.assert_not_called()

    def test_internal_worker_cli_returns_nonzero_for_invalid_action(self) -> None:
        request = self.request("arbitrary_shell")
        claim_active_dispatch(self.profile, request)
        request_path = write_dispatch_request(self.profile, request)

        with redirect_stdout(io.StringIO()):
            exit_code = cli_main(["--json", "run-worker", "--request", str(request_path)])

        self.assertEqual(exit_code, 1)

    def test_worker_config_failure_still_writes_result_and_clears_dispatch(self) -> None:
        request = self.request("run_once")
        claim_active_dispatch(self.profile, request)
        request_path = write_dispatch_request(self.profile, request)
        self.config_path.unlink()

        result = execute_dispatch_request(request_path)

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["code"], "worker_failed")
        self.assertTrue(dispatch_paths(self.profile, "dispatch-run_once")["result"].exists())
        self.assertFalse(active_dispatch_path(self.profile).exists())


class RunDispatchLaunchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "projects.json"
        self.profile = ProjectProfile(
            project_id="demo",
            name="Demo",
            root_dir=self.root,
            state_dir=self.root / ".loopforge",
        )
        self.config_path.write_text(
            json.dumps(
                {
                    "projects": [
                        {
                            "id": "demo",
                            "name": "Demo",
                            "root_dir": str(self.root),
                            "state_dir": str(self.profile.loopforge_dir),
                            "notification_channel": "none",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_dispatch_returns_accepted_without_waiting(self) -> None:
        fake_process = SimpleNamespace(pid=os.getpid())
        with patch("loopforge.run_dispatch.subprocess.Popen", return_value=fake_process):
            payload = dispatch_project_action(self.config_path, "demo", "run_once")

        self.assertEqual(payload["status"], "accepted")
        self.assertTrue(payload["dispatch_id"])
        self.assertEqual(payload["pid"], os.getpid())
        self.assertNotIn("run", payload)

    def test_reason_is_persisted_but_never_passed_in_process_arguments(self) -> None:
        reason = "只允许存在于受限请求文件"
        fake_process = SimpleNamespace(pid=os.getpid())
        with patch("loopforge.run_dispatch.subprocess.Popen", return_value=fake_process) as popen:
            payload = dispatch_project_action(
                self.config_path,
                "demo",
                "resume_once",
                reason=reason,
            )

        command = popen.call_args.args[0]
        self.assertNotIn(reason, command)
        request_path = dispatch_paths(self.profile, payload["dispatch_id"])["request"]
        request = json.loads(request_path.read_text(encoding="utf-8"))
        self.assertEqual(request["reason"], reason)

    def test_active_project_lock_skips_dispatch(self) -> None:
        self.assertIsNone(acquire_lock(self.profile, "existing-run"))
        self.addCleanup(release_lock, self.profile, "existing-run")

        with patch("loopforge.run_dispatch.subprocess.Popen") as popen:
            payload = dispatch_project_action(self.config_path, "demo", "run_once")

        self.assertEqual(payload["status"], "skipped_already_running")
        self.assertFalse(popen.called)

    def test_spawn_failure_writes_result_and_releases_active_dispatch(self) -> None:
        with patch("loopforge.run_dispatch.subprocess.Popen", side_effect=OSError("无法启动")):
            payload = dispatch_project_action(self.config_path, "demo", "run_once")

        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["code"], "failed_to_start")
        self.assertFalse(active_dispatch_path(self.profile).exists())
        result_path = dispatch_paths(self.profile, payload["dispatch_id"])["result"]
        self.assertEqual(json.loads(result_path.read_text(encoding="utf-8"))["status"], "failed_to_start")


if __name__ == "__main__":
    unittest.main()
