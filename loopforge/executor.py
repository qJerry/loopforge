"""Codex worker 执行器。"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

from .cancel import read_cancel_request
from .config import ProjectProfile
from .document_phases import ordered_document_phases
from .domain import utc_now
from .history import read_jsonl
from .planning import planning_context
from .token_usage import extract_token_usage
from .worker_adapters import (
    build_worker_environment,
    build_worker_command,
    parse_worker_output,
    provider_label,
    request_metadata,
)


CODEX_BINARY_CANDIDATES = (
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    Path("/Applications/Codex.app/Contents/Resources/codex"),
    Path.home() / ".local/bin/codex",
    Path("/opt/homebrew/bin/codex"),
    Path("/usr/local/bin/codex"),
)
CLAUDE_BINARY_CANDIDATES = (
    Path.home() / ".local/bin/claude",
    Path("/opt/homebrew/bin/claude"),
    Path("/usr/local/bin/claude"),
)


def build_run_dir(profile: ProjectProfile, run_id: str) -> Path:
    return profile.loopforge_dir / "runs" / run_id


def artifact_paths(profile: ProjectProfile, run_id: str) -> Dict[str, str]:
    run_dir = build_run_dir(profile, run_id)
    return {
        "run_dir": str(run_dir),
        "prompt_path": str(run_dir / "prompt.md"),
        "command_path": str(run_dir / "command.json"),
        "request_path": str(run_dir / "request.json"),
        "events_path": str(run_dir / "events.jsonl"),
        "last_message_path": str(run_dir / "last-message.md"),
        "worker_result_path": str(run_dir / "worker-result.json"),
        "result_path": str(run_dir / "result.json"),
    }


def resolve_codex_binary() -> str:
    configured = os.environ.get("LOOPFORGE_CODEX_BIN", "").strip()
    if configured:
        return configured
    resolved = shutil.which("codex")
    if resolved:
        return resolved
    for candidate in CODEX_BINARY_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return "codex"


def resolve_claude_binary() -> str:
    configured = os.environ.get("LOOPFORGE_CLAUDE_BIN", "").strip()
    if configured:
        return configured
    resolved = shutil.which("claude")
    if resolved:
        return resolved
    for candidate in CLAUDE_BINARY_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    return "claude"


def codex_command(
    profile: ProjectProfile,
    run_id: str,
    execution_root: str = "",
    *,
    sandbox: str = "",
    output_path: Path | None = None,
) -> List[str]:
    run_dir = build_run_dir(profile, run_id)
    last_message = output_path or run_dir / "last-message.md"
    if sandbox and sandbox != profile.codex_sandbox:
        profile = replace(profile, codex_sandbox=sandbox)
    return build_worker_command(
        profile,
        execution_root or str(profile.root_dir),
        binary=resolve_codex_binary(),
        last_message_path=last_message,
    )


def worker_command(
    profile: ProjectProfile,
    run_id: str,
    execution_root: str = "",
    *,
    mode: str = "development",
    output_path: Path | None = None,
) -> List[str]:
    if profile.executor == "claude_cli":
        return build_worker_command(
            profile,
            execution_root or str(profile.root_dir),
            mode=mode,
            binary=resolve_claude_binary(),
            last_message_path=output_path,
        )
    if profile.executor == "fake_codex":
        fake_profile = replace(profile, executor="codex_cli")
        return codex_command(
            fake_profile,
            run_id,
            execution_root,
            sandbox="read-only" if mode == "readonly" else "",
            output_path=output_path,
        )
    return codex_command(
        profile,
        run_id,
        execution_root,
        sandbox="read-only" if mode == "readonly" else "",
        output_path=output_path,
    )


def build_prompt(profile: ProjectProfile, item: Dict[str, Any], run_id: str) -> str:
    execution_root = str(item.get("execution_root") or profile.root_dir)
    planning_level = str(item.get("planning_level") or "complex").strip() or "complex"
    if planning_level not in {"complex", "lightweight"}:
        planning_level = "complex"
    resolved_reason = str(item.get("resolved_reason") or "").strip()
    resolved_block = ""
    if resolved_reason:
        resolved_block = f"""
人工处理说明：
- 用户已处理上一轮阻塞，说明为：{resolved_reason}
- 处理时间：{item.get("resolved_at") or "未知"}
- 上一阻塞状态：{item.get("previous_blocked_status") or "未知"}
- 本轮必须把这条说明当作最新人工决策；如果它改变了原 Requirements 或 Specs 的边界，请按当前规划 provider 同步规划文档，并在 worker-result 中明确记录任务口径差异，不要直接改写任务 JSON。
- 除非该说明违反项目 AGENTS.md、安全边界或明确不可实现，否则不要仅因旧 blocker 再次停在同一个阻塞点。
"""
    run_instruction = str(item.get("run_instruction") or "").strip()
    run_instruction_block = ""
    if run_instruction:
        run_instruction_block = f"""
本轮继续说明：
- 用户补充说明：{run_instruction}
- 该说明仅对本轮运行有效，优先于历史运行摘要，但不得突破项目 AGENTS.md、安全边界或当前权威文档合同。
"""
    review_feedback = str(item.get("review_feedback") or "").strip()
    review_feedback_type = str(item.get("review_feedback_type") or "").strip()
    review_block = ""
    if review_feedback and review_feedback_type in {"code", "spec"}:
        feedback_label = "代码问题" if review_feedback_type == "code" else "需求或规则问题"
        review_block = f"""
最近一次验收驳回：
- 驳回类型：{feedback_label}
- 用户反馈：{review_feedback}
- 驳回时间：{item.get("reviewed_at") or "未知"}
- 本轮必须把这条反馈当作最新验收门禁。代码问题驳回时直接修代码和验证；需求或规则问题驳回时先同步模块三件套，再继续开发。
"""
    continuation_block = _continuation_prompt_block(item)
    target_plan = item.get("target_plan") if isinstance(item.get("target_plan"), dict) else {}
    authority_block = _authority_prompt_block(item)
    acceptance_block = _acceptance_prompt_block(item)
    planning = planning_context(profile, item)
    if planning["mode"] == "external":
        planning_rule = (
            "由外部规划适配器判断规划是否完整；遵循项目自己的工件布局，不得假设或创建固定目录；"
            f"worker-result.planning 必须返回 provider={planning['provider']}、status=passed 和非空 reference。"
        )
    else:
        planning_rule = (
            f"由 LoopForge builtin planning 管理 {planning['workspace_path']}；"
            "复杂任务需具备 prd.md、design.md、implement.md。"
        )
    target_block = ""
    target_rows = target_plan.get("targets") if isinstance(target_plan.get("targets"), list) else []
    explicit_targets = target_plan.get("explicit") is True
    commit_rule = (
        "每个 Target 必须在自己的 feature branch 形成本地 commit；不要提交父项目根目录，执行结果通过 worker-result 返回。"
        if profile.worktree_managed or explicit_targets
        else (
            "LoopForge 会在本轮成功后执行安全自动 commit；你只需完成改动和验证，不要主动提交，除非项目文档强制要求由 agent 自行提交。"
            if profile.auto_commit
            else "禁止提交 commit；只保留工作区改动，执行结果通过 worker-result 返回。"
        )
    )
    if explicit_targets and target_rows:
        ordered_ids = target_plan.get("ordered_target_ids") if isinstance(target_plan.get("ordered_target_ids"), list) else []
        lines = []
        for target in target_rows:
            if not isinstance(target, dict):
                continue
            after_values = target.get("after") if isinstance(target.get("after"), list) else []
            lines.append(
                "- "
                f"id={target.get('id') or ''}；"
                f"project={target.get('project') or ''}；"
                f"repo_path={target.get('repo_path') or ''}；"
                f"scope={target.get('scope') or ''}；"
                f"branch={target.get('branch') or '待 worker 创建或回填'}；"
                f"target_branch={target.get('target_branch') or 'main'}；"
                f"after={', '.join(after_values) if after_values else '无'}"
            )
        target_block = f"""
父 task 多 target：
- 执行顺序：{', '.join(str(value) for value in ordered_ids) if ordered_ids else '按 targets 原顺序'}
{chr(10).join(lines)}
- 父项目触发子仓执行时，子仓不得创建镜像 task；只在对应 repo_path 产生 branch、worktree、代码/文档改动和 commit。
- worker-result 必须包含 targets；每个 Target 结果必须包含与预分配 Target 对应的 id、非空执行摘要 summary、非空 branch、非空 worktree_path 和 validation。
- 每个 Target 的 status 必须为 completed、validation.status 必须为 passed，且 validation.commands 必须为非空命令列表。
- 所有 Target 结果完整并验证通过后，才允许 recommended_status=ready_for_review；commit 由运行时在合入前直接读取 branch HEAD 校验。
"""
    return f"""你是 LoopForge 调用的 {provider_label(profile)} worker。

工作目录：{execution_root}
本轮 run_id：{run_id}
任务源：data/tasks/{item.get("id")}.json
当前 item id：{item.get("id")}
当前 item 标题：{item.get("title")}
当前 item 状态：{item.get("status")}
当前 item 规划级别：{planning_level}
规划 provider：{planning['provider']}
规划规则：{planning_rule}
{resolved_block}
{run_instruction_block}
{review_block}
{continuation_block}
{target_block}
{authority_block}
{acceptance_block}

边界：
- 本轮只处理 data/tasks/{item.get("id")}.json 对应的任务。
- 不得领取、修改或完成其他 item。
- 单任务 JSON 对 worker 只读；不得直接修改任何任务字段或 status。LoopForge 是执行期唯一 writer。
- 如果完成开发或发现阻塞，请优先写入 .loopforge/runs/{run_id}/worker-result.json，内容为 JSON object，至少包含 status、summary、recommended_status、validation 和 blockers。
- recommended_status 只允许 ready_for_review、spec_blocked、dev_blocked、coding；LoopForge 会根据 worker-result 裁决最终 task 状态。
- claimed 只表示本轮已被 LoopForge 领取；项目内任务工件可以按项目文档创建，但不得调用会改写任务状态的本地 backlog 脚本。
- 本轮最多推进到 ready_for_review；accepted 只能由人工验收产生，merged/completed 只能由后续合入和清理动作产生，abandoned 只能由人工明确放弃。
- 默认把 item 视为复杂任务；只有 item.planning_level 明确为 lightweight 时，才允许 requirements-only。
- 复杂任务在请求 ready_for_review 前必须通过当前 planning provider 的完整性门禁；不是仅创建文档后收尾。
- lightweight 任务也必须至少补齐 prd.md 和可验证验收标准，才能请求 ready_for_review。
- 运行中新发现的信息可以回填项目长期文档；普通回填继续执行，并在 worker-result.documentation 中列出文件且设置 review_required=true，留到最终人工验收统一审核。
- 只有新信息使当前任务合同无法继续满足时，才设置 contract_change_required=true 并建议 spec_blocked；不得把普通文档编辑升级成人工阻塞。
- {commit_rule}

请按项目 AGENTS.md 和 agent_docs/DEV_TASK_EXECUTION.md 执行当前 item。完成或阻塞时，只写 worker-result，由 LoopForge 统一更新并发布单任务 JSON。
"""


def _continuation_prompt_block(item: Dict[str, Any]) -> str:
    context = item.get("continuation_context")
    if not isinstance(context, dict) or not context.get("previous_run_id"):
        return ""
    completeness = "完整" if context.get("complete") is True else "不完整"
    return "\n".join(
        [
            "跨模型继续上下文：",
            f"- 上一相关 run：{context.get('previous_run_id')}",
            f"- 交接完整度：{completeness}",
            f"- 已知摘要：{context.get('summary') or '无'}",
            f"- 验证：{json.dumps(context.get('validation') or {}, ensure_ascii=False)}",
            f"- 阻塞：{json.dumps(context.get('blockers') or [], ensure_ascii=False)}",
            f"- 诊断：{json.dumps(context.get('diagnostic') or {}, ensure_ascii=False)}",
            f"- 工作区：{json.dumps(context.get('worktree') or {}, ensure_ascii=False)}",
            f"- 上一消息摘录：{context.get('last_message_excerpt') or '无'}",
            "- 这是新模型会话，但必须延续现有任务状态；先检查已有实现、文档、Git diff 与验证证据，再确定剩余工作。",
            "- 不得重置 worktree、覆盖已有改动或无条件重做已完成步骤；上下文不完整时，只重复必要的现状检查和验证。",
        ]
    )


def _acceptance_prompt_block(item: Dict[str, Any]) -> str:
    acceptance = item.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        return ""
    rows = [f"- {index}. {str(value).strip()}" for index, value in enumerate(acceptance)]
    return "\n".join(
        [
            "任务级验收项：",
            *rows,
            "- 请求 ready_for_review 时，worker-result.acceptance_results 必须逐项返回相同 index、status=passed，并在 evidence 中写入可复核的非空证据。",
        ]
    )


def _authority_prompt_block(item: Dict[str, Any]) -> str:
    lines: list[str] = []
    for label, document_set in _document_sets(item):
        phases = ordered_document_phases(document_set)
        if not phases:
            continue
        lines.append(f"- {label}：")
        authority = _authority_location(item, label)
        if authority:
            lines.append(f"  - 文档根目录：{authority['root']}")
            lines.append(f"  - 文档版本：{authority['revision']}")
            if label.startswith("Target "):
                lines.append(f"  - 实现与文档回填目录：{authority['root']}")
        for phase in phases:
            phase_id = str(phase.get("id") or "")
            title = str(phase.get("title") or phase_id)
            after = phase.get("after") if isinstance(phase.get("after"), list) else []
            lines.append(
                f"  - Phase {phase_id}（{title}）；after={', '.join(str(value) for value in after) if after else '无'}"
            )
            for field in ("requirements", "design", "specs"):
                paths = phase.get(field) if isinstance(phase.get(field), list) else []
                lines.append(f"    - {field}：{', '.join(str(path) for path in paths) if paths else '无'}")
        supporting = document_set.get("supporting")
        if isinstance(supporting, list) and supporting:
            lines.append(f"  - supporting（按需读取）：{', '.join(str(path) for path in supporting)}")
    if not lines:
        return ""
    rules = [
        "权威文档阶段：",
        "- 先按项目路由读取 AGENTS.md 与其明确要求的约束文档一次。",
        "- 每组文档的相对路径必须在其文档根目录下解析；不得回退到父级 main 或其他 Target 工作目录。",
        "- 随后严格按下列拓扑顺序执行；同一 worker run 内只读取当前 Phase 的 requirements、design 与 specs，不得预读后续 Phase。",
        "- 当前 Phase 完成实现、局部验证和简短结果记录后，自动进入下一 Phase，不得要求人工再次确认。",
        "- supporting 是辅助资料，只能在当前 Phase 确有需要时按需读取，不得默认全量加载。",
    ]
    return "\n".join(rules + lines)


def _authority_location(item: Dict[str, Any], label: str) -> Dict[str, str]:
    roots = item.get("authority_roots")
    if not isinstance(roots, dict):
        return {}
    raw: Any
    if label == "项目":
        raw = roots.get("project")
    elif label.startswith("Target "):
        targets = roots.get("targets")
        raw = targets.get(label.removeprefix("Target ")) if isinstance(targets, dict) else None
    else:
        raw = None
    if not isinstance(raw, dict):
        return {}
    root = str(raw.get("root") or "").strip()
    revision = str(raw.get("revision") or "").strip()
    return {"root": root, "revision": revision} if root and revision else {}


def _document_sets(item: Dict[str, Any]) -> list[tuple[str, Dict[str, Any]]]:
    document_sets: list[tuple[str, Dict[str, Any]]] = []
    docs = item.get("docs")
    if isinstance(docs, dict) and docs:
        document_sets.append(("项目", docs))
    targets = item.get("targets")
    if isinstance(targets, list):
        for index, target in enumerate(targets):
            if not isinstance(target, dict) or not isinstance(target.get("docs"), dict):
                continue
            target_id = str(target.get("id") or target.get("project") or f"target-{index + 1}")
            document_sets.append((f"Target {target_id}", target["docs"]))
    return document_sets


def preview(profile: ProjectProfile, item: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    execution_item = _item_with_continuation_context(profile, item)
    command = worker_command(profile, run_id, str(execution_item.get("execution_root") or ""))
    prompt = build_prompt(profile, execution_item, run_id)
    return {
        "executor": profile.executor,
        "agent": item.get("agent") or profile.default_agent,
        "command": command,
        "prompt": prompt,
        "prompt_excerpt": prompt[:420],
        "run_dir": str(build_run_dir(profile, run_id)),
        "artifacts": artifact_paths(profile, run_id),
    }


def run_executor(profile: ProjectProfile, item: Dict[str, Any], run_id: str) -> Dict[str, Any]:
    run_dir = build_run_dir(profile, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    last_message_path = run_dir / "last-message.md"
    events_path = run_dir / "events.jsonl"
    execution_item = _item_with_continuation_context(profile, item)
    execution_root = str(execution_item.get("execution_root") or profile.root_dir)
    command = worker_command(profile, run_id, execution_root)
    prompt = build_prompt(profile, execution_item, run_id)
    artifacts = artifact_paths(profile, run_id)
    _write_request_artifacts(profile, execution_item, run_id, command, prompt, artifacts)
    if profile.executor == "fake_codex":
        last_message_path.write_text(f"fake codex 完成任务：{execution_item.get('title')}\n", encoding="utf-8")
        events_path.write_text(json.dumps({"event": "fake_completed", "item_id": execution_item.get("id")}, ensure_ascii=False) + "\n", encoding="utf-8")
        return _write_result_artifact(
            artifacts,
            {
                "status": "completed",
                "exit_code": 0,
                "summary": f"fake codex 已完成：{execution_item.get('title')}",
                "last_message_path": str(last_message_path),
                "log_path": str(events_path),
                "final_status": "completed",
            },
        )

    try:
        process = subprocess.Popen(
            command,
            cwd=execution_root,
            env=build_worker_environment(profile),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = _communicate_with_cancel(profile, run_id, process, prompt)
    except FileNotFoundError as exc:
        if profile.executor == "claude_cli":
            binary_name = "claude"
            env_name = "LOOPFORGE_CLAUDE_BIN"
            cli_name = "Claude Code CLI"
        else:
            binary_name = "codex"
            env_name = "LOOPFORGE_CODEX_BIN"
            cli_name = "Codex CLI"
        summary = f"{profile.executor} 执行失败：找不到 {binary_name} 可执行文件"
        detail = f"{summary}。请设置 {env_name}，或把 {cli_name} 加入启动 LoopForge 后台的 PATH。原始错误：{exc}"
        events_path.write_text(
            json.dumps({"event": f"{binary_name}_not_found", "command": command[0], "error": str(exc)}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        last_message_path.write_text(detail, encoding="utf-8")
        return _write_result_artifact(
            artifacts,
            {
                "status": "failed",
                "exit_code": 127,
                "summary": summary,
                "last_message_path": str(last_message_path),
                "log_path": str(events_path),
                "stderr": detail,
            },
        )
    events_path.write_text(stdout, encoding="utf-8")
    cancel_request = read_cancel_request(profile, run_id)
    if cancel_request:
        summary = "运行已按取消请求停止"
        detail = cancel_request.get("reason") or summary
        last_message_path.write_text(detail, encoding="utf-8")
        return _write_result_artifact(
            artifacts,
            {
                "status": "cancelled",
                "exit_code": process.returncode,
                "summary": summary,
                "required_action": "本轮已取消；请检查工作区半成品后决定继续运行、回滚或放弃任务。",
                "last_message_path": str(last_message_path),
                "log_path": str(events_path),
                "stderr": stderr.strip(),
                "cancel_request": cancel_request,
            },
        )
    outcome = parse_worker_output(profile.executor, stdout, stderr, process.returncode or 0)
    if profile.executor == "claude_cli" or not last_message_path.exists():
        last_message_path.write_text(outcome.last_message or outcome.error_detail or outcome.stderr or outcome.summary, encoding="utf-8")
    result_payload: Dict[str, Any] = {
        "status": outcome.status,
        "exit_code": process.returncode,
        "summary": outcome.summary,
        "last_message_path": str(last_message_path),
        "log_path": str(events_path),
        "stderr": outcome.stderr,
    }
    if outcome.required_action:
        result_payload["required_action"] = outcome.required_action
    if outcome.error_detail:
        result_payload["error_detail"] = outcome.error_detail
    if outcome.session_id:
        result_payload["session_id"] = outcome.session_id
    if outcome.provider_usage:
        result_payload["provider_usage"] = outcome.provider_usage
    if outcome.reported_cost_usd is not None:
        result_payload["reported_cost_usd"] = outcome.reported_cost_usd
    return _write_result_artifact(
        artifacts,
        result_payload,
    )


def _communicate_with_cancel(
    profile: ProjectProfile,
    run_id: str,
    process: subprocess.Popen[str],
    prompt: str,
) -> tuple[str, str]:
    input_sent = False
    while True:
        try:
            if input_sent:
                return process.communicate(timeout=0.5)
            input_sent = True
            return process.communicate(prompt, timeout=0.5)
        except subprocess.TimeoutExpired:
            if read_cancel_request(profile, run_id):
                process.terminate()
                try:
                    return process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    return process.communicate(timeout=5)


def _write_request_artifacts(
    profile: ProjectProfile,
    item: Dict[str, Any],
    run_id: str,
    command: List[str],
    prompt: str,
    artifacts: Dict[str, str],
) -> None:
    legacy_codex_metadata = {}
    if profile.executor in {"codex_cli", "fake_codex"}:
        legacy_codex_metadata = {
            "codex_model": profile.codex_model,
            "codex_reasoning_effort": profile.codex_reasoning_effort,
            "codex_sandbox": profile.codex_sandbox,
        }
    Path(artifacts["prompt_path"]).write_text(prompt, encoding="utf-8")
    _write_json(
        Path(artifacts["command_path"]),
        {
            "version": 1,
            "created_at": utc_now(),
            "cwd": str(item.get("execution_root") or profile.root_dir),
            "command": command,
        },
    )
    _write_json(
        Path(artifacts["request_path"]),
        {
            "version": 1,
            "created_at": utc_now(),
            "run_id": run_id,
            "project_id": profile.project_id,
            "project_name": profile.name,
            "root_dir": str(profile.root_dir),
            "executor": profile.executor,
            "agent": item.get("agent") or profile.default_agent,
            "auto_commit": profile.auto_commit,
            **legacy_codex_metadata,
            **request_metadata(profile),
            "continuation_context": item.get("continuation_context") if isinstance(item.get("continuation_context"), dict) else None,
            "item": _item_snapshot(item),
            "prompt_chars": len(prompt),
        },
    )


def _item_with_continuation_context(profile: ProjectProfile, item: Dict[str, Any]) -> Dict[str, Any]:
    projected = dict(item)
    if not isinstance(projected.get("continuation_context"), dict):
        context = _build_continuation_context(profile, projected)
        if context:
            projected["continuation_context"] = context
    return projected


def _build_continuation_context(profile: ProjectProfile, item: Dict[str, Any]) -> Dict[str, Any]:
    task_id = str(item.get("id") or "").strip()
    if not task_id:
        return {}
    previous = next(
        (
            record
            for record in reversed(read_jsonl(profile.history_path))
            if str(record.get("task_id") or "").strip() == task_id and record.get("run_id")
        ),
        None,
    )
    if not isinstance(previous, dict):
        return {}
    previous_run_id = str(previous.get("run_id") or "").strip()
    run_dir = _safe_previous_run_dir(profile, previous_run_id)
    worker_result: Dict[str, Any] = {}
    last_message_excerpt = ""
    if run_dir is not None:
        worker_result = _read_json_object(run_dir / "worker-result.json")
        last_message_excerpt = _read_bounded_text(run_dir / "last-message.md", 1200)
    validation = worker_result.get("validation") if isinstance(worker_result.get("validation"), dict) else {}
    blockers = worker_result.get("blockers") if isinstance(worker_result.get("blockers"), list) else []
    diagnostic = previous.get("diagnostic") if isinstance(previous.get("diagnostic"), dict) else {}
    summary = str(worker_result.get("summary") or previous.get("summary") or "").strip()
    return {
        "previous_run_id": previous_run_id,
        "complete": bool(worker_result),
        "status": str(previous.get("status") or ""),
        "summary": _bounded_text(summary, 1200),
        "validation": validation,
        "blockers": blockers,
        "diagnostic": diagnostic,
        "last_message_excerpt": last_message_excerpt,
        "worktree": _worktree_snapshot(str(item.get("execution_root") or profile.root_dir)),
    }


def _safe_previous_run_dir(profile: ProjectProfile, run_id: str) -> Path | None:
    if not run_id or Path(run_id).name != run_id:
        return None
    runs_dir = (profile.loopforge_dir / "runs").resolve()
    candidate = (runs_dir / run_id).resolve()
    try:
        candidate.relative_to(runs_dir)
    except ValueError:
        return None
    return candidate if candidate.is_dir() else None


def _read_json_object(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_bounded_text(path: Path, limit: int) -> str:
    try:
        return _bounded_text(path.read_text(encoding="utf-8"), limit)
    except OSError:
        return ""


def _bounded_text(value: str, limit: int) -> str:
    clean = str(value or "").strip()
    return clean if len(clean) <= limit else clean[: limit - 1] + "…"


def _worktree_snapshot(root: str) -> Dict[str, Any]:
    path = Path(root)
    snapshot: Dict[str, Any] = {"path": str(path), "branch": "", "status": "unavailable", "dirty_files": []}
    try:
        branch = subprocess.run(
            ["git", "-C", str(path), "branch", "--show-current"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = subprocess.run(
            ["git", "-C", str(path), "status", "--porcelain"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return snapshot
    if branch.returncode == 0:
        snapshot["branch"] = branch.stdout.strip()
    if status.returncode == 0:
        dirty_files = [line[3:].strip() for line in status.stdout.splitlines() if len(line) >= 4]
        snapshot["status"] = "dirty" if dirty_files else "clean"
        snapshot["dirty_files"] = dirty_files[:50]
    return snapshot


def _item_snapshot(item: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "id",
        "title",
        "description",
        "priority",
        "planning_level",
        "status",
        "agent",
        "source",
        "planning",
        "execution_root",
        "targets",
        "acceptance",
        "acceptance_refs",
    ]
    return {key: item.get(key) for key in keys if key in item}


def _write_result_artifact(artifacts: Dict[str, str], result: Dict[str, Any]) -> Dict[str, Any]:
    enriched = dict(result)
    token_usage = extract_token_usage(Path(artifacts["events_path"]))
    if token_usage is not None:
        enriched["token_usage"] = token_usage
    enriched["artifacts"] = artifacts
    enriched["result_path"] = artifacts["result_path"]
    _write_json(Path(artifacts["result_path"]), enriched)
    return enriched


def merge_result_artifact(result_path: str, **fields: Any) -> Dict[str, Any]:
    path = Path(result_path)
    payload: Dict[str, Any] = {}
    if path.exists():
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if isinstance(loaded, dict):
            payload = loaded
    payload.update(fields)
    _write_json(path, payload)
    return payload


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _failure_summary(returncode: int, stderr: str) -> str:
    clean_stderr = (stderr or "").strip()
    if clean_stderr:
        first_line = clean_stderr.splitlines()[0].strip()
        return f"codex_cli 执行失败：{first_line}"
    if returncode < 0:
        signal_name = _signal_name(-returncode)
        return f"codex_cli 被系统信号终止：{signal_name}"
    return f"codex_cli 执行失败：exit_code={returncode}"


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:
        return f"SIG{signum}"
