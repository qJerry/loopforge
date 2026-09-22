from __future__ import annotations

import tempfile
import unittest
import json
import os
from pathlib import Path
from unittest.mock import patch

from loopforge.cancel import write_cancel_request
from loopforge.config import ProjectProfile
from loopforge.executor import build_prompt, codex_command, run_executor
from loopforge.worker_adapters import build_worker_command, parse_worker_output
from loopforge.worker_result import worker_result_decision


class ExecutorTests(unittest.TestCase):
    def test_run_executor_injects_project_worker_proxy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake-codex-proxy"
            observed = root / "proxy-env.json"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import pathlib\n"
                "import sys\n"
                "sys.stdin.read()\n"
                f"pathlib.Path({str(observed)!r}).write_text(json.dumps({{key: os.environ.get(key) for key in "
                "['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']}))\n"
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            profile = ProjectProfile(project_id="demo", name="Demo", root_dir=root, state_dir=root / ".loopforge")
            profile.worker_network = {
                "mode": "custom",
                "http_proxy": "http://127.0.0.1:8899",
                "https_proxy": "http://127.0.0.1:8899",
                "all_proxy": "socks5://127.0.0.1:8899",
            }

            with patch("loopforge.executor.resolve_codex_binary", return_value=str(script)):
                result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-proxy")

            self.assertEqual(result["status"], "completed")
            values = json.loads(observed.read_text(encoding="utf-8"))
            self.assertEqual(set(values.values()), {
                "http://127.0.0.1:8899",
                "socks5://127.0.0.1:8899",
            })
            self.assertEqual(values["HTTP_PROXY"], values["http_proxy"])
            self.assertEqual(values["HTTPS_PROXY"], values["https_proxy"])
            self.assertEqual(values["ALL_PROXY"], values["all_proxy"])

    def test_run_executor_direct_mode_removes_inherited_proxy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake-codex-direct"
            observed = root / "proxy-env.json"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import pathlib\n"
                "import sys\n"
                "sys.stdin.read()\n"
                f"pathlib.Path({str(observed)!r}).write_text(json.dumps({{key: os.environ.get(key) for key in "
                "['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy']}))\n"
                "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            profile = ProjectProfile(project_id="demo", name="Demo", root_dir=root, state_dir=root / ".loopforge")
            profile.worker_network = {
                "mode": "direct",
                "http_proxy": "http://127.0.0.1:7897",
                "https_proxy": "http://127.0.0.1:7897",
                "all_proxy": "socks5://127.0.0.1:7897",
            }

            inherited = {
                "HTTP_PROXY": "http://inherited.invalid:1",
                "HTTPS_PROXY": "http://inherited.invalid:1",
                "ALL_PROXY": "socks5://inherited.invalid:1",
                "http_proxy": "http://inherited.invalid:1",
                "https_proxy": "http://inherited.invalid:1",
                "all_proxy": "socks5://inherited.invalid:1",
            }
            with patch.dict(os.environ, inherited), patch("loopforge.executor.resolve_codex_binary", return_value=str(script)):
                result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-direct")

            self.assertEqual(result["status"], "completed")
            self.assertTrue(all(value is None for value in json.loads(observed.read_text(encoding="utf-8")).values()))

    def test_claude_command_uses_non_interactive_stream_json_and_provider_permission(self) -> None:
        profile = ProjectProfile(
            project_id="demo",
            name="Demo",
            root_dir=Path("/workspace/demo"),
            executor="claude_cli",
            claude_model="opus",
            claude_permission_mode="acceptEdits",
        )

        command = build_worker_command(profile, "/workspace/task", binary="/usr/local/bin/claude")
        preview_command = build_worker_command(
            profile,
            "/workspace/task",
            mode="readonly",
            binary="/usr/local/bin/claude",
        )

        self.assertEqual(command[0], "/usr/local/bin/claude")
        self.assertIn("-p", command)
        self.assertIn("--verbose", command)
        self.assertEqual(command[command.index("--output-format") + 1], "stream-json")
        self.assertEqual(command[command.index("--model") + 1], "opus")
        self.assertEqual(command[command.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(preview_command[preview_command.index("--permission-mode") + 1], "plan")
        self.assertEqual(preview_command[preview_command.index("--tools") + 1], "")

    def test_claude_stream_json_success_returns_final_message_usage_and_cost(self) -> None:
        stdout = "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init", "session_id": "session-1"}),
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": False,
                        "result": "任务已完成",
                        "session_id": "session-1",
                        "total_cost_usd": 0.012,
                        "usage": {
                            "input_tokens": 10,
                            "cache_creation_input_tokens": 4,
                            "cache_read_input_tokens": 6,
                            "output_tokens": 5,
                        },
                    }
                ),
            ]
        )

        outcome = parse_worker_output("claude_cli", stdout, "", 0)

        self.assertEqual(outcome.status, "completed")
        self.assertEqual(outcome.last_message, "任务已完成")
        self.assertEqual(outcome.session_id, "session-1")
        self.assertEqual(outcome.reported_cost_usd, 0.012)
        self.assertEqual(outcome.provider_usage["cache_read_input_tokens"], 6)

    def test_claude_stream_json_business_error_fails_even_with_zero_exit_code(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "Invalid API key · Please run /login",
                "session_id": "session-auth",
                "usage": {},
            }
        )

        outcome = parse_worker_output("claude_cli", stdout, "", 0)

        self.assertEqual(outcome.status, "failed")
        self.assertIn("认证失败", outcome.summary)
        self.assertIn("claude", outcome.required_action)
        self.assertIn("登录", outcome.required_action)

    def test_claude_authentication_failure_is_actionable_with_nonzero_exit_code(self) -> None:
        stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "Authentication failed. Please run /login",
                "session_id": "session-auth",
                "usage": {},
            }
        )

        outcome = parse_worker_output("claude_cli", stdout, "", 1)

        self.assertEqual(outcome.status, "failed")
        self.assertIn("认证失败", outcome.summary)
        self.assertIn("登录", outcome.required_action)
        self.assertEqual(outcome.session_id, "session-auth")

    def test_claude_stream_json_rejects_missing_result_invalid_json_and_nonzero_exit(self) -> None:
        missing = parse_worker_output("claude_cli", json.dumps({"type": "system", "subtype": "init"}), "", 0)
        invalid = parse_worker_output("claude_cli", "not-json", "", 0)
        nonzero = parse_worker_output("claude_cli", json.dumps({"type": "result", "is_error": False, "subtype": "success"}), "boom", 2)

        self.assertEqual(missing.status, "failed")
        self.assertIn("最终 result", missing.summary)
        self.assertEqual(invalid.status, "failed")
        self.assertIn("JSON", invalid.summary)
        self.assertEqual(nonzero.status, "failed")
        self.assertIn("boom", nonzero.summary)

    def test_prompt_uses_selected_worker_provider_name(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."), executor="claude_cli")

        prompt = build_prompt(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-1")

        self.assertIn("Claude worker", prompt)
        self.assertNotIn("codex_cli worker", prompt)

    def test_prompt_traverses_authority_phases_without_preloading_later_docs(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."))
        item = {
            "id": "task-phases",
            "title": "分阶段实现",
            "status": "spec_ready",
            "docs": {
                "revision": "a" * 40,
                "phases": [
                    {
                        "id": "exchange-rate",
                        "title": "汇率同步",
                        "after": ["review"],
                        "module": "exchange-rate",
                        "requirements": ["docs/exchange-rate/requirements.md"],
                        "design": ["docs/exchange-rate/design.md"],
                        "specs": ["docs/exchange-rate/specs.md"],
                    },
                    {
                        "id": "review",
                        "title": "审核策略",
                        "after": [],
                        "module": "review",
                        "requirements": ["docs/review/requirements.md"],
                        "design": ["docs/review/design.md"],
                        "specs": ["docs/review/specs.md"],
                    },
                ],
                "supporting": ["docs/shared/glossary.md"],
            },
        }

        prompt = build_prompt(profile, item, "run-phases")

        self.assertIn("权威文档阶段", prompt)
        self.assertLess(prompt.index("Phase review"), prompt.index("Phase exchange-rate"))
        self.assertIn("docs/review/requirements.md", prompt)
        self.assertIn("docs/review/design.md", prompt)
        self.assertIn("docs/review/specs.md", prompt)
        self.assertIn("只读取当前 Phase", prompt)
        self.assertIn("不得预读后续 Phase", prompt)
        self.assertIn("自动进入下一 Phase", prompt)
        self.assertIn("docs/shared/glossary.md", prompt)
        self.assertIn("按需读取", prompt)

    def test_prompt_routes_parent_and_target_documents_to_their_feature_worktrees(self) -> None:
        profile = ProjectProfile(project_id="ad", name="AD", root_dir=Path("/workspace/ad"))
        item = {
            "id": "task-offer-feed",
            "title": "Offer Feed",
            "status": "spec_ready",
            "docs": {
                "revision": "a" * 40,
                "phases": [
                    {
                        "id": "platform",
                        "module": "platform",
                        "requirements": ["docs/platform/requirements.md"],
                        "design": ["docs/platform/design.md"],
                        "specs": ["docs/platform/specs.md"],
                        "bdd_exemption": {"reason": "父级文档", "test_refs": ["tests/docs.py"]},
                    }
                ],
            },
            "targets": [
                {
                    "id": "api",
                    "project": "ad-api-go",
                    "docs": {
                        "revision": "b" * 40,
                        "phases": [
                            {
                                "id": "runtime",
                                "module": "affiliate",
                                "requirements": ["docs/affiliate/requirements.md"],
                                "design": ["docs/affiliate/design.md"],
                                "specs": ["docs/affiliate/specs.md"],
                                "bdd": ["bdd/features/affiliate/runtime.feature"],
                            }
                        ],
                    },
                }
            ],
            "authority_roots": {
                "project": {
                    "root": "/worktrees/ad/task-offer-feed-ad",
                    "revision": "a" * 40,
                },
                "targets": {
                    "api": {
                        "root": "/worktrees/ad-api-go/task-offer-feed-api",
                        "revision": "b" * 40,
                    }
                },
            },
        }

        prompt = build_prompt(profile, item, "run-authority")

        self.assertIn("文档根目录：/worktrees/ad/task-offer-feed-ad", prompt)
        self.assertIn(f"文档版本：{'a' * 40}", prompt)
        self.assertIn("文档根目录：/worktrees/ad-api-go/task-offer-feed-api", prompt)
        self.assertIn(f"文档版本：{'b' * 40}", prompt)
        self.assertIn("实现与文档回填目录：/worktrees/ad-api-go/task-offer-feed-api", prompt)

    def test_run_executor_persists_codex_token_usage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake-real-codex"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'type': 'turn.completed', 'usage': "
                "{'input_tokens': 100, 'cached_input_tokens': 25, 'output_tokens': 40, 'reasoning_output_tokens': 10}}))\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                state_dir=root / ".loopforge",
                executor="codex_cli",
                codex_model="gpt-5.6-sol",
                codex_reasoning_effort="high",
            )

            with patch("loopforge.executor.resolve_codex_binary", return_value=str(script)):
                result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-usage")

            result_artifact = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
            request_artifact = json.loads(Path(result["artifacts"]["request_path"]).read_text(encoding="utf-8"))

        expected = {
            "input_tokens": 100,
            "cached_input_tokens": 25,
            "output_tokens": 40,
            "reasoning_output_tokens": 10,
            "total_tokens": 140,
        }
        self.assertEqual(result["token_usage"], expected)
        self.assertEqual(result_artifact["token_usage"], expected)
        self.assertEqual(request_artifact["codex_model"], "gpt-5.6-sol")
        self.assertEqual(request_artifact["codex_reasoning_effort"], "high")

    def test_run_executor_persists_claude_stream_result_and_worker_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake-claude"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'type':'result','subtype':'success','is_error':False,'result':'Claude 完成',"
                "'session_id':'session-ok','total_cost_usd':0.02,'usage':{'input_tokens':7,'output_tokens':3}}))\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                state_dir=root / ".loopforge",
                executor="claude_cli",
                default_agent="claude",
                claude_model="sonnet",
                claude_permission_mode="bypassPermissions",
            )

            with patch("loopforge.executor.resolve_claude_binary", return_value=str(script)):
                result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-claude")

            request = json.loads(Path(result["artifacts"]["request_path"]).read_text(encoding="utf-8"))
            result_artifact = json.loads(Path(result["result_path"]).read_text(encoding="utf-8"))
            last_message = Path(result["last_message_path"]).read_text(encoding="utf-8")
            events = Path(result["log_path"]).read_text(encoding="utf-8")

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["session_id"], "session-ok")
        self.assertEqual(result["reported_cost_usd"], 0.02)
        self.assertEqual(last_message, "Claude 完成")
        self.assertIn('"type": "result"', events)
        self.assertEqual(request["worker_provider"], "claude")
        self.assertEqual(request["worker_model"], "sonnet")
        self.assertEqual(request["worker_settings"], {"permission_mode": "bypassPermissions"})
        self.assertNotIn("codex_model", request)
        self.assertEqual(result_artifact["provider_usage"]["input_tokens"], 7)

    def test_run_executor_treats_claude_authentication_result_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "fake-claude-auth"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'type':'result','subtype':'error_during_execution','is_error':True,"
                "'result':'Invalid API key · Please run /login','session_id':'session-auth','usage':{}}))\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                state_dir=root / ".loopforge",
                executor="claude_cli",
                default_agent="claude",
            )

            with patch("loopforge.executor.resolve_claude_binary", return_value=str(script)):
                result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-auth")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 0)
        self.assertIn("认证失败", result["summary"])
        self.assertIn("登录", result["required_action"])

    def test_run_executor_returns_actionable_failure_when_claude_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                state_dir=root / ".loopforge",
                executor="claude_cli",
                default_agent="claude",
            )

            with patch("loopforge.executor.resolve_claude_binary", return_value=str(root / "missing-claude")):
                result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-missing")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("找不到 claude 可执行文件", result["summary"])
        self.assertIn("LOOPFORGE_CLAUDE_BIN", result["stderr"])


    def test_codex_command_uses_known_app_binary_when_path_misses_codex(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."))

        def exists_only_for_chatgpt_app(path: Path) -> bool:
            return str(path) == "/Applications/ChatGPT.app/Contents/Resources/codex"

        with patch("loopforge.executor.shutil.which", return_value=None), patch(
            "loopforge.executor.Path.exists",
            exists_only_for_chatgpt_app,
        ):
            command = codex_command(profile, "run-1")

        self.assertEqual(command[0], "/Applications/ChatGPT.app/Contents/Resources/codex")
        self.assertIn("--model", command)
        self.assertIn("gpt-5-codex", command)
        self.assertIn('model_reasoning_effort="high"', command)

    def test_run_executor_returns_actionable_failure_when_codex_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                state_dir=root / ".loopforge",
                executor="codex_cli",
            )

            with patch("loopforge.executor.resolve_codex_binary", return_value=str(root / "missing-codex")):
                result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "claimed"}, "run-1")

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["exit_code"], 127)
        self.assertIn("找不到 codex 可执行文件", result["summary"])
        self.assertIn("LOOPFORGE_CODEX_BIN", result["stderr"])

    def test_run_executor_honors_cancel_request(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / "sleeping-codex"
            script.write_text(
                "#!/usr/bin/env python3\n"
                "import sys\n"
                "import time\n"
                "sys.stdin.read()\n"
                "time.sleep(10)\n"
                "print('done')\n",
                encoding="utf-8",
            )
            script.chmod(0o755)
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                state_dir=root / ".loopforge",
                executor="codex_cli",
            )
            write_cancel_request(profile, "run-cancel", "测试取消")

            with patch("loopforge.executor.resolve_codex_binary", return_value=str(script)):
                result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-cancel")

        self.assertEqual(result["status"], "cancelled")
        self.assertIn("取消请求", result["summary"])
        self.assertEqual(result["cancel_request"]["reason"], "测试取消")

    def test_auto_commit_prompt_requires_project_commit_rule(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."), auto_commit=True)

        prompt = build_prompt(profile, {"id": "task-1", "title": "Task", "status": "claimed"}, "run-1")

        self.assertIn("LoopForge 会在本轮成功后执行安全自动 commit", prompt)
        self.assertIn("不要主动提交", prompt)

    def test_prompt_treats_items_as_complex_by_default(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."))

        prompt = build_prompt(profile, {"id": "task-1", "title": "Task", "status": "claimed"}, "run-1")

        self.assertIn("当前 item 规划级别：complex", prompt)
        self.assertIn("默认把 item 视为复杂任务", prompt)
        self.assertIn("prd.md、design.md、implement.md", prompt)
        self.assertIn("不是仅创建文档后收尾", prompt)

    def test_prompt_allows_explicit_lightweight_planning_level(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."))

        prompt = build_prompt(
            profile,
            {"id": "task-1", "title": "Task", "status": "claimed", "planning_level": "lightweight"},
            "run-1",
        )

        self.assertIn("当前 item 规划级别：lightweight", prompt)
        self.assertIn("只有 item.planning_level 明确为 lightweight 时，才允许 requirements-only", prompt)
        self.assertIn("至少补齐 prd.md", prompt)

    def test_prompt_includes_human_resolution_as_latest_decision(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."))

        prompt = build_prompt(
            profile,
            {
                "id": "task-1",
                "title": "Task",
                "status": "prd_ready",
                "resolved_reason": "允许先使用 mock 返回对齐页面字段",
                "resolved_at": "2026-07-03T08:00:00+00:00",
                "previous_blocked_status": "prd_blocked",
            },
            "run-1",
        )

        self.assertIn("人工处理说明", prompt)
        self.assertIn("允许先使用 mock 返回对齐页面字段", prompt)
        self.assertIn("最新人工决策", prompt)
        self.assertIn("不要仅因旧 blocker 再次停在同一个阻塞点", prompt)

    def test_prompt_includes_instruction_for_this_run(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."))

        prompt = build_prompt(
            profile,
            {
                "id": "task-1",
                "title": "Task",
                "status": "coding",
                "run_instruction": "继续现有实现，不要运行阻塞任务以外的任务",
            },
            "run-1",
        )

        self.assertIn("本轮继续说明", prompt)
        self.assertIn("继续现有实现，不要运行阻塞任务以外的任务", prompt)
        self.assertIn("仅对本轮运行有效", prompt)

    def test_prompt_includes_provider_neutral_continuation_context(self) -> None:
        profile = ProjectProfile(project_id="demo", name="Demo", root_dir=Path("."), executor="claude_cli")
        item = {
            "id": "task-1",
            "title": "Task",
            "status": "coding",
            "continuation_context": {
                "previous_run_id": "run-old",
                "complete": True,
                "summary": "Codex 已完成配置层，执行测试时额度耗尽",
                "validation": {"status": "failed", "commands": ["python3 -m unittest tests.test_config_runner"]},
                "blockers": [{"type": "quota_exhausted", "summary": "额度耗尽"}],
                "worktree": {"path": "/workspace/task", "branch": "feature/task", "status": "dirty"},
            },
        }

        prompt = build_prompt(profile, item, "run-new")

        self.assertIn("跨模型继续上下文", prompt)
        self.assertIn("run-old", prompt)
        self.assertIn("Codex 已完成配置层", prompt)
        self.assertIn("先检查已有实现", prompt)
        self.assertIn("不得重置 worktree", prompt)

    def test_run_executor_builds_complete_continuation_context_from_previous_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".loopforge"
            old_run_dir = state / "runs" / "run-old"
            old_run_dir.mkdir(parents=True)
            (old_run_dir / "worker-result.json").write_text(
                json.dumps(
                    {
                        "status": "blocked",
                        "summary": "已完成配置层",
                        "recommended_status": "dev_blocked",
                        "validation": {"status": "failed", "commands": ["python3 -m unittest"]},
                        "blockers": [{"type": "quota_exhausted", "summary": "额度耗尽"}],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (state / "index.jsonl").write_text(
                json.dumps(
                    {
                        "run_id": "run-old",
                        "task_id": "task-1",
                        "status": "blocked",
                        "summary": "旧运行阻塞",
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                state_dir=state,
                executor="fake_codex",
            )

            result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-new")

            request = json.loads(Path(result["artifacts"]["request_path"]).read_text(encoding="utf-8"))
            prompt = Path(result["artifacts"]["prompt_path"]).read_text(encoding="utf-8")

        context = request["continuation_context"]
        self.assertTrue(context["complete"])
        self.assertEqual(context["previous_run_id"], "run-old")
        self.assertEqual(context["summary"], "已完成配置层")
        self.assertEqual(context["validation"]["status"], "failed")
        self.assertEqual(context["blockers"][0]["type"], "quota_exhausted")
        self.assertIn("跨模型继续上下文", prompt)

    def test_run_executor_marks_continuation_context_incomplete_without_worker_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / ".loopforge"
            old_run_dir = state / "runs" / "run-old"
            old_run_dir.mkdir(parents=True)
            (old_run_dir / "last-message.md").write_text("执行中断前已修改 executor.py", encoding="utf-8")
            (state / "index.jsonl").write_text(
                json.dumps(
                    {
                        "run_id": "run-old",
                        "task_id": "task-1",
                        "status": "failed",
                        "summary": "模型额度耗尽",
                        "diagnostic": {"failure_kind": "quota_exhausted", "next_action": "更换模型"},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            profile = ProjectProfile(
                project_id="demo",
                name="Demo",
                root_dir=root,
                state_dir=state,
                executor="fake_codex",
            )

            result = run_executor(profile, {"id": "task-1", "title": "Task", "status": "coding"}, "run-new")
            request = json.loads(Path(result["artifacts"]["request_path"]).read_text(encoding="utf-8"))

        context = request["continuation_context"]
        self.assertFalse(context["complete"])
        self.assertEqual(context["summary"], "模型额度耗尽")
        self.assertEqual(context["diagnostic"]["failure_kind"], "quota_exhausted")
        self.assertIn("已修改 executor.py", context["last_message_excerpt"])

    def test_prompt_forbids_worker_from_writing_dev_task_and_requires_target_results(self) -> None:
        profile = ProjectProfile(project_id="ad", name="AD", root_dir=Path("."))
        item = {
            "id": "task-1",
            "title": "Target task",
            "status": "spec_ready",
            "target_plan": {
                "explicit": True,
                "ordered_target_ids": ["admin-ui"],
                "targets": [
                    {
                        "id": "admin-ui",
                        "project": "ad-admin-react",
                        "repo_path": "/tmp/ad-admin-react",
                        "branch": "",
                        "target_branch": "main",
                        "after": [],
                    }
                ],
            },
        }

        prompt = build_prompt(profile, item, "run-1")

        self.assertIn("单任务 JSON 对 worker 只读", prompt)
        self.assertIn("worker-result 必须包含 targets", prompt)
        self.assertIn("本轮最多推进到 ready_for_review", prompt)
        self.assertNotIn("可以推进当前 item 的 status", prompt)
        self.assertNotIn("确保 data/dev-task.json 中该 item 的 status", prompt)

    def test_prompt_requires_non_empty_summary_for_each_target_result(self) -> None:
        profile = ProjectProfile(project_id="ad", name="AD", root_dir=Path("."))
        item = {
            "id": "task-1",
            "title": "Target task",
            "status": "spec_ready",
            "target_plan": {
                "explicit": True,
                "ordered_target_ids": ["admin-ui"],
                "targets": [{"id": "admin-ui", "project": "ad-admin-react"}],
            },
            "targets": [
                {
                    "id": "admin-ui",
                    "project": "ad-admin-react",
                    "branch": "feature/ad/task-1/admin-ui",
                    "worktree_path": "/tmp/admin-ui",
                }
            ],
        }

        prompt = build_prompt(profile, item, "run-1")
        worker_result = {
            "status": "completed",
            "recommended_status": "ready_for_review",
            "targets": [
                {
                    "id": "admin-ui",
                    "status": "completed",
                    "branch": "feature/ad/task-1/admin-ui",
                    "worktree_path": "/tmp/admin-ui",
                    "validation": {"status": "passed", "commands": ["npm test"]},
                }
            ],
        }

        self.assertIn("非空执行摘要 summary", prompt)
        self.assertIn("非空 branch、非空 worktree_path 和 validation", prompt)
        self.assertIn("status 必须为 completed", prompt)
        self.assertIn("validation.status 必须为 passed", prompt)
        self.assertIn("validation.commands 必须为非空命令列表", prompt)

        missing_summary = worker_result_decision(
            {"worker_result_source": "inline", "worker_result": worker_result},
            item,
        )
        worker_result["targets"][0]["summary"] = "管理端实现和门禁已完成"
        complete = worker_result_decision(
            {"worker_result_source": "inline", "worker_result": worker_result},
            item,
        )

        self.assertEqual(missing_summary["next_state"], "dev_blocked")
        self.assertIn("admin-ui.summary 缺失", missing_summary["blockers"][0]["summary"])
        self.assertEqual(complete["next_state"], "ready_for_review")


if __name__ == "__main__":
    unittest.main()
