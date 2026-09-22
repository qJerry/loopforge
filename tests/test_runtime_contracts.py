from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from loopforge.config import ProjectProfile
from loopforge.dev_tasks import normalize_item, summary as task_summary
from loopforge.executor import build_prompt
from loopforge.planning import ensure_planning_workspace, planning_context, planning_gate
from loopforge.targets import TargetPlanError, task_target_plan
from loopforge.worker_result import worker_result_decision
from loopforge.scheduler import loop_types_for_status


class RuntimeContractTests(unittest.TestCase):
    def test_ordinary_task_requires_passing_evidence_for_every_acceptance_item(self) -> None:
        task = {
            "id": "task-ordinary",
            "docs": {},
            "acceptance": ["接口返回成功", "错误输入被拒绝"],
            "acceptance_refs": [],
        }
        loaded = {
            "worker_result_source": "inline",
            "worker_result": {
                "status": "completed",
                "recommended_status": "ready_for_review",
                "validation": {"status": "passed", "commands": ["python3 -m unittest"]},
            },
        }

        missing = worker_result_decision(loaded, task)
        loaded["worker_result"]["acceptance_results"] = [
            {"index": 0, "status": "passed", "evidence": ["tests.test_api 通过"]},
            {"index": 1, "status": "passed", "evidence": ["tests.test_invalid_input 通过"]},
        ]
        passed = worker_result_decision(loaded, task)

        self.assertEqual(missing["next_state"], "dev_blocked")
        self.assertEqual(missing["blockers"][0]["type"], "acceptance_validation_failed")
        self.assertIn("缺少验收项", missing["required_action"])
        self.assertEqual(passed["next_state"], "ready_for_review")

    def test_ordinary_task_rejects_failed_or_evidenceless_acceptance_result(self) -> None:
        task = {
            "id": "task-ordinary",
            "docs": {},
            "acceptance": ["成功路径", "失败路径"],
            "acceptance_refs": [],
        }
        loaded = {
            "worker_result_source": "inline",
            "worker_result": {
                "status": "completed",
                "recommended_status": "ready_for_review",
                "acceptance_results": [
                    {"index": 0, "status": "passed", "evidence": []},
                    {"index": 1, "status": "failed", "evidence": ["仍然返回 200"]},
                ],
            },
        }

        decision = worker_result_decision(loaded, task)

        self.assertEqual(decision["next_state"], "dev_blocked")
        self.assertIn("evidence", decision["required_action"])
        self.assertIn("status 必须为 passed", decision["required_action"])


    def test_normalize_item_preserves_extensible_docs_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path(tmp))
            item = {
                "id": "task-1",
                "title": "扩展文档引用",
                "docs": {
                    "module": "orders",
                    "requirements": ["docs/orders/requirements.md"],
                    "design": ["docs/orders/design.md"],
                    "specs": ["docs/orders/specs.md"],
                    "api": ["docs/orders/api.md"],
                    "runbook": "docs/orders/runbook.md",
                },
            }

            normalized = normalize_item(item, profile)

            self.assertEqual(normalized["docs"]["api"], ["docs/orders/api.md"])
            self.assertEqual(normalized["docs"]["runbook"], "docs/orders/runbook.md")

    def test_v2_api_projection_only_exposes_acceptance_for_ordinary_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data" / "tasks").mkdir(parents=True)
            profile = ProjectProfile(project_id="demo", name="Demo", root_dir=root)
            ordinary = normalize_item(
                {
                    "id": "task-ordinary",
                    "title": "普通任务",
                    "docs": {},
                    "targets": [],
                    "acceptance": ["普通任务验收"],
                    "acceptance_refs": [],
                },
                profile,
            )
            documented = normalize_item(
                {
                    "id": "task-docs",
                    "title": "文档任务",
                    "docs": {
                        "module": "demo",
                        "requirements": ["docs/demo/requirements.md"],
                        "design": ["docs/demo/design.md"],
                        "specs": ["docs/demo/specs.md"],
                    },
                    "targets": [],
                    "acceptance": ["旧重复验收"],
                    "acceptance_refs": [],
                },
                profile,
            )

            self.assertEqual(ordinary["acceptance"], ["普通任务验收"])
            self.assertEqual(ordinary["acceptance_refs"], [])
            self.assertNotIn("acceptance", documented)
            self.assertNotIn("acceptance_refs", documented)


    def test_target_plan_exposes_scope_and_rejects_unknown_explicit_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            child = root / "admin-ui"
            child.mkdir()
            profile = ProjectProfile(
                project_id="ad",
                name="AD",
                root_dir=root,
                project_group={"children": [{"key": "admin-ui", "path": str(child)}]},
            )
            plan = task_target_plan(
                profile,
                {
                    "id": "task-1",
                    "targets": [
                        {
                            "id": "admin-ui",
                            "project": "admin-ui",
                            "scope": "完成管理端页面",
                            "after": [],
                        }
                    ],
                },
                require_repo_paths=True,
            )

            self.assertEqual(plan.to_payload()["targets"][0]["scope"], "完成管理端页面")

            with self.assertRaises(TargetPlanError) as raised:
                task_target_plan(
                    profile,
                    {"id": "task-2", "targets": [{"id": "unknown", "project": "unknown"}]},
                    require_repo_paths=True,
                )
            self.assertEqual(raised.exception.code, "target_repo_missing")

    def test_planning_context_uses_external_adapter_without_assuming_its_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                planning_adapter="trellis",
            )
            builtin = ProjectProfile(project_id="plain", name="Plain", root_dir=root)

            external_context = planning_context(external, {"id": "task-1"})
            builtin_context = planning_context(builtin, {"id": "task-2"})

            self.assertEqual(external_context["provider"], "trellis")
            self.assertEqual(external_context["mode"], "external")
            self.assertNotIn("workspace_path", external_context)
            self.assertEqual(builtin_context["provider"], "builtin")
            self.assertEqual(builtin_context["workspace_path"], str((root / ".loopforge" / "task" / "task-2").resolve()))

    def test_executor_prompt_describes_planning_capability_without_trellis_layout(self) -> None:
        profile = ProjectProfile(
            project_id="demo",
            name="Demo",
            root_dir=Path("."),
            planning_adapter="openspec",
        )

        prompt = build_prompt(profile, {"id": "task-1", "title": "通用规划", "planning_level": "complex"}, "run-1")

        self.assertIn("规划 provider：openspec", prompt)
        self.assertIn("由外部规划适配器判断规划是否完整", prompt)
        self.assertNotIn("Trellis task 目录", prompt)

    def test_executor_prompt_lists_ordinary_acceptance_result_contract(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."))

        prompt = build_prompt(
            profile,
            {
                "id": "task-ordinary",
                "title": "普通任务",
                "planning_level": "lightweight",
                "acceptance": ["成功路径通过", "错误路径通过"],
                "acceptance_refs": [],
            },
            "run-1",
        )

        self.assertIn("任务级验收项", prompt)
        self.assertIn("0. 成功路径通过", prompt)
        self.assertIn("1. 错误路径通过", prompt)
        self.assertIn("acceptance_results", prompt)

    def test_worker_result_must_match_preallocated_branch_and_worktree(self) -> None:
        task = {
            "id": "task-1",
            "targets": [
                {
                    "id": "admin-ui",
                    "project": "admin-ui",
                    "branch": "loopforge/ad/task-1/admin-ui",
                    "worktree_path": "/tmp/owned-worktree",
                }
            ],
        }
        loaded = {
            "worker_result_source": "inline",
            "worker_result": {
                "status": "completed",
                "recommended_status": "ready_for_review",
                "targets": [
                    {
                        "id": "admin-ui",
                        "status": "completed",
                        "summary": "完成",
                        "branch": "other-branch",
                        "worktree_path": "/tmp/other-worktree",
                        "validation": {"status": "passed", "commands": ["npm test"]},
                    }
                ],
            },
        }

        decision = worker_result_decision(loaded, task)

        self.assertEqual(decision["next_state"], "dev_blocked")
        self.assertIn("与预分配值不一致", decision["blockers"][0]["summary"])

    def test_builtin_and_external_planning_share_status_contract_not_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            builtin = ProjectProfile(project_id="demo", name="Demo", root_dir=root)
            item = {"id": "task-1", "planning_level": "complex"}
            workspace = ensure_planning_workspace(builtin, item)

            self.assertTrue(Path(workspace["metadata_path"]).is_file())
            self.assertFalse(planning_gate(builtin, item, {})["ready"])
            for filename in ("prd.md", "design.md", "implement.md"):
                (Path(workspace["workspace_path"]) / filename).write_text(f"# {filename}\n", encoding="utf-8")
            self.assertTrue(planning_gate(builtin, item, {})["ready"])

            external = ProjectProfile(project_id="demo", name="Demo", root_dir=root, planning_adapter="openspec")
            passed = planning_gate(
                external,
                item,
                {"planning": {"provider": "openspec", "status": "passed", "reference": "spec:task-1"}},
            )
            self.assertTrue(passed["ready"])
            self.assertNotIn("workspace_path", passed)

    def test_contract_change_required_blocks_only_current_item(self) -> None:
        decision = worker_result_decision(
            {
                "worker_result_source": "inline",
                "worker_result": {
                    "status": "needs_spec",
                    "summary": "接口合同与任务验收冲突。",
                    "contract_change_required": True,
                },
            },
            {"id": "task-1"},
        )

        self.assertEqual(decision["next_state"], "spec_blocked")
        self.assertEqual(decision["blockers"][0]["type"], "contract_change_required")

    def test_waiting_item_does_not_block_other_runnable_items(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."))
        payload = {
            "items": [
                {"id": "waiting", "title": "等待人工", "status": "spec_blocked"},
                {"id": "runnable", "title": "可继续", "status": "spec_ready"},
            ]
        }

        status = task_summary(payload, profile)

        self.assertEqual(status["active_task"]["id"], "waiting")
        self.assertEqual(status["runnable_loops"], ["dev"])
        self.assertEqual(loop_types_for_status(status), ["dev", "scan"])


if __name__ == "__main__":
    unittest.main()
