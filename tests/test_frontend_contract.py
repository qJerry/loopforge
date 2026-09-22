from __future__ import annotations

from pathlib import Path
import unittest


FRONTEND_MAIN = Path("frontend/src/main.jsx")


class FrontendContractTests(unittest.TestCase):
    def test_runtime_settings_are_provider_driven(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn("project.worker_runtime", source)
        self.assertIn("workerRuntime.providers", source)
        self.assertIn("worker_provider: workerProvider", source)
        self.assertIn("worker_settings: workerSettings", source)
        self.assertIn("workerProfile.controls", source)
        self.assertIn("Worker Provider", source)
        self.assertIn("当前运行不受影响", source)
        self.assertIn("run.provider", source)
        self.assertIn("Provider 报告", source)
        self.assertIn('/api/v1/worker-health', source)
        self.assertIn("Worker 环境检测", source)
        self.assertIn("重新检测", source)
        self.assertIn("不影响保存运行配置", source)
        self.assertIn('workerHealth?.providers?.[provider]', source)
        self.assertIn('worker_network: workerNetwork', source)
        self.assertIn('workerRuntime.network', source)
        self.assertIn('networkProfile.controls', source)
        self.assertIn('HTTP_PROXY', source)
        self.assertIn('HTTPS_PROXY', source)
        self.assertIn('ALL_PROXY', source)
        self.assertIn('/api/v1/worker-health?project_id=', source)
        self.assertNotIn("project.codex_reasoning_effort", source)
        self.assertNotIn("Codex Sandbox", source)
        self.assertNotIn("Codex usage", source)

    def test_current_blocker_ui_does_not_fallback_to_latest_run(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn("function currentBlockerReasonFrom", source)
        self.assertNotIn("runBlockerReason(latest)", source)
        self.assertNotIn("latest?.required_action", source)
        self.assertIn("function ProjectOverviewPanel", source)
        self.assertIn("<TaskFlowPanel", source)

    def test_project_card_surfaces_current_issue_diagnostic(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn("function compactDiagnosticRows", source)
        self.assertIn("const issue = project.issue || {}", source)
        self.assertIn("function ProjectIssueSummary", source)
        self.assertIn("project-alert-summary", source)
        self.assertIn("project-diagnostic-inline", source)
        self.assertIn("secondaryIssueCopy", source)

    def test_inbox_is_default_control_surface(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn('useState(readSelectedProjectIdFromUrl() ? "project" : "inbox")', source)
        self.assertIn("function InboxView", source)
        self.assertIn("function QueuePressureChart", source)
        self.assertIn("function ProjectHotspots", source)
        self.assertIn("function InboxClueRow", source)
        self.assertIn("function TaskActionButtons", source)
        self.assertIn("function ProjectTabs", source)
        self.assertIn("function ProjectCluesPanel", source)
        self.assertIn("function ProjectRunsPanel", source)
        self.assertIn("function DocumentHealthPanel", source)
        self.assertIn("function RunArtifactPanel", source)
        self.assertIn("function ProjectSettingsPanel", source)
        self.assertIn("function AutomationModeBadge", source)
        self.assertIn("automationModeOptions", source)
        self.assertIn("automation_mode: automationMode", source)
        self.assertIn('schedule_enabled: automationMode !== "off"', source)
        self.assertIn("automation-badge", source)
        self.assertIn("automation-mode-card", source)
        self.assertIn('/tasks/${encodeURIComponent(taskId)}/${action}', source)
        self.assertIn('/clues/decide', source)
        self.assertIn('decision: "reject_code"', source)
        self.assertIn('decision: "reject_spec"', source)
        self.assertIn("allClueEntries(projects)", source)
        self.assertIn("/cancel-run", source)
        self.assertNotIn("线索 API 尚未接入", source)
        self.assertIn("const selected = useMemo(() => selectedId ? projects.find", source)
        self.assertNotIn("projects[0], [projects, selectedId]", source)

    def test_serial_project_uses_one_contextual_primary_action(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn('from "./project-run-actions.mjs"', source)
        self.assertIn('runAction(projectId, "start-run")', source)
        self.assertIn('runAction(projectId, "resume-once")', source)
        self.assertIn("model.primaryAction", source)
        self.assertIn("查看当前任务", Path("frontend/src/project-run-actions.mjs").read_text(encoding="utf-8"))
        self.assertIn("查看运行计划", source)
        self.assertIn("仅查看计划，不会调用执行器或修改任务。", source)
        self.assertIn("预计影响范围", source)
        self.assertIn("{ quiet: true, refreshAfter: false }", source)
        self.assertIn('aria-labelledby="run-plan-title"', source)
        self.assertIn('disabled={busy || model.running}', source)
        self.assertNotIn(">\n        开始运行\n", source)
        self.assertNotIn(">\n        继续一轮\n", source)
        self.assertNotIn(">\n        预演\n", source)
        self.assertNotIn(">\n        需求校准\n", source)
        self.assertNotIn(">\n        开发执行\n", source)

    def test_run_action_treats_accepted_as_background_execution(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn('result.status === "accepted"', source)
        self.assertIn("已受理，任务将在后台继续运行", source)
        self.assertNotIn("等待本轮完成", source)

    def test_inbox_loads_task_token_trend_with_project_filter_and_accessible_tooltip(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn("function TaskTokenTrend", source)
        self.assertIn('api(`/api/v1/token-usage/tasks?${params.toString()}`)', source)
        self.assertIn("tokenProjectId", source)
        self.assertIn('<svg className="token-trend-svg"', source)
        self.assertIn("onMouseEnter", source)
        self.assertIn("onFocus", source)
        self.assertIn('tabIndex="0"', source)
        self.assertIn("实际消耗可能更高", source)
        self.assertIn("无统计数据", source)
        self.assertIn("关联项目", source)
        self.assertIn('useState("cost")', source)
        self.assertIn('value="cost"', source)
        self.assertIn('value="tokens"', source)
        self.assertIn("API 等价估算", source)
        self.assertIn("model_runs", source)
        self.assertIn("reasoning_effort", source)
        self.assertIn("cost_estimate", source)
        self.assertIn("不等于实际账单", source)
        self.assertNotIn("recharts", source.lower())

    def test_project_detail_phase7_tabs_and_evidence_views_exist(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        for label in ["总览", "任务", "线索", "运行记录", "文档健康", "设置"]:
            self.assertIn(f'label: "{label}"', source)
        self.assertIn("taskGroupDefinitions", source)
        self.assertIn("clueGroupDefinitions", source)
        self.assertIn("runArtifactRows", source)
        self.assertIn("docHealthModel", source)
        self.assertIn("requirements/design/specs/code/test 对齐矩阵", source)
        self.assertIn("requirementsList", source)
        self.assertIn("designList", source)
        self.assertIn("specsList", source)
        self.assertIn("hasRequirements", source)
        self.assertIn("hasDesign", source)
        self.assertIn("specsPresent", source)
        self.assertNotIn("updateNotificationConfig", source)

    def test_task_details_show_project_and_target_document_references(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn("function taskDocumentSets", source)
        self.assertIn("function taskDocumentPaths", source)
        self.assertIn("function TaskDocumentReferences", source)
        self.assertIn("<TaskDocumentReferences task={task}", source)
        self.assertIn('title={revision || "未记录版本"}', source)
        self.assertIn("<strong>关联文档</strong>", source)
        self.assertIn("docs.phases", source)
        self.assertIn("phase?.bdd", source)
        self.assertIn("function TaskAcceptanceCriteria", source)
        self.assertIn("task.acceptance", source)
        self.assertIn("task.acceptance_criteria", source)
        self.assertIn(">验收项<", source)
        self.assertIn("<TaskAcceptanceCriteria task={task}", source)

    def test_task_document_references_use_compact_grouped_disclosures(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn("function taskDocumentGroups", source)
        self.assertIn('className="task-document-references"', source)
        self.assertIn('className="task-document-target"', source)
        self.assertIn('className="task-document-phase"', source)
        self.assertIn("个范围 ·", source)
        self.assertNotIn('<DetailRow label="关联文档"', source)
        self.assertNotIn("open={index === 0}", source)

    def test_task_manager_uses_fixed_order_and_collapsed_recent_history(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertNotIn("tasks/reorder", source)
        self.assertNotIn("拖动排序", source)
        self.assertNotIn(">上移<", source)
        self.assertNotIn(">下移<", source)
        self.assertIn("按优先级查看正在进行与等待处理的任务", source)
        self.assertIn("const groups = groupTasks(tasks)", source)
        self.assertIn("historyExpanded", source)
        self.assertIn("tasks/history?limit=20", source)
        self.assertIn("展开最近历史任务（最多 20 条）", source)
        self.assertIn("readOnly task={task}", source)

    def test_task_cleanup_and_abandon_require_explicit_dirty_worktree_confirmation(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn("function cleanupRequiresDiscard", source)
        self.assertIn('discard_changes: true', source)
        self.assertIn('task.cleanup_pending === "abandon"', source)
        self.assertIn("重试放弃清理", source)
        self.assertIn("远端 branch 会保留", source)
        self.assertIn("确认永久丢弃这些变化", source)

    def test_review_rejection_uses_visible_feedback_dialog(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn("function ReviewFeedbackDialog", source)
        self.assertIn('aria-label="验收驳回反馈"', source)
        self.assertIn('"确认驳回"', source)
        self.assertIn('role="alert"', source)
        self.assertIn('useState("")', source)
        self.assertIn('result?.summary || "驳回未保存，请重试。"', source)
        self.assertNotIn('window.prompt("代码问题反馈")', source)
        self.assertNotIn('window.prompt("需求或规则问题反馈")', source)

    def test_onboarding_form_uses_explicit_owner_and_builtin_workflow_contract(self) -> None:
        source = FRONTEND_MAIN.read_text(encoding="utf-8")

        self.assertIn('onboardingOwner={settings?.onboarding_defaults?.owner || ""}', source)
        self.assertIn('const [planningAdapter, setPlanningAdapter] = useState("builtin")', source)
        self.assertIn("owner: owner.trim()", source)
        self.assertIn("Owner（必填）", source)
        self.assertIn("Owner 不能为空。", source)
        self.assertIn("preview.workflow_initialization.provider", source)
        self.assertNotIn('<option value="trellis">', source)


if __name__ == "__main__":
    unittest.main()
