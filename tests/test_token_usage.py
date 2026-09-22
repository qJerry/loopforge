from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Optional

from loopforge import token_usage
from loopforge.config import ProjectProfile
from loopforge.token_usage import build_task_usage, extract_token_usage, task_associated_project_ids


class TokenUsageTests(unittest.TestCase):
    def test_estimates_api_equivalent_cost_with_cached_input_and_long_context_range(self) -> None:
        self.assertTrue(hasattr(token_usage, "estimate_api_cost"), "缺少 API 等价成本估算入口")
        estimate = token_usage.estimate_api_cost(
            "gpt-5.6-sol",
            {
                "input_tokens": 640786,
                "cached_input_tokens": 588032,
                "output_tokens": 7571,
                "reasoning_output_tokens": 3360,
                "total_tokens": 648357,
            },
        )

        self.assertEqual(
            estimate,
            {
                "currency": "USD",
                "minimum_usd": 0.597649,
                "maximum_usd": 1.119588,
                "long_context_pricing_possible": True,
            },
        )

    def test_unknown_model_has_no_cost_estimate(self) -> None:
        self.assertTrue(hasattr(token_usage, "estimate_api_cost"), "缺少 API 等价成本估算入口")
        estimate = token_usage.estimate_api_cost(
            "custom-model",
            {
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "reasoning_output_tokens": 10,
                "total_tokens": 130,
            },
        )

        self.assertIsNone(estimate)

    def test_collects_owner_and_target_projects_without_duplicates(self) -> None:
        project_ids = task_associated_project_ids(
            "owner",
            {
                "targets": [
                    {"project": "web"},
                    {"project_id": "api"},
                    {"project": "owner"},
                    {"project": ""},
                    "invalid",
                ]
            },
        )

        self.assertEqual(project_ids, ["api", "owner", "web"])

    def test_extracts_last_valid_turn_completed_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            events_path.write_text(
                "\n".join(
                    [
                        "不是 JSON",
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 10,
                                    "cached_input_tokens": 2,
                                    "output_tokens": 3,
                                    "reasoning_output_tokens": 1,
                                },
                            }
                        ),
                        json.dumps({"type": "item.completed", "usage": {"input_tokens": 999, "output_tokens": 999}}),
                        json.dumps(
                            {
                                "type": "turn.completed",
                                "usage": {
                                    "input_tokens": 120,
                                    "cached_input_tokens": 40,
                                    "output_tokens": 30,
                                    "reasoning_output_tokens": 12,
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            usage = extract_token_usage(events_path)

        self.assertEqual(
            usage,
            {
                "input_tokens": 120,
                "cached_input_tokens": 40,
                "output_tokens": 30,
                "reasoning_output_tokens": 12,
                "total_tokens": 150,
            },
        )

    def test_rejects_invalid_usage_without_raising(self) -> None:
        invalid_usages = [
            {"input_tokens": True, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0},
            {"input_tokens": -1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0},
            {"input_tokens": 5, "cached_input_tokens": 6, "output_tokens": 1, "reasoning_output_tokens": 0},
            {"input_tokens": 5, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 2},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            events_path.write_text(
                "\n".join(json.dumps({"type": "turn.completed", "usage": usage}) for usage in invalid_usages),
                encoding="utf-8",
            )

            usage = extract_token_usage(events_path)

        self.assertIsNone(usage)

    def test_missing_events_file_has_no_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(extract_token_usage(Path(tmp) / "missing.jsonl"))

    def test_extracts_claude_usage_with_cache_creation_and_cache_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / "events.jsonl"
            events_path.write_text(
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "usage": {
                            "input_tokens": 10,
                            "cache_creation_input_tokens": 4,
                            "cache_read_input_tokens": 6,
                            "output_tokens": 5,
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            usage = extract_token_usage(events_path)

        self.assertEqual(
            usage,
            {
                "input_tokens": 20,
                "cached_input_tokens": 6,
                "cache_creation_input_tokens": 4,
                "output_tokens": 5,
                "reasoning_output_tokens": 0,
                "total_tokens": 25,
            },
        )


class TaskUsageAggregationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.owner = self._profile("owner", "Owner")
        self.other = self._profile("other", "Other")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _profile(self, project_id: str, name: str) -> ProjectProfile:
        root = self.root / project_id
        state = root / ".loopforge"
        state.mkdir(parents=True)
        return ProjectProfile(project_id=project_id, name=name, root_dir=root, state_dir=state)

    @staticmethod
    def _write_jsonl(path: Path, records: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    def _write_run_artifacts(self, profile: ProjectProfile, run_id: str, *, request: Optional[dict] = None, events: str = "") -> None:
        run_dir = profile.loopforge_dir / "runs" / run_id
        run_dir.mkdir(parents=True)
        (run_dir / "events.jsonl").write_text(events, encoding="utf-8")
        if request is not None:
            (run_dir / "request.json").write_text(json.dumps(request), encoding="utf-8")

    def test_aggregates_completed_tasks_with_history_backfill_and_coverage(self) -> None:
        usage = {
            "input_tokens": 100,
            "cached_input_tokens": 20,
            "output_tokens": 30,
            "reasoning_output_tokens": 10,
        }
        self._write_run_artifacts(
            self.owner,
            "run-known",
            request={"project_id": "owner", "item": {"targets": [{"project": "web"}, {"project_id": "api"}]}},
            events=json.dumps({"type": "turn.completed", "usage": usage}) + "\n",
        )
        self._write_run_artifacts(self.owner, "run-missing", events="")
        owner_runs = [
            {
                "run_id": "run-known",
                "project_id": "owner",
                "task_id": "task-cross",
                "task_title": "跨项目任务",
                "executor": "codex_cli",
                "agent_exit_code": 0,
                "ended_at": "2026-08-28T09:00:00Z",
            },
            {
                "run_id": "run-known",
                "project_id": "owner",
                "task_id": "task-cross",
                "task_title": "跨项目任务",
                "executor": "codex_cli",
                "agent_exit_code": 0,
                "ended_at": "2026-08-28T09:00:00Z",
            },
            {
                "run_id": "run-missing",
                "project_id": "owner",
                "task_id": "task-cross",
                "task_title": "跨项目任务",
                "executor": "codex_cli",
                "agent_exit_code": 1,
                "ended_at": "2026-08-28T09:30:00Z",
            },
            {
                "run_id": "preflight",
                "project_id": "owner",
                "task_id": "task-cross",
                "task_title": "跨项目任务",
                "executor": "codex_cli",
                "status": "failed",
                "ended_at": "2026-08-28T09:40:00Z",
            },
        ]
        self._write_jsonl(self.owner.history_path, owner_runs)
        self._write_jsonl(
            self.owner.events_path,
            [
                {
                    "event_type": "task_transition",
                    "task_id": "task-cross",
                    "task_title": "跨项目任务",
                    "next_state": "completed",
                    "created_at": "2026-08-28T10:00:00Z",
                }
            ],
        )
        self._write_jsonl(
            self.other.history_path,
            [
                {
                    "run_id": "other-earlier-run",
                    "project_id": "other",
                    "task_id": "task-cross",
                    "task_title": "同名但不同任务",
                    "executor": "codex_cli",
                    "token_usage": {**usage, "total_tokens": 130},
                    "next_state": "coding",
                    "ended_at": "2026-08-28T10:30:00Z",
                },
                {
                    "run_id": "other-run",
                    "project_id": "other",
                    "task_id": "task-cross",
                    "task_title": "同名但不同任务",
                    "executor": "codex_cli",
                    "token_usage": {**usage, "total_tokens": 130},
                    "associated_project_ids": ["other"],
                    "task_state": "completed",
                    "ended_at": "2026-08-28T11:00:00Z",
                }
            ],
        )

        payload = build_task_usage([self.owner, self.other])

        self.assertEqual([(item["owner_project_id"], item["task_id"]) for item in payload["items"]], [("owner", "task-cross"), ("other", "task-cross")])
        cross = payload["items"][0]
        self.assertEqual(cross["associated_project_ids"], ["api", "owner", "web"])
        self.assertEqual(cross["coverage"], "partial")
        self.assertEqual(cross["run_count"], 2)
        self.assertEqual(cross["missing_run_count"], 1)
        self.assertEqual(cross["usage"]["total_tokens"], 130)
        self.assertEqual(build_task_usage([self.owner, self.other], project_id="web")["items"], [cross])
        latest = build_task_usage([self.owner, self.other], limit=1)["items"][0]
        self.assertEqual(latest["owner_project_id"], "other")
        self.assertEqual(latest["run_count"], 2)
        self.assertEqual(latest["usage"]["total_tokens"], 260)

    def test_uses_each_runs_recorded_model_and_reasoning_for_task_cost(self) -> None:
        sol_usage = {
            "input_tokens": 1000,
            "cached_input_tokens": 200,
            "output_tokens": 100,
            "reasoning_output_tokens": 40,
        }
        luna_usage = {
            "input_tokens": 1000,
            "cached_input_tokens": 0,
            "output_tokens": 100,
            "reasoning_output_tokens": 20,
        }
        self._write_run_artifacts(
            self.owner,
            "run-sol",
            request={
                "project_id": "owner",
                "codex_model": "gpt-5.6-sol",
                "codex_reasoning_effort": "high",
                "item": {"id": "priced-task"},
            },
        )
        self._write_run_artifacts(
            self.owner,
            "run-luna",
            request={
                "project_id": "owner",
                "codex_model": "gpt-5.6-luna",
                "codex_reasoning_effort": "medium",
                "item": {"id": "priced-task"},
            },
        )
        self._write_jsonl(
            self.owner.history_path,
            [
                {
                    "run_id": "run-sol",
                    "task_id": "priced-task",
                    "task_title": "按实际模型估价",
                    "executor": "codex_cli",
                    "token_usage": sol_usage,
                    "codex_model": "gpt-5.6-sol",
                    "codex_reasoning_effort": "high",
                    "next_state": "coding",
                    "ended_at": "2026-08-29T10:00:00Z",
                },
                {
                    "run_id": "run-luna",
                    "task_id": "priced-task",
                    "task_title": "按实际模型估价",
                    "executor": "codex_cli",
                    "token_usage": luna_usage,
                    "next_state": "completed",
                    "ended_at": "2026-08-29T10:30:00Z",
                },
            ],
        )

        item = build_task_usage([self.owner])["items"][0]

        self.assertEqual(
            item["model_runs"],
            [
                {
                    "provider": "codex",
                    "model": "gpt-5.6-luna",
                    "reasoning_effort": "medium",
                    "run_count": 1,
                    "usage_run_count": 1,
                    "priced_run_count": 1,
                    "reported_cost_usd": None,
                },
                {
                    "provider": "codex",
                    "model": "gpt-5.6-sol",
                    "reasoning_effort": "high",
                    "run_count": 1,
                    "usage_run_count": 1,
                    "priced_run_count": 1,
                    "reported_cost_usd": None,
                },
            ],
        )
        self.assertEqual(item["cost_estimate"]["coverage"], "complete")
        self.assertEqual(item["cost_estimate"]["minimum_usd"], 0.0056)
        self.assertEqual(item["cost_estimate"]["maximum_usd"], 0.0056)
        self.assertEqual(item["cost_estimate"]["priced_run_count"], 2)
        self.assertEqual(item["cost_estimate"]["missing_run_count"], 0)
        self.assertEqual(item["cost_estimate"]["basis"], "api_equivalent")

    def test_keeps_model_identity_but_marks_unknown_model_cost_unavailable(self) -> None:
        self._write_run_artifacts(
            self.owner,
            "run-custom",
            request={
                "project_id": "owner",
                "codex_model": "custom-model",
                "codex_reasoning_effort": "xhigh",
                "item": {"id": "custom-task"},
            },
        )
        self._write_jsonl(
            self.owner.history_path,
            [
                {
                    "run_id": "run-custom",
                    "task_id": "custom-task",
                    "task_title": "未知模型",
                    "executor": "codex_cli",
                    "token_usage": {
                        "input_tokens": 100,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 10,
                    },
                    "next_state": "completed",
                    "ended_at": "2026-08-29T11:00:00Z",
                }
            ],
        )

        item = build_task_usage([self.owner])["items"][0]

        self.assertEqual(item["model_runs"][0]["model"], "custom-model")
        self.assertEqual(item["model_runs"][0]["reasoning_effort"], "xhigh")
        self.assertEqual(item["cost_estimate"]["coverage"], "unavailable")
        self.assertIsNone(item["cost_estimate"]["minimum_usd"])
        self.assertEqual(item["cost_estimate"]["missing_run_count"], 1)

    def test_claude_runs_keep_provider_usage_without_openai_price_estimate(self) -> None:
        self._write_run_artifacts(
            self.owner,
            "run-claude",
            request={
                "project_id": "owner",
                "worker_provider": "claude",
                "worker_model": "sonnet",
                "worker_settings": {"permission_mode": "bypassPermissions"},
                "item": {"id": "claude-task"},
            },
            events=json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "total_cost_usd": 0.015,
                    "usage": {
                        "input_tokens": 10,
                        "cache_creation_input_tokens": 4,
                        "cache_read_input_tokens": 6,
                        "output_tokens": 5,
                    },
                }
            )
            + "\n",
        )
        self._write_jsonl(
            self.owner.history_path,
            [
                {
                    "run_id": "run-claude",
                    "task_id": "claude-task",
                    "task_title": "Claude 任务",
                    "executor": "claude_cli",
                    "worker_provider": "claude",
                    "worker_model": "sonnet",
                    "reported_cost_usd": 0.015,
                    "next_state": "completed",
                    "ended_at": "2026-09-20T10:00:00Z",
                }
            ],
        )

        item = build_task_usage([self.owner])["items"][0]

        self.assertEqual(item["coverage"], "complete")
        self.assertEqual(item["usage"]["total_tokens"], 25)
        self.assertEqual(item["usage"]["cache_creation_input_tokens"], 4)
        self.assertEqual(item["model_runs"][0]["provider"], "claude")
        self.assertEqual(item["model_runs"][0]["model"], "sonnet")
        self.assertEqual(item["model_runs"][0]["reported_cost_usd"], 0.015)
        self.assertEqual(item["cost_estimate"]["coverage"], "unavailable")
        self.assertIsNone(item["cost_estimate"]["minimum_usd"])

    def test_unavailable_task_keeps_unknown_value_and_abandoned_is_excluded(self) -> None:
        self._write_run_artifacts(self.owner, "unknown-run", events="")
        self._write_jsonl(
            self.owner.history_path,
            [
                {
                    "run_id": "unknown-run",
                    "task_id": "unknown",
                    "task_title": "未知用量",
                    "executor": "codex_cli",
                    "agent_exit_code": 1,
                    "next_state": "completed",
                    "ended_at": "2026-08-28T10:00:00Z",
                },
                {
                    "run_id": "abandoned-run",
                    "task_id": "abandoned",
                    "task_title": "已放弃",
                    "executor": "codex_cli",
                    "token_usage": {"input_tokens": 1, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0},
                    "next_state": "abandoned",
                    "ended_at": "2026-08-28T11:00:00Z",
                },
            ],
        )
        self._write_jsonl(
            self.owner.events_path,
            [
                {"event_type": "task_transition", "task_id": "unknown", "next_state": "completed", "created_at": "2026-08-28T10:00:00Z"},
                {"event_type": "task_transition", "task_id": "abandoned", "next_state": "abandoned", "created_at": "2026-08-28T11:00:00Z"},
            ],
        )

        payload = build_task_usage([self.owner])

        self.assertEqual([item["task_id"] for item in payload["items"]], ["unknown"])
        self.assertEqual(payload["items"][0]["coverage"], "unavailable")
        self.assertIsNone(payload["items"][0]["usage"])
        self.assertEqual(payload["items"][0]["missing_run_count"], 1)

    def test_history_errors_are_isolated_and_artifact_paths_cannot_escape(self) -> None:
        outside = self.root / "outside-events.jsonl"
        outside.write_text(
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 999, "cached_input_tokens": 0, "output_tokens": 1, "reasoning_output_tokens": 0},
                }
            ),
            encoding="utf-8",
        )
        self.owner.history_path.write_text(
            "非法 JSON\n"
            + json.dumps(
                {
                    "run_id": "../escape",
                    "task_id": "safe-task",
                    "task_title": "安全任务",
                    "executor": "codex_cli",
                    "log_path": str(outside),
                    "next_state": "completed",
                    "ended_at": "2026-08-28T12:00:00Z",
                }
            )
            + "\n",
            encoding="utf-8",
        )

        payload = build_task_usage([self.owner])

        self.assertEqual(payload["items"][0]["coverage"], "unavailable")
        self.assertEqual(payload["items"][0]["run_count"], 0)
        self.assertGreaterEqual(len(payload["issues"]), 2)


if __name__ == "__main__":
    unittest.main()
