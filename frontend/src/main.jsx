import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { parseApiResponse } from "./api-response.mjs";
import { cleanupFailureMessage } from "./cleanup-feedback.mjs";
import { DEFAULT_CLUE_SCOPE, visibleClues } from "./clue-filters.mjs";
import { submitReviewWithContinuation } from "./review-continuation.mjs";
import { canResumeWithInstruction, resumeWithInstruction } from "./run-instruction.mjs";
import { projectRunActionModel } from "./project-run-actions.mjs";
import { isBlockedTask } from "./task-state.mjs";
import { buildTargetReviewRows } from "./target-review.mjs";
import "./styles.css";
import "./theme.css";

const token =
  window.LOOPFORGE_TOKEN && window.LOOPFORGE_TOKEN !== "__LOOPFORGE_TOKEN__"
    ? window.LOOPFORGE_TOKEN
    : new URLSearchParams(window.location.search).get("token") || import.meta.env.VITE_LOOPFORGE_TOKEN || "loopforge-local";
const SELECTED_PROJECT_QUERY_KEY = "project";

function headers() {
  return {
    "Content-Type": "application/json",
    "X-LoopForge-Token": token,
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { ...headers(), ...(options.headers || {}) },
  });
  return parseApiResponse(response);
}

function formatTime(value) {
  if (!value) return "无";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", { hour12: false });
}

function statusClass(value) {
  return String(value || "idle").replace(/[^a-z0-9_-]/g, "");
}

function StatusBadge({ value, label }) {
  return <span className={`status-badge ${statusClass(value)}`}>{label || value || "未知"}</span>;
}

function readSelectedProjectIdFromUrl() {
  return new URLSearchParams(window.location.search).get(SELECTED_PROJECT_QUERY_KEY) || null;
}

function writeSelectedProjectIdToUrl(projectId) {
  const url = new URL(window.location.href);
  if (projectId) {
    url.searchParams.set(SELECTED_PROJECT_QUERY_KEY, projectId);
  } else {
    url.searchParams.delete(SELECTED_PROJECT_QUERY_KEY);
  }
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

function runLabel(run) {
  const labels = {
    completed: "已完成",
    blocked: "阻塞",
    timeout_continue: "超时可继续",
    timeout_blocked: "超时阻塞",
    skipped_no_task: "无任务",
    skipped_paused: "暂停跳过",
    skipped_already_running: "已有运行",
    skipped_no_running: "无运行",
    skipped_not_implemented: "未实现",
    cancel_requested: "取消中",
    cancelled: "已取消",
    failed: "失败",
    misconfigured: "配置错误",
  };
  return labels[run?.status] || run?.status || "无记录";
}

const taskStateLabels = {
  open: "待领取",
  claimed: "已领取",
  spec_ready: "规格就绪",
  spec_blocked: "规格阻塞",
  prd_ready: "规格就绪",
  prd_blocked: "规格阻塞",
  coding: "实现中",
  dev_blocked: "开发阻塞",
  ready_for_review: "待验收",
  accepted: "已验收",
  merged: "已合入",
  blocked: "执行阻塞",
  completed: "已完成",
  abandoned: "已放弃",
};
const specBlockedTaskStates = new Set(["spec_blocked", "prd_blocked"]);
const devBlockedTaskStates = new Set(["dev_blocked", "blocked"]);
const inboxTaskStates = new Set(["ready_for_review", "spec_blocked", "prd_blocked", "dev_blocked", "blocked", "accepted", "merged"]);
const inboxStatusOrder = {
  ready_for_review: 0,
  spec_blocked: 1,
  prd_blocked: 1,
  dev_blocked: 2,
  blocked: 2,
  accepted: 4,
  merged: 5,
};
const priorityOrder = { P0: 0, P1: 1, P2: 2, P3: 3, P4: 4 };
const projectTabs = [
  { id: "overview", label: "总览" },
  { id: "tasks", label: "任务" },
  { id: "clues", label: "线索" },
  { id: "runs", label: "运行记录" },
  { id: "docs", label: "文档健康" },
  { id: "settings", label: "设置" },
];
const taskGroupDefinitions = [
  { id: "blocked-before-dev", label: "待处理阻塞", states: ["spec_blocked", "prd_blocked"] },
  { id: "development-ready", label: "待开发", states: ["open", "spec_ready", "prd_ready"] },
  { id: "development", label: "开发中", states: ["coding", "dev_blocked", "blocked"] },
  { id: "review", label: "待验收", states: ["ready_for_review"] },
  { id: "merge", label: "待合入", states: ["accepted"] },
  { id: "cleanup", label: "收尾中", states: ["merged"] },
  { id: "done", label: "已结束", states: ["completed", "abandoned"] },
];
const clueGroupDefinitions = [
  { id: "needs_confirmation", label: "需要确认", statuses: ["needs_confirmation"] },
  { id: "open", label: "可升级任务", statuses: ["open"] },
  { id: "task_created", label: "已创建任务", statuses: ["task_created"] },
  { id: "resolved", label: "已自动修复", statuses: ["resolved"] },
  { id: "false_positive", label: "误报", statuses: ["false_positive"] },
];

function taskStateLabel(value, fallback) {
  return fallback || taskStateLabels[value] || value || "无";
}

function sourceLabel(value) {
  const labels = {
    seed: "内置",
    manual: "手动",
    ai: "AI 创建",
  };
  return labels[value] || value || "未知";
}

function triggerLabel(value) {
  const labels = {
    manual: "手动运行",
    schedule: "调度触发",
    preview: "预演",
    resume_once: "仅恢复一轮",
    resolve_and_resume: "处理阻塞后恢复",
    manual_start: "手动开始运行",
  };
  return labels[value] || value || "未知";
}

function loopTypeLabel(value) {
  return {
    scan: "项目巡检",
    dev: "开发执行",
    task_action: "任务动作",
  }[value] || value || "开发执行";
}

function scheduleFrequencyLabel(value) {
  return {
    half_hourly: "每半小时",
    hourly: "每小时",
    daily: "每天",
    weekly: "每周",
  }[value] || "每小时";
}

const automationModeOptions = [
  { value: "off", label: "关闭", risk: "最低", loops: "无自动循环", description: "不参与后台自动调度，仍可手动运行。" },
  { value: "observe", label: "观察模式", risk: "低", loops: "项目巡检", description: "只自动巡检，不自动补文档或改代码。" },
  { value: "execute", label: "执行模式", risk: "高", loops: "巡检 + 开发", description: "允许后台自动开发执行，风险最高。" },
];

function automationModeFrom(project) {
  return project?.automation_mode || (project?.schedule_enabled ? "execute" : "off");
}

function automationModeInfo(projectOrMode) {
  const mode = typeof projectOrMode === "string" ? projectOrMode : automationModeFrom(projectOrMode);
  const option = automationModeOptions.find((item) => item.value === mode) || automationModeOptions[0];
  const payload = typeof projectOrMode === "string" ? null : projectOrMode;
  return {
    ...option,
    label: payload?.automation_mode_label || option.label,
    description: payload?.automation_mode_description || option.description,
  };
}

function AutomationModeBadge({ project, compact = false }) {
  const info = automationModeInfo(project);
  return (
    <span className={`automation-badge mode-${info.value} ${compact ? "compact" : ""}`} title={info.description}>
      {compact ? info.label.replace("模式", "") : info.label}
    </span>
  );
}

function scheduleDisplayLabel(project, scheduler) {
  const modeInfo = automationModeInfo(project);
  if (modeInfo.value === "off") return "自动化关闭";
  const frequency = scheduleFrequencyLabel(project.schedule_frequency);
  if (scheduler && scheduler.enabled === false) return `${modeInfo.label} · ${frequency}（后台未启动）`;
  const value = project.next_scheduled_at;
  if (!value) return `${modeInfo.label} · ${frequency}`;
  const date = new Date(value);
  const nextText = Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
  return `${modeInfo.label} · ${frequency}（${nextText}）`;
}

function blockerReasonFrom(value) {
  if (!value) return "";
  return value.blocker_reason || value.last_error || value.required_action || "";
}

function currentBlockerReasonFrom(value) {
  return isBlockedTask(value) ? blockerReasonFrom(value) : "";
}

function runBlockerReason(run) {
  if (!run) return "";
  if (run.blocker_reason) return run.blocker_reason;
  return isBlockedTask(run.blocker_type) ? run.required_action || "" : "";
}

function taskStatus(task) {
  return task?.status || task?.state || "open";
}

function priorityRank(value) {
  return priorityOrder[String(value || "P2").toUpperCase()] ?? 9;
}

function projectDisplayName(project) {
  return project?.name || project?.project_id || "未知项目";
}

const projectOrderStorageKey = "loopforge.project-order.v1";

function readProjectOrder() {
  try {
    const value = JSON.parse(window.localStorage.getItem(projectOrderStorageKey) || "[]");
    return Array.isArray(value) ? value.filter((item) => typeof item === "string" && item) : [];
  } catch {
    return [];
  }
}

function saveProjectOrder(projectIds) {
  try {
    window.localStorage.setItem(projectOrderStorageKey, JSON.stringify(projectIds));
  } catch {
    // 本地偏好不可写时仍保留当前会话内的排序。
  }
}

function applyProjectOrder(projects, projectIds) {
  if (!projectIds.length) return projects;
  const position = new Map(projectIds.map((projectId, index) => [projectId, index]));
  return projects
    .map((project, sourceIndex) => ({ project, sourceIndex }))
    .sort((left, right) => {
      const leftPosition = position.get(left.project.project_id);
      const rightPosition = position.get(right.project.project_id);
      if (leftPosition === undefined && rightPosition === undefined) return left.sourceIndex - right.sourceIndex;
      if (leftPosition === undefined) return 1;
      if (rightPosition === undefined) return -1;
      return leftPosition - rightPosition;
    })
    .map(({ project }) => project);
}

function moveProject(projects, sourceId, targetId, placement = "before") {
  if (!sourceId || !targetId || sourceId === targetId) return projects;
  const source = projects.find((project) => project.project_id === sourceId);
  if (!source) return projects;
  const remaining = projects.filter((project) => project.project_id !== sourceId);
  const targetIndex = remaining.findIndex((project) => project.project_id === targetId);
  if (targetIndex < 0) return projects;
  const insertIndex = placement === "after" ? targetIndex + 1 : targetIndex;
  remaining.splice(insertIndex, 0, source);
  return remaining;
}

const documentKindLabels = {
  requirements: "需求",
  design: "设计",
  specs: "规格",
  bdd: "BDD",
  supporting: "补充",
};

function documentPathValues(...values) {
  return Array.from(new Set(values
    .flatMap((value) => Array.isArray(value) ? value : typeof value === "string" ? [value] : [])
    .map((value) => String(value || "").trim())
    .filter(Boolean)));
}

function taskDocumentGroups(docs) {
  if (!docs || typeof docs !== "object" || Array.isArray(docs)) return [];
  const phases = Array.isArray(docs.phases) && docs.phases.length ? docs.phases : [docs];
  const groups = phases.map((phase, index) => {
    const kinds = [
      { id: "requirements", paths: documentPathValues(phase?.requirements, phase?.prd) },
      { id: "design", paths: documentPathValues(phase?.design) },
      { id: "specs", paths: documentPathValues(phase?.specs, phase?.spec) },
      { id: "bdd", paths: documentPathValues(phase?.bdd, phase?.when_then) },
    ].filter((kind) => kind.paths.length);
    return {
      id: String(phase?.id || phase?.title || phase?.module || `phase-${index + 1}`),
      label: phase?.title || phase?.module || phase?.id || docs.module || `阶段 ${index + 1}`,
      kinds,
      documentCount: kinds.reduce((total, kind) => total + kind.paths.length, 0),
    };
  }).filter((group) => group.documentCount);
  const supporting = documentPathValues(docs.supporting);
  if (supporting.length) {
    groups.push({
      id: "supporting",
      label: "补充材料",
      kinds: [{ id: "supporting", paths: supporting }],
      documentCount: supporting.length,
    });
  }
  return groups;
}

function taskDocumentPaths(docs) {
  return taskDocumentGroups(docs).flatMap((group) => group.kinds.flatMap((kind) => kind.paths));
}

function taskDocumentSets(task) {
  const sets = [];
  if (taskDocumentPaths(task?.docs).length) {
    sets.push({ target: "项目", docs: task.docs });
  }
  const targets = Array.isArray(task?.targets) ? task.targets : [];
  targets.forEach((target, index) => {
    if (!target || typeof target !== "object" || Array.isArray(target)) return;
    if (!taskDocumentPaths(target.docs).length) return;
    sets.push({ target: taskTargetId(target, index), docs: target.docs });
  });
  return sets;
}

function taskDocsBadges(task) {
  const documentSets = taskDocumentSets(task);
  const docs = taskDocsInfo(task);
  const requirements = docs.hasRequirements;
  const design = docs.hasDesign;
  const specs = docs.specsPresent;
  if (!documentSets.length && !requirements && !design && !specs) {
    return [{ label: "Docs 缺失", tone: "danger" }];
  }
  return [
    { label: `文档关联 ${documentSets.length}`, tone: documentSets.length ? "done" : "danger" },
    { label: `Requirements ${requirements ? "✓" : "缺失"}`, tone: requirements ? "done" : "danger" },
    { label: `Design ${design ? "✓" : "缺失"}`, tone: design ? "done" : "danger" },
    { label: `Specs ${specs ? "✓" : "缺失"}`, tone: specs ? "done" : "danger" },
  ];
}

function taskDocsInfo(task) {
  const documentSets = taskDocumentSets(task);
  const docsList = documentSets.length ? documentSets.map((entry) => entry.docs) : [task?.docs || {}];
  const phases = docsList.flatMap((docs) => Array.isArray(docs?.phases) ? docs.phases : [docs]);
  const values = (key) => phases.flatMap((phase) => {
    const value = phase?.[key];
    return Array.isArray(value) ? value : value ? [value] : [];
  });
  const requirementsList = Array.from(new Set([...values("requirements"), ...values("prd")]));
  const designList = Array.from(new Set(values("design")));
  const specsList = Array.from(new Set([...values("specs"), ...values("spec")]));
  return {
    module: Array.from(new Set(phases.map((phase) => phase?.module).filter(Boolean))).join(", "),
    requirementsList,
    designList,
    specsList,
    hasRequirements: requirementsList.length > 0,
    hasDesign: designList.length > 0,
    specsPresent: specsList.length > 0,
  };
}

function groupTasks(tasks) {
  const byGroup = new Map(taskGroupDefinitions.map((group) => [group.id, { ...group, tasks: [] }]));
  const fallback = { id: "other", label: "其他", states: [], tasks: [] };
  tasks.forEach((task) => {
    const status = taskStatus(task);
    const group = taskGroupDefinitions.find((item) => item.states.includes(status));
    if (group) {
      byGroup.get(group.id).tasks.push(task);
    } else {
      fallback.tasks.push(task);
    }
  });
  const groups = Array.from(byGroup.values()).filter((group) => group.tasks.length);
  if (fallback.tasks.length) groups.push(fallback);
  return groups;
}

function clueStatusLabel(status) {
  return {
    open: "可升级任务",
    needs_confirmation: "需要确认",
    task_created: "已创建任务",
    resolved: "已解决",
    false_positive: "误报",
    misconfigured: "读取失败",
  }[status] || status || "未知";
}

function clueDecisionLabel(decision) {
  return {
    keep_open: "保留",
    needs_confirmation: "需要确认",
    create_task: "升级任务",
    resolve: "已解决",
    false_positive: "误报",
  }[decision] || decision;
}

function groupClues(clues) {
  const knownStatuses = new Set(clueGroupDefinitions.flatMap((group) => group.statuses));
  const groups = clueGroupDefinitions
    .map((group) => ({
      ...group,
      clues: clues.filter((clue) => group.statuses.includes(clue.status || "open")),
    }))
    .filter((group) => group.clues.length);
  const otherClues = clues.filter((clue) => !knownStatuses.has(clue.status || "open"));
  if (otherClues.length) groups.push({ id: "other", label: "其他", statuses: [], clues: otherClues });
  return groups;
}

function clueSubjectText(clue) {
  const subject = clue?.subject || {};
  if (!subject.kind && !subject.id) return "无";
  return `${subject.kind || "unknown"}:${subject.id || "unknown"}`;
}

function evidenceList(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value.map((item) => String(item || "").trim()).filter(Boolean);
  return [String(value).trim()].filter(Boolean);
}

function runArtifactRows(run) {
  const artifacts = run?.artifacts && typeof run.artifacts === "object" ? run.artifacts : {};
  return [
    ["Prompt", artifacts.prompt_path],
    ["Command", artifacts.command_path],
    ["Request", artifacts.request_path],
    ["Events", artifacts.events_path || run?.log_path],
    ["Last message", artifacts.last_message_path || run?.last_message_path],
    ["Worker result", artifacts.worker_result_path || run?.worker_result_path],
    ["Result", artifacts.result_path || run?.result_path],
  ].filter(([, value]) => value);
}

function docHealthModel(project, taskPayload) {
  const tasks = Array.isArray(taskPayload?.tasks) ? taskPayload.tasks : [];
  const clues = Array.isArray(project?.clues) ? project.clues : [];
  const activeClues = clues.filter((clue) => ["open", "needs_confirmation"].includes(clue.status || "open"));
  const rows = tasks.map((task) => {
    const docs = taskDocsInfo(task);
    return {
      id: task.id,
      title: task.title || task.id,
      status: taskStatus(task),
      priority: task.priority || "P2",
      docs,
      blocker: currentBlockerReasonFrom(task),
      targets: taskTargetOrder(task) || "单项目",
      updatedAt: task.updated_at,
    };
  });
  const requirementsCount = new Set(rows.flatMap((row) => row.docs.requirementsList)).size;
  const designCount = new Set(rows.flatMap((row) => row.docs.designList)).size;
  const specsCount = new Set(rows.flatMap((row) => row.docs.specsList)).size;
  const codeRefClues = activeClues.filter((clue) => ["code_without_docs", "spec_code_ref_missing", "spec_test_ref_missing", "spec_code_reference_missing", "spec_test_reference_missing"].includes(clue.type));
  return {
    rows,
    activeClues,
    metrics: [
      { label: "Requirements", value: requirementsCount },
      { label: "Design", value: designCount },
      { label: "Specs", value: specsCount },
      { label: "代码引用问题", value: codeRefClues.length },
      { label: "未处理线索", value: activeClues.length },
    ],
  };
}

function allTaskEntries(projects, taskPayloads) {
  return projects.flatMap((project) => {
    const tasks = Array.isArray(taskPayloads[project.project_id]?.tasks) ? taskPayloads[project.project_id].tasks : [];
    return tasks.map((task) => ({ project, task }));
  });
}

function allRunEntries(projects) {
  return projects.flatMap((project) => {
    const runs = Array.isArray(project.runs) ? project.runs : [];
    return runs.map((run) => ({ project, run }));
  });
}

function allClueEntries(projects) {
  return projects.flatMap((project) => {
    const clues = Array.isArray(project.clues) ? project.clues : [];
    return clues.map((clue) => ({ project, clue }));
  });
}

function timeValue(value) {
  if (!value) return 0;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? 0 : date.getTime();
}

function buildInboxModel(projects, taskPayloads) {
  const taskEntries = allTaskEntries(projects, taskPayloads);
  const clueEntries = allClueEntries(projects)
    .filter(({ clue }) => ["open", "needs_confirmation"].includes(clue.status))
    .sort((left, right) => timeValue(right.clue.last_seen_at || right.clue.created_at) - timeValue(left.clue.last_seen_at || left.clue.created_at));
  const waiting = taskEntries
    .filter(({ task }) => inboxTaskStates.has(taskStatus(task)))
    .sort((left, right) => {
      const leftStatus = taskStatus(left.task);
      const rightStatus = taskStatus(right.task);
      return (
        (inboxStatusOrder[leftStatus] ?? 9) - (inboxStatusOrder[rightStatus] ?? 9)
        || priorityRank(left.task.priority) - priorityRank(right.task.priority)
        || timeValue(right.task.updated_at) - timeValue(left.task.updated_at)
      );
    });
  const running = projects
    .filter((project) => project.project_status === "running" || project.status?.active_task?.agent_status === "running")
    .map((project) => ({ project, run: project.latest_run || {}, task: project.status?.active_task || {} }));
  const runs = allRunEntries(projects)
    .sort((left, right) => timeValue(right.run.ended_at || right.run.started_at) - timeValue(left.run.ended_at || left.run.started_at));
  return {
    waiting,
    running,
    recentRuns: runs.filter(({ run }) => run.status !== "running").slice(0, 6),
    newClues: clueEntries.slice(0, 8),
    queue: [
      { label: "待验收", value: waiting.filter(({ task }) => taskStatus(task) === "ready_for_review").length, tone: "review" },
      { label: "需求阻塞", value: waiting.filter(({ task }) => specBlockedTaskStates.has(taskStatus(task))).length, tone: "spec" },
      { label: "开发阻塞", value: waiting.filter(({ task }) => devBlockedTaskStates.has(taskStatus(task))).length, tone: "dev" },
      { label: "待合入", value: waiting.filter(({ task }) => taskStatus(task) === "accepted").length, tone: "merge" },
      { label: "收尾中", value: waiting.filter(({ task }) => taskStatus(task) === "merged").length, tone: "cleanup" },
      { label: "新线索", value: clueEntries.length, tone: "clue" },
    ],
    projectPressure: projectPressureRows(waiting, clueEntries),
  };
}

function projectPressureRows(waiting, clues = []) {
  const counts = new Map();
  waiting.forEach(({ project, task }) => {
    const targets = Array.isArray(task.targets) && task.targets.length ? task.targets : [project.project_id];
    targets.forEach((target) => {
      const projectId = typeof target === "string" ? target : target?.project || target?.project_id || project.project_id;
      counts.set(projectId, (counts.get(projectId) || 0) + 1);
    });
  });
  clues.forEach(({ project }) => {
    counts.set(project.project_id, (counts.get(project.project_id) || 0) + 1);
  });
  const rows = Array.from(counts.entries())
    .map(([projectId, value]) => ({ projectId, name: projectId, value }))
    .sort((left, right) => right.value - left.value || left.name.localeCompare(right.name, "zh-Hans"))
    .slice(0, 5);
  const max = Math.max(...rows.map((row) => row.value), 1);
  return rows.map((row) => ({ ...row, width: Math.max(8, Math.round((row.value / max) * 100)) }));
}

function diagnosticRows(diagnostic) {
  if (!diagnostic || diagnostic.has_signal === false) return [];
  return [
    ["任务", diagnostic.item_title || diagnostic.item_id],
    ["任务 ID", diagnostic.item_id],
    ["子目标", diagnostic.target_id],
    ["子仓", diagnostic.repo],
    ["Worktree", diagnostic.worktree],
    ["分支", diagnostic.branch],
    ["失败类型", diagnostic.failure_kind],
    ["失败命令", diagnostic.failed_command],
    ["脏文件", Array.isArray(diagnostic.dirty_files) ? diagnostic.dirty_files.join("\n") : ""],
    ["证据", diagnostic.artifacts?.result_path || diagnostic.artifacts?.last_message_path || diagnostic.artifacts?.events_path],
  ].filter(([, value]) => value);
}

function compactDiagnosticRows(diagnostic) {
  if (!diagnostic || diagnostic.has_signal === false) return [];
  const dirtyFiles = Array.isArray(diagnostic.dirty_files) ? diagnostic.dirty_files : [];
  return [
    ["类型", diagnostic.failure_kind],
    ["子目标", diagnostic.target_id],
    ["子仓", diagnostic.repo],
    ["Worktree", diagnostic.worktree],
    ["命令", diagnostic.failed_command],
    ["脏文件", dirtyFiles.slice(0, 3).join(", ")],
  ].filter(([, value]) => value);
}

function App() {
  const [payload, setPayload] = useState(null);
  const [selectedId, setSelectedId] = useState(readSelectedProjectIdFromUrl);
  const [view, setView] = useState(readSelectedProjectIdFromUrl() ? "project" : "inbox");
  const [message, setMessage] = useState(null);
  const [busy, setBusy] = useState(false);
  const [projectBusy, setProjectBusy] = useState(false);
  const [taskBusy, setTaskBusy] = useState(false);
  const [configBusy, setConfigBusy] = useState(false);
  const [taskPayloads, setTaskPayloads] = useState({});
  const [scheduler, setScheduler] = useState(null);
  const [settings, setSettings] = useState(null);
  const [tokenUsage, setTokenUsage] = useState({ items: [], issues: [], loading: true, error: "" });
  const [tokenProjectId, setTokenProjectId] = useState("");
  const [navigationRequest, setNavigationRequest] = useState(null);
  const [projectOrder, setProjectOrder] = useState(readProjectOrder);

  const rawProjects = payload?.projects || [];
  const projects = useMemo(() => applyProjectOrder(rawProjects, projectOrder), [rawProjects, projectOrder]);
  const selected = useMemo(() => selectedId ? projects.find((item) => item.project_id === selectedId) || null : null, [projects, selectedId]);
  const inbox = useMemo(() => buildInboxModel(projects, taskPayloads), [projects, taskPayloads]);

  function selectProject(projectId, { syncUrl = true } = {}) {
    setNavigationRequest(null);
    setSelectedId(projectId || null);
    setView(projectId ? "project" : "inbox");
    if (syncUrl) {
      writeSelectedProjectIdToUrl(projectId || null);
    }
  }

  function openProjectEntry(projectId, tab, entryId = "") {
    selectProject(projectId);
    setNavigationRequest({ projectId, tab, entryId, issuedAt: Date.now() });
  }

  function openProjectTask(projectId, taskId) {
    openProjectEntry(projectId, "tasks", taskId === undefined ? "create" : taskId);
  }

  function openProjectRun(projectId, runId = "") {
    openProjectEntry(projectId, "runs", runId);
  }

  function selectInbox() {
    selectProject(null);
  }

  function selectStaticView(nextView) {
    setView(nextView);
    setSelectedId(null);
    writeSelectedProjectIdToUrl(null);
  }

  function reorderProjects(sourceId, targetId, placement) {
    const nextProjects = moveProject(projects, sourceId, targetId, placement);
    const nextOrder = nextProjects.map((project) => project.project_id);
    setProjectOrder(nextOrder);
    saveProjectOrder(nextOrder);
  }

  async function refreshDashboard() {
    const [next, schedulerPayload, settingsPayload] = await Promise.all([
      api("/api/v1/projects"),
      api("/api/v1/scheduler"),
      api("/api/v1/settings"),
    ]);
    setPayload(next);
    setScheduler(schedulerPayload.scheduler || null);
    setSettings(settingsPayload.settings || null);
    const nextProjects = next.projects || [];
    const selectedStillExists = selectedId ? nextProjects.some((project) => project.project_id === selectedId) : true;
    if (selectedId && !selectedStillExists) {
      selectProject(null, { syncUrl: false });
    }
    if (selectedId && !selectedStillExists && readSelectedProjectIdFromUrl()) {
      writeSelectedProjectIdToUrl(null);
    }
    return nextProjects;
  }

  async function refresh({ includeTasks = true } = {}) {
    const nextProjects = await refreshDashboard();
    if (includeTasks) {
      await refreshProjectTasks(nextProjects);
    }
    await refreshTokenUsage(tokenProjectId);
  }

  async function refreshTokenUsage(projectId = tokenProjectId) {
    setTokenUsage((current) => ({ ...current, loading: true, error: "" }));
    const params = new URLSearchParams({ limit: "20" });
    if (projectId) params.set("project_id", projectId);
    try {
      const next = await api(`/api/v1/token-usage/tasks?${params.toString()}`);
      setTokenUsage({
        items: Array.isArray(next.items) ? next.items : [],
        issues: Array.isArray(next.issues) ? next.issues : [],
        loading: false,
        error: "",
      });
    } catch (error) {
      setTokenUsage((current) => ({ ...current, loading: false, error: error.message || "Token 用量加载失败" }));
    }
  }

  async function selectTokenProject(projectId) {
    setTokenProjectId(projectId);
    await refreshTokenUsage(projectId);
  }

  async function refreshProjectTasks(nextProjects = projects) {
    const entries = await Promise.all(nextProjects.map(async (project) => {
      try {
        const next = await api(`/api/v1/projects/${project.project_id}/tasks`);
        return [project.project_id, next];
      } catch (error) {
        return [project.project_id, { status: "failed", summary: error.message, tasks: [] }];
      }
    }));
    setTaskPayloads((current) => ({ ...current, ...Object.fromEntries(entries) }));
  }

  async function refreshTasks(projectId = selected?.project_id) {
    if (!projectId) return;
    const next = await api(`/api/v1/projects/${projectId}/tasks`);
    setTaskPayloads((current) => ({ ...current, [projectId]: next }));
  }

  function flash(text, error = false) {
    setMessage({ text, error });
    window.clearTimeout(flash.timer);
    flash.timer = window.setTimeout(() => setMessage(null), 4200);
  }

  async function runAction(projectId, action, bodyPayload = {}, options = {}) {
    const quiet = options.quiet === true;
    const refreshAfter = options.refreshAfter !== false;
    try {
      setBusy(true);
      if (!quiet && action === "run-once" && bodyPayload.loop_type === "scan") {
        flash("正在提交项目巡检。");
      } else if (!quiet && action === "start-run") {
        flash("正在提交运行任务。");
      }
      if (!quiet && action === "resume-once") {
        flash("正在提交继续运行请求。");
      }
      if (action === "latest-report") {
        const reportPayload = await api(`/api/v1/projects/${projectId}/latest-report`);
        const report = reportPayload.report || {};
        if (!report.exists) {
          flash("当前项目暂无 HTML report");
          return;
        }
        window.open(`/reports/${projectId}/${report.run_id || "latest"}?token=${encodeURIComponent(token)}`, "_blank", "noopener");
        return;
      }
      const result = action === "resume-once" && String(bodyPayload.reason || "").trim()
        ? await resumeWithInstruction({ request: api, projectId, reason: bodyPayload.reason })
        : await api(`/api/v1/projects/${projectId}/${action}`, {
            method: "POST",
            body: JSON.stringify(bodyPayload),
          });
      const plan = result.plan?.command ? `：${result.plan.command.join(" ")}` : "";
      const summary = result.status === "accepted"
        ? result.summary || "已受理，任务将在后台继续运行"
        : result.summary || "操作完成";
      if (!quiet) flash(summary + plan);
      if (refreshAfter) {
        await refresh();
        await refreshTasks(projectId);
      }
      return result;
    } catch (error) {
      if (quiet) return { status: "failed", summary: error.message || "请求失败" };
      flash(error.message, true);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function cancelRun(entry) {
    const projectId = entry?.project?.project_id;
    const runId = entry?.task?.agent_run_id || entry?.run?.run_id || "";
    if (!projectId) return;
    try {
      setBusy(true);
      const result = await api(`/api/v1/projects/${projectId}/cancel-run`, {
        method: "POST",
        body: JSON.stringify({ run_id: runId, reason: "用户在控制台取消" }),
      });
      flash(result.summary || "取消请求已记录");
      await refresh();
    } catch (error) {
      flash(error.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function resolveBlocker(projectId, reason) {
    try {
      setBusy(true);
      flash("已提交处理说明，准备恢复运行。");
      const result = await api(`/api/v1/projects/${projectId}/resolve-and-resume`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      });
      flash(result.summary || "阻塞已处理并触发恢复运行");
      await refresh();
      await refreshTasks(projectId);
      return true;
    } catch (error) {
      flash(error.message, true);
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function schedule() {
    try {
      setBusy(true);
      const result = await api("/api/v1/schedule-tick", { method: "POST", body: "{}" });
      flash(result.summary || "调度完成");
      await refresh();
    } catch (error) {
      flash(error.message, true);
    } finally {
      setBusy(false);
    }
  }

  async function onboardProject(request) {
    try {
      setProjectBusy(true);
      const endpoint = request.apply ? "/api/v1/projects/onboarding/apply" : "/api/v1/projects/onboarding/preview";
      const result = await api(endpoint, {
        method: "POST",
        body: JSON.stringify(request),
      });
      if (result.status === "blocked" || result.status === "misconfigured") {
        flash(result.summary || "项目接入被阻塞", true);
        return result;
      }
      flash(result.summary || (request.apply ? "项目接入完成" : "预检完成"));
      if (request.apply) {
        const importedId = result.profile?.id || result.profile?.project_id;
        await refresh({ includeTasks: false });
        if (importedId) {
          selectProject(importedId);
          await refreshTasks(importedId);
        }
      }
      return result;
    } catch (error) {
      flash(error.message, true);
      return { status: "failed", summary: error.message };
    } finally {
      setProjectBusy(false);
    }
  }

  async function removeProject(projectId) {
    const target = projects.find((project) => project.project_id === projectId);
    const name = target?.name || projectId;
    if (!window.confirm(`从 LoopForge 移除项目“${name}”？项目目录、data/tasks 和运行历史不会被删除。`)) {
      return false;
    }
    try {
      setProjectBusy(true);
      const result = await api(`/api/v1/projects/${projectId}/remove`, {
        method: "POST",
        body: "{}",
      });
      flash(result.summary || "项目已移除");
      setTaskPayloads((current) => {
        const next = { ...current };
        delete next[projectId];
        return next;
      });
      await refresh();
      return true;
    } catch (error) {
      flash(error.message, true);
      return false;
    } finally {
      setProjectBusy(false);
    }
  }

  async function addTask(projectId, mode, payload) {
    try {
      setTaskBusy(true);
      const path = mode === "ai" ? `/api/v1/projects/${projectId}/tasks/ai-create` : `/api/v1/projects/${projectId}/tasks`;
      const result = await api(path, {
        method: "POST",
        body: JSON.stringify(payload),
      });
      flash(result.summary || "任务已创建");
      await refresh();
      await refreshTasks(projectId);
      return true;
    } catch (error) {
      flash(error.message, true);
      return false;
    } finally {
      setTaskBusy(false);
    }
  }

  async function taskAction(projectId, taskId, action, bodyPayload = {}) {
    try {
      setTaskBusy(true);
      const result = action === "review"
        ? await submitReviewWithContinuation({
            request: api,
            projectId,
            taskId,
            decision: bodyPayload.decision,
            feedback: bodyPayload.feedback,
          })
        : await api(`/api/v1/projects/${projectId}/tasks/${encodeURIComponent(taskId)}/${action}`, {
            method: "POST",
            body: JSON.stringify(bodyPayload),
          });
      flash(result.summary || "任务动作已完成", result.status === "failed");
      await refresh();
      await refreshTasks(projectId);
      return result;
    } catch (error) {
      flash(error.message, true);
      return null;
    } finally {
      setTaskBusy(false);
    }
  }

  async function clueAction(projectId, clueId, decision, bodyPayload = {}) {
    try {
      setTaskBusy(true);
      const result = await api(`/api/v1/projects/${projectId}/clues/decide`, {
        method: "POST",
        body: JSON.stringify({
          clue_id: clueId,
          decision,
          ...bodyPayload,
        }),
      });
      flash(result.summary || "线索裁决已保存");
      await refresh();
      await refreshTasks(projectId);
      return true;
    } catch (error) {
      flash(error.message, true);
      return false;
    } finally {
      setTaskBusy(false);
    }
  }

  async function updateGlobalNotificationConfig(config) {
    try {
      setConfigBusy(true);
      const result = await api("/api/v1/settings/notification-config", {
        method: "POST",
        body: JSON.stringify(config),
      });
      flash(result.summary || "全局通知配置已保存");
      await refresh({ includeTasks: false });
      return true;
    } catch (error) {
      flash(error.message, true);
      return false;
    } finally {
      setConfigBusy(false);
    }
  }

  async function updateRuntimeConfig(projectId, config) {
    try {
      setConfigBusy(true);
      const currentProject = projects.find((project) => project.project_id === projectId);
      const runIsActive = currentProject?.project_status === "running"
        || currentProject?.status?.active_task?.agent_status === "running";
      const result = await api(`/api/v1/projects/${projectId}/runtime-config`, {
        method: "POST",
        body: JSON.stringify(config),
      });
      flash(runIsActive
        ? "运行配置已保存；当前运行不受影响，下次继续运行使用新配置。"
        : result.summary || "运行配置已保存；下一轮使用新配置。");
      await refresh({ includeTasks: false });
      return true;
    } catch (error) {
      flash(error.message, true);
      return false;
    } finally {
      setConfigBusy(false);
    }
  }

  useEffect(() => {
    refresh().catch((error) => flash(error.message, true));
  }, []);

  useEffect(() => {
    if (!selected?.project_id) return;
    refreshTasks(selected.project_id).catch((error) => flash(error.message, true));
  }, [selected?.project_id]);

  useEffect(() => {
    if (!busy) return undefined;
    const timer = window.setInterval(() => {
      refresh({ includeTasks: false }).catch((error) => flash(error.message, true));
    }, 700);
    return () => window.clearInterval(timer);
  }, [busy, selectedId]);

  const counts = payload?.summary_counts || {};
  const pageTitle = view === "project" && selected ? "项目工作台" : view === "runs" ? "运行归档" : view === "settings" ? "全局设置" : "待处理";
  const pageCopy = view === "project" && selected
    ? `当前查看 ${projectDisplayName(selected)} 的状态与任务。`
    : view === "runs"
      ? "按时间查看所有项目的运行结果与证据。"
      : view === "settings"
        ? "通知与巡检配置。"
        : "需要你处理的事项与最近结果。";

  return (
    <main className="app-shell">
      <WorkspaceSidebar
        busy={busy || projectBusy}
        inboxCount={inbox.waiting.length}
        onImport={onboardProject}
        onRemove={removeProject}
        onReorderProjects={reorderProjects}
        onSelectInbox={selectInbox}
        onSelectProject={selectProject}
        onSelectStaticView={selectStaticView}
        onboardingOwner={settings?.onboarding_defaults?.owner || ""}
        projects={projects}
        scheduler={scheduler}
        selectedId={selected?.project_id}
        view={view}
      />
      <section className="workspace-main">
        <header className="topbar">
          <div>
            <p className="kicker">LOOPFORGE / 工作空间</p>
            <h1>{pageTitle}</h1>
            <p className="topbar-copy">{pageCopy}</p>
          </div>
          <div className="toolbar">
            {view === "project" && selected ? <button className="button primary" type="button" onClick={() => openProjectTask(selected.project_id)} disabled={busy}>新建任务</button> : null}
            <button className="button secondary" type="button" onClick={() => refresh()} disabled={busy}>刷新</button>
          </div>
        </header>

        <RunningTray running={inbox.running} busy={busy} onCancelRun={cancelRun} onOpenProject={selectProject} />

        <div className="workspace-content">
          {message ? <section className={`notice ${message.error ? "error" : ""}`}>{message.text}</section> : null}
          {scheduler && scheduler.enabled === false ? (
            <section className="notice error">后台自动调度未启动；项目定时频率只会计算下次时间，不会自动触发。请重启后端并开启自动调度。</section>
          ) : null}

          {view === "project" && selected ? (
            <DetailPane
              project={selected}
              scheduler={scheduler}
              busy={busy}
              configBusy={configBusy}
              taskBusy={taskBusy}
              taskPayload={taskPayloads[selected.project_id]}
              navigationRequest={navigationRequest}
              addTask={addTask}
              resolveBlocker={resolveBlocker}
              runAction={runAction}
              clueAction={clueAction}
              taskAction={taskAction}
              updateRuntimeConfig={updateRuntimeConfig}
            />
          ) : view === "runs" ? (
            <GlobalRunsView projects={projects} onOpenProject={openProjectRun} />
          ) : view === "settings" ? (
            <GlobalSettingsView busy={configBusy} onSaveNotification={updateGlobalNotificationConfig} projects={projects} scheduler={scheduler} settings={settings} />
          ) : (
            <InboxView
              counts={counts}
              inbox={inbox}
              busy={busy || taskBusy}
              onOpenProject={selectProject}
              onOpenTask={openProjectTask}
              onOpenRun={openProjectRun}
              projects={projects}
              taskAction={taskAction}
              tokenProjectId={tokenProjectId}
              tokenUsage={tokenUsage}
              onTokenProjectChange={selectTokenProject}
            />
          )}
        </div>
      </section>
    </main>
  );
}

function WorkspaceSidebar({ busy, inboxCount, onImport, onRemove, onReorderProjects, onSelectInbox, onSelectProject, onSelectStaticView, onboardingOwner, projects, scheduler, selectedId, view }) {
  const [draggingId, setDraggingId] = useState("");
  const [dropTarget, setDropTarget] = useState(null);
  const [orderMessage, setOrderMessage] = useState("");
  const draggingProjectRef = useRef("");
  const touchTargetRef = useRef(null);

  function dropPlacement(event, element) {
    const rect = element.getBoundingClientRect();
    const horizontal = window.matchMedia("(max-width: 920px)").matches;
    return horizontal
      ? event.clientX < rect.left + rect.width / 2 ? "before" : "after"
      : event.clientY < rect.top + rect.height / 2 ? "before" : "after";
  }

  function describeMove(sourceId, targetId, placement) {
    const source = projects.find((project) => project.project_id === sourceId);
    const target = projects.find((project) => project.project_id === targetId);
    if (!source || !target) return;
    setOrderMessage(`${projectDisplayName(source)} 已移到 ${projectDisplayName(target)}${placement === "after" ? "之后" : "之前"}`);
  }

  function commitMove(sourceId, targetId, placement) {
    if (!targetId || sourceId === targetId) return;
    onReorderProjects(sourceId, targetId, placement);
    describeMove(sourceId, targetId, placement);
  }

  function handlePointerMove(event) {
    if (!draggingProjectRef.current) return;
    const row = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-project-id]");
    if (!row) return;
    const projectId = row.dataset.projectId;
    const placement = dropPlacement(event, row);
    touchTargetRef.current = { projectId, placement };
    setDropTarget(touchTargetRef.current);
  }

  function handlePointerEnd(event) {
    const target = touchTargetRef.current;
    if (target) commitMove(draggingProjectRef.current, target.projectId, target.placement);
    draggingProjectRef.current = "";
    touchTargetRef.current = null;
    setDraggingId("");
    setDropTarget(null);
  }

  function cancelPointerMove(event) {
    draggingProjectRef.current = "";
    touchTargetRef.current = null;
    setDraggingId("");
    setDropTarget(null);
  }

  function handleOrderKey(event, projectId) {
    if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const index = projects.findIndex((project) => project.project_id === projectId);
    if (index < 0) return;
    if (event.key === "Home" && index > 0) commitMove(projectId, projects[0].project_id, "before");
    if (event.key === "End" && index < projects.length - 1) commitMove(projectId, projects.at(-1).project_id, "after");
    if (event.key === "ArrowUp" && index > 0) commitMove(projectId, projects[index - 1].project_id, "before");
    if (event.key === "ArrowDown" && index < projects.length - 1) commitMove(projectId, projects[index + 1].project_id, "after");
  }

  return (
    <aside className="workspace-sidebar">
      <div className="brand-block">
        <div className="brand-title"><img className="brand-mark" src="/loopforge-icon.svg" alt="" /><span>LoopForge</span></div>
        <p>工程工作台</p>
      </div>
      <nav className="workspace-nav" aria-label="主导航">
        <button className={`nav-item ${view === "inbox" ? "active" : ""}`} type="button" onClick={onSelectInbox}>
          <span>Inbox</span>
          <strong>{inboxCount}</strong>
        </button>
        <div className="nav-group-title">项目</div>
        <div className="project-tree" aria-describedby="project-order-help">
          {projects.length ? projects.map((project) => (
            <div
              className={`project-nav-row ${draggingId === project.project_id ? "dragging" : ""} ${dropTarget?.projectId === project.project_id ? `drop-${dropTarget.placement}` : ""}`}
              data-project-id={project.project_id}
              key={project.project_id}
            >
              <button
                className="project-drag-handle"
                type="button"
                aria-label={`调整 ${projectDisplayName(project)} 的顺序`}
                title="拖动调整顺序；也可使用方向键、Home 或 End"
                onKeyDown={(event) => handleOrderKey(event, project.project_id)}
                onPointerDown={(event) => {
                  event.currentTarget.setPointerCapture(event.pointerId);
                  draggingProjectRef.current = project.project_id;
                  setDraggingId(project.project_id);
                }}
                onPointerMove={handlePointerMove}
                onPointerUp={handlePointerEnd}
                onPointerCancel={cancelPointerMove}
              >
                <span aria-hidden="true">⋮⋮</span>
              </button>
              <button
                className={`project-nav-item ${project.project_id === selectedId ? "active" : ""}`}
                type="button"
                onClick={() => onSelectProject(project.project_id)}
              >
                <span className={`project-dot ${project.project_status || "idle"}`} />
                <span className="project-nav-main">
                  <span className="project-nav-name">{projectDisplayName(project)}</span>
                  <AutomationModeBadge compact project={project} />
                </span>
                <strong>{project.status?.backlog?.active ?? 0}</strong>
              </button>
            </div>
          )) : <div className="compact-empty">暂无项目</div>}
          <span className="sr-only" id="project-order-help">拖动项目调整顺序，或聚焦排序按钮后使用上下方向键、Home 和 End。</span>
          <span className="sr-only" role="status" aria-live="polite">{orderMessage}</span>
        </div>
        <button className={`nav-item ${view === "runs" ? "active" : ""}`} type="button" onClick={() => onSelectStaticView("runs")}>
          <span>运行记录</span>
          <strong>{projects.reduce((total, project) => total + (project.runs?.length || 0), 0)}</strong>
        </button>
        <button className={`nav-item ${view === "settings" ? "active" : ""}`} type="button" onClick={() => onSelectStaticView("settings")}>
          <span>设置</span>
        </button>
      </nav>
      <details className="sidebar-import-panel">
        <summary>导入或接入项目</summary>
        <ProjectImportForm busy={busy} defaultOwner={onboardingOwner} onImport={onImport} />
      </details>
      <div className="sidebar-foot">
        <StatusBadge value={scheduler?.enabled ? "running" : "blocked"} label={scheduler?.enabled ? "自动调度运行中" : "自动调度未启动"} />
        <p>{projects.length} 个项目 · {inboxCount} 个待处理</p>
      </div>
      <details className="sidebar-remove-list">
        <summary>管理项目</summary>
        {projects.map((project) => (
          <button key={`remove-${project.project_id}`} type="button" disabled={busy} onClick={() => onRemove(project.project_id)}>
            移除 {projectDisplayName(project)}
          </button>
        ))}
      </details>
    </aside>
  );
}

function RunningTray({ running, busy, onCancelRun, onOpenProject }) {
  if (!running.length) return null;
  return (
    <section className="running-tray" aria-label="全局运行中">
      <div className="running-tray-label"><strong>正在运行</strong><span>{running.length} 个项目</span></div>
      <div className="running-items">
        {running.map((entry) => {
          const { project, run, task } = entry;
          return (
            <div className="running-entry" key={`${project.project_id}-${run.run_id || task.id || "running"}`}>
              <button className="running-pill" type="button" onClick={() => onOpenProject(project.project_id)}>
                <StatusBadge value="running" label="运行中" />
                <span>{projectDisplayName(project)}</span>
                <strong>{task.title || run.task_title || run.run_id || "无任务"}</strong>
              </button>
              <button className="running-cancel" type="button" disabled={busy} onClick={() => onCancelRun(entry)} aria-label={`取消 ${projectDisplayName(project)} 的运行`}>取消</button>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function InboxView({ counts, inbox, busy, onOpenProject, onOpenTask, onOpenRun, projects, taskAction, tokenProjectId, tokenUsage, onTokenProjectChange }) {
  return (
    <div className="inbox-view">
      <section className="status-strip" aria-label="Inbox 指标">
        <Metric tone="danger" value={inbox.waiting.length} label="待我处理" />
        <Metric tone="warning" value={inbox.queue.find((item) => item.label === "待验收")?.value || 0} label="待验收" />
        <Metric tone="danger" value={(counts.blocked || 0) + inbox.queue.filter((item) => item.label.includes("阻塞")).reduce((total, item) => total + item.value, 0)} label="阻塞" />
        <Metric tone="active" value={inbox.running.length || counts.running || 0} label="运行中" />
        <Metric tone="calm" value={inbox.newClues.length} label="新线索" />
        <Metric tone="done" value={counts.completed || 0} label="已完成" />
      </section>

      <InboxSection title="待我处理" count={inbox.waiting.length}>
        {inbox.waiting.length ? inbox.waiting.map(({ project, task }) => (
          <InboxTaskRow
            busy={busy}
            key={`${project.project_id}-${task.id}`}
            onOpenTask={onOpenTask}
            project={project}
            task={task}
            taskAction={taskAction}
          />
        )) : <div className="compact-empty">当前没有需要你处理的任务。</div>}
      </InboxSection>

      <section className="inbox-insights" aria-label="Inbox 图表">
        <QueuePressureChart queue={inbox.queue} total={inbox.waiting.length + inbox.newClues.length} />
        <ProjectHotspots rows={inbox.projectPressure} projects={projects} />
      </section>

      <TaskTokenTrend
        data={tokenUsage}
        onProjectChange={onTokenProjectChange}
        projectId={tokenProjectId}
        projects={projects}
      />

      <InboxSection title="新线索" count={inbox.newClues.length}>
        {inbox.newClues.length ? inbox.newClues.map(({ project, clue }) => (
          <InboxClueRow key={`${project.project_id}-${clue.id}`} project={project} clue={clue} onOpenProject={onOpenProject} />
        )) : <div className="compact-empty">当前没有新的项目巡检线索。</div>}
      </InboxSection>

      <InboxSection title="最近结果" count={inbox.recentRuns.length}>
        {inbox.recentRuns.length ? inbox.recentRuns.map(({ project, run }) => (
          <InboxRunRow key={`${project.project_id}-${run.run_id || run.started_at}`} project={project} run={run} onOpenRun={onOpenRun} />
        )) : <div className="compact-empty">暂无运行记录。</div>}
      </InboxSection>
    </div>
  );
}

function formatTokenCount(value) {
  if (!Number.isFinite(value)) return "—";
  return new Intl.NumberFormat("zh-CN").format(value);
}

function compactTokenCount(value) {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1000000) return `${(value / 1000000).toFixed(value >= 10000000 ? 0 : 1)}M`;
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}K`;
  return String(value);
}

function formatUsd(value) {
  if (!Number.isFinite(value)) return "—";
  const digits = value < 0.01 ? 6 : value < 1 ? 4 : 2;
  return `$${value.toFixed(digits)}`;
}

function compactUsd(value) {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1000) return `$${(value / 1000).toFixed(value >= 10000 ? 0 : 1)}K`;
  if (value >= 1) return `$${value.toFixed(value >= 10 ? 0 : 1)}`;
  return `$${value.toFixed(value < 0.01 ? 4 : 2)}`;
}

function costEstimateCopy(item) {
  const estimate = item.cost_estimate || {};
  if (estimate.coverage === "unavailable") return "无法估价：运行缺少模型、用量，或该模型没有公开单价。";
  if (estimate.coverage === "partial") {
    return `部分估价：${estimate.priced_run_count || 0} 次已计价、${estimate.missing_run_count || 0} 次未计价，当前金额是已知下限。`;
  }
  if ((estimate.long_context_run_count || 0) > 0) return "已完整计价；因长上下文费率条件，金额显示为可能区间。";
  return "已按每次运行登记的模型完整计价。";
}

function costEstimateRange(estimate) {
  if (!Number.isFinite(estimate?.minimum_usd)) return "—";
  if (Number.isFinite(estimate.maximum_usd) && estimate.maximum_usd > estimate.minimum_usd) {
    return `${formatUsd(estimate.minimum_usd)}–${formatUsd(estimate.maximum_usd)}`;
  }
  return formatUsd(estimate.minimum_usd);
}

function modelRunsCopy(item) {
  const modelRuns = Array.isArray(item.model_runs) ? item.model_runs : [];
  if (!modelRuns.length) return "未登记";
  return modelRuns.map((run) => {
    const provider = run.provider === "codex" ? "Codex" : run.provider === "claude" ? "Claude" : "未知 Worker";
    const reasoning = run.reasoning_effort ? ` · ${run.reasoning_effort}` : "";
    const reportedCost = Number.isFinite(run.reported_cost_usd) ? ` · Provider 报告 ${formatUsd(run.reported_cost_usd)}` : "";
    return `${provider} · ${run.model || "未登记模型"}${reasoning} · ${run.run_count || 0} 次${reportedCost}`;
  }).join("；");
}

function tokenCoverageCopy(item) {
  if (item.coverage === "partial") {
    return `部分统计：缺少 ${item.missing_run_count || 0} 次运行，当前值为已知下限，实际消耗可能更高。`;
  }
  if (item.coverage === "unavailable") return "无统计数据：历史运行没有可用的 Worker usage。";
  return "统计完整：已覆盖任务的全部 Worker 运行。";
}

function TaskTokenTrend({ data, projectId, projects, onProjectChange }) {
  const [tooltip, setTooltip] = useState(null);
  const [metric, setMetric] = useState("cost");
  const items = Array.isArray(data?.items) ? data.items : [];
  const projectNames = useMemo(
    () => new Map(projects.map((project) => [project.project_id, projectDisplayName(project)])),
    [projects],
  );
  const chart = useMemo(() => {
    const width = Math.max(680, items.length * 76 + 96);
    const height = 300;
    const margin = { top: 24, right: 28, bottom: 66, left: 62 };
    const plotWidth = width - margin.left - margin.right;
    const plotHeight = height - margin.top - margin.bottom;
    const values = items.map((item) => (
      metric === "cost" ? item.cost_estimate?.minimum_usd : item.usage?.total_tokens
    ));
    const maximum = Math.max(...values.filter(Number.isFinite), metric === "cost" ? 0.000001 : 1);
    const points = items.map((item, index) => {
      const x = margin.left + (items.length === 1 ? plotWidth / 2 : (index / Math.max(items.length - 1, 1)) * plotWidth);
      const value = metric === "cost" ? item.cost_estimate?.minimum_usd : item.usage?.total_tokens;
      const known = Number.isFinite(value);
      const y = known ? margin.top + plotHeight - (value / maximum) * plotHeight : margin.top + plotHeight;
      return { item, x, y, known, value };
    });
    const segments = [];
    let current = [];
    points.forEach((point) => {
      if (point.known) {
        current.push(point);
      } else if (current.length) {
        segments.push(current);
        current = [];
      }
    });
    if (current.length) segments.push(current);
    return { width, height, margin, plotHeight, maximum, points, segments };
  }, [items, metric]);

  function showTooltip(point) {
    setTooltip({
      item: point.item,
      left: Math.min(chart.width - 324, Math.max(8, point.x + 14)),
      top: Math.min(chart.height - 244, Math.max(8, point.y - 28)),
    });
  }

  return (
    <article className="insight-card token-trend-card" aria-label="任务 Token 趋势">
      <div className="token-trend-head">
        <div>
          <h2>任务 Token 趋势</h2>
          <p>最近 20 个已完成任务，默认比较 API 等价估算，可切换为 Token；项目只用于筛选关联任务。</p>
        </div>
        <div className="token-trend-controls">
          <label className="token-project-filter">
            <span>比较指标</span>
            <select value={metric} onChange={(event) => { setMetric(event.target.value); setTooltip(null); }}>
              <option value="cost">价格估算</option>
              <option value="tokens">Token</option>
            </select>
          </label>
          <label className="token-project-filter">
            <span>关联项目</span>
            <select disabled={data?.loading} value={projectId} onChange={(event) => onProjectChange(event.target.value)}>
              <option value="">全部项目</option>
              {projects.map((project) => <option key={project.project_id} value={project.project_id}>{projectDisplayName(project)}</option>)}
            </select>
          </label>
        </div>
      </div>
      {data?.error ? (
        <div className="token-trend-state error">
          <strong>Token 趋势暂时不可用</strong>
          <span>{data.error}</span>
        </div>
      ) : !items.length ? (
        <div className="token-trend-state">
          <strong>{data?.loading ? "正在读取 Token 用量…" : "暂无已完成任务用量"}</strong>
          <span>完成带有 Worker usage 的任务后，这里会显示趋势。</span>
        </div>
      ) : (
        <div className="token-chart-scroll">
          <div className="token-chart-stage" style={{ width: chart.width }}>
            <svg className="token-trend-svg" width={chart.width} height={chart.height} viewBox={`0 0 ${chart.width} ${chart.height}`} role="img" aria-label={`最近完成任务的${metric === "cost" ? "API 等价价格估算" : "总 Token"}折线图`}>
              {[0, 0.5, 1].map((ratio) => {
                const y = chart.margin.top + chart.plotHeight - ratio * chart.plotHeight;
                return (
                  <g key={ratio}>
                    <line className="token-grid-line" x1={chart.margin.left} x2={chart.width - chart.margin.right} y1={y} y2={y} />
                    <text className="token-axis-label" x={chart.margin.left - 10} y={y + 4} textAnchor="end">{metric === "cost" ? compactUsd(chart.maximum * ratio) : compactTokenCount(chart.maximum * ratio)}</text>
                  </g>
                );
              })}
              {chart.segments.map((segment, index) => segment.length > 1 ? (
                <path
                  className="token-trend-line"
                  d={segment.map((point, pointIndex) => `${pointIndex ? "L" : "M"}${point.x},${point.y}`).join(" ")}
                  key={`segment-${index}`}
                />
              ) : null)}
              {chart.points.map((point, index) => (
                <g
                  key={`${point.item.owner_project_id}-${point.item.task_id}`}
                >
                  {point.known ? (
                    <>
                      <circle
                        aria-label={`${point.item.title || point.item.task_id}：${metric === "cost" ? `${formatUsd(point.value)} API 等价估算` : `${formatTokenCount(point.value)} Token`}`}
                        className={`token-point ${metric === "cost" ? point.item.cost_estimate?.coverage : point.item.coverage}`}
                        cx={point.x}
                        cy={point.y}
                        onBlur={() => setTooltip(null)}
                        onFocus={() => showTooltip(point)}
                        onMouseEnter={() => showTooltip(point)}
                        onMouseLeave={() => setTooltip(null)}
                        r="5"
                        role="button"
                        tabIndex="0"
                      />
                      <text className="token-value-label" x={point.x} y={Math.max(13, point.y - 11)} textAnchor="middle">
                        {metric === "cost" ? compactUsd(point.value) : compactTokenCount(point.value)}
                      </text>
                    </>
                  ) : (
                    <>
                      <circle
                        aria-label={`${point.item.title || point.item.task_id}：无统计数据`}
                        className="token-point unavailable"
                        cx={point.x}
                        cy={point.y}
                        onBlur={() => setTooltip(null)}
                        onFocus={() => showTooltip(point)}
                        onMouseEnter={() => showTooltip(point)}
                        onMouseLeave={() => setTooltip(null)}
                        r="10"
                        role="button"
                        tabIndex="0"
                      />
                      <g className="token-unknown-marker">
                        <line x1={point.x - 5} x2={point.x + 5} y1={point.y - 5} y2={point.y + 5} />
                        <line x1={point.x - 5} x2={point.x + 5} y1={point.y + 5} y2={point.y - 5} />
                      </g>
                    </>
                  )}
                  <text className="token-task-label" x={point.x} y={chart.height - 34} textAnchor="end" transform={`rotate(-35 ${point.x} ${chart.height - 34})`}>
                    {(point.item.title || point.item.task_id || `任务 ${index + 1}`).slice(0, 14)}
                  </text>
                </g>
              ))}
            </svg>
            {tooltip ? (
              <div className="token-tooltip" role="status" style={{ left: tooltip.left, top: tooltip.top }}>
                <strong>{tooltip.item.title || tooltip.item.task_id}</strong>
                <span>{formatTime(tooltip.item.completed_at)}</span>
                <dl>
                  <div><dt>关联项目</dt><dd>{tooltip.item.associated_project_ids.map((id) => projectNames.get(id) || id).join("、")}</dd></div>
                  <div><dt>模型与等级</dt><dd>{modelRunsCopy(tooltip.item)}</dd></div>
                  <div><dt>价格估算</dt><dd>{costEstimateRange(tooltip.item.cost_estimate)}</dd></div>
                  <div><dt>输入</dt><dd>{formatTokenCount(tooltip.item.usage?.input_tokens)}</dd></div>
                  <div><dt>缓存输入</dt><dd>{formatTokenCount(tooltip.item.usage?.cached_input_tokens)}</dd></div>
                  <div><dt>输出</dt><dd>{formatTokenCount(tooltip.item.usage?.output_tokens)}</dd></div>
                  <div><dt>推理输出</dt><dd>{formatTokenCount(tooltip.item.usage?.reasoning_output_tokens)}</dd></div>
                  <div><dt>总 Token</dt><dd>{formatTokenCount(tooltip.item.usage?.total_tokens)}</dd></div>
                  <div><dt>运行覆盖</dt><dd>{tooltip.item.run_count || 0} 次 / 缺 {tooltip.item.missing_run_count || 0} 次</dd></div>
                </dl>
                <p>{costEstimateCopy(tooltip.item)} API 等价估算不等于实际账单。</p>
                <p>{tokenCoverageCopy(tooltip.item)}</p>
              </div>
            ) : null}
          </div>
        </div>
      )}
      {data?.issues?.length ? <p className="token-history-note">{data.issues.length} 条历史记录无法完整读取，已跳过异常证据。</p> : null}
    </article>
  );
}

function QueuePressureChart({ queue, total }) {
  const safeTotal = Math.max(total, queue.reduce((sum, item) => sum + item.value, 0), 1);
  return (
    <article className="insight-card">
      <div className="insight-head">
        <h2>队列压力</h2>
        <span>{total} 个待处理信号</span>
      </div>
      <div className="queue-strip" aria-label="待处理队列按状态拆分">
        {queue.map((item) => (
          <span
            className={`queue-segment tone-${item.tone} ${item.value ? "" : "zero"}`}
            key={item.label}
            style={{ "--value": item.value || 0, "--basis": `${Math.max(0, Math.round((item.value / safeTotal) * 100))}%` }}
            title={`${item.label} ${item.value}`}
          />
        ))}
      </div>
      <div className="insight-legend">
        {queue.map((item) => (
          <div className="legend-item" key={item.label}>
            <span className={`legend-dot tone-${item.tone}`} />
            <span>{item.label}</span>
            <strong>{item.value}</strong>
          </div>
        ))}
      </div>
    </article>
  );
}

function ProjectHotspots({ rows, projects }) {
  const projectById = new Map(projects.map((project) => [project.project_id, project]));
  return (
    <article className="insight-card">
      <div className="insight-head">
        <h2>项目热点</h2>
        <span>含自动化模式</span>
      </div>
      <div className="project-bars">
        {rows.length ? rows.map((row) => {
          const project = projectById.get(row.projectId);
          return (
            <div className="project-bar-row" key={row.projectId}>
              <span className="project-bar-main">
                <span className="project-bar-name">{project ? projectDisplayName(project) : row.name}</span>
                {project ? <AutomationModeBadge compact project={project} /> : null}
              </span>
              <div className="bar-track"><div className="bar-fill" style={{ "--bar": `${row.width}%` }} /></div>
              <span className="bar-count">{row.value}</span>
            </div>
          );
        }) : <div className="compact-empty">当前没有项目压力。</div>}
      </div>
    </article>
  );
}

function InboxSection({ title, count, children }) {
  return (
    <section className="section inbox-section">
      <div className="section-head">
        <h2>{title}</h2>
        <span className="status-badge idle">{count} 项</span>
      </div>
      <div className="inbox-list">{children}</div>
    </section>
  );
}

function InboxTaskRow({ project, task, busy, onOpenTask, taskAction }) {
  const status = taskStatus(task);
  const blocker = currentBlockerReasonFrom(task);
  const hasDirectAction = ["ready_for_review", "accepted", "merged"].includes(status) || task.cleanup_pending === "abandon";
  return (
    <article className="inbox-row">
      <span className={`row-severity ${statusClass(status)}`} />
      <div className="inbox-row-main">
        <div className="inbox-row-title">
          <StatusBadge value={status} label={taskStateLabel(status, task.state_label)} />
          <strong>{task.title || task.id}</strong>
          <span className="pill dark">{projectDisplayName(project)}</span>
          <span className={`pill ${priorityRank(task.priority) <= 1 ? "danger" : "active"}`}>{task.priority || "P2"}</span>
        </div>
        <p>{blocker || task.description || "未填写说明。"}</p>
        <div className="inbox-row-meta">
          {taskDocsBadges(task).map((badge) => <span className={`pill ${badge.tone}`} key={badge.label}>{badge.label}</span>)}
          {task.agent_run_id ? <span className="pill dark">{task.agent_run_id}</span> : null}
          <span className="pill dark">{formatTime(task.updated_at)}</span>
        </div>
      </div>
      <div className="inbox-row-actions">
        <button className="button secondary" type="button" onClick={() => onOpenTask(project.project_id, task.id)}>查看任务</button>
        {hasDirectAction ? (
          <TaskActionButtons busy={busy} compact onTaskAction={taskAction} projectId={project.project_id} task={task} />
        ) : (
          <button className="button primary" type="button" onClick={() => onOpenTask(project.project_id, task.id)}>{devBlockedTaskStates.has(status) || specBlockedTaskStates.has(status) ? "查看阻塞" : "打开任务"}</button>
        )}
      </div>
    </article>
  );
}

function InboxRunRow({ project, run, task, onOpenRun }) {
  const status = run.status || project.project_status;
  return (
    <article className="inbox-row">
      <span className={`row-severity ${statusClass(status)}`} />
      <div className="inbox-row-main">
        <div className="inbox-row-title">
          <StatusBadge value={status} label={runLabel(run)} />
          <strong>{run.run_id || task?.agent_run_id || task?.title || "运行中"}</strong>
          <span className="pill dark">{projectDisplayName(project)}</span>
        </div>
        <p>{run.summary || task?.next_action || "等待运行结果。"}</p>
        <div className="inbox-row-meta">
          {run.task_title ? <span className="pill dark">{run.task_title}</span> : null}
          {run.trigger ? <span className="pill active">{triggerLabel(run.trigger)}</span> : null}
          <span className="pill dark">{formatTime(run.ended_at || run.started_at)}</span>
        </div>
      </div>
      <div className="inbox-row-actions">
        <button className="button secondary" type="button" onClick={() => onOpenRun(project.project_id, run.run_id || "")}>查看运行</button>
      </div>
    </article>
  );
}

function InboxClueRow({ project, clue, onOpenProject }) {
  const status = clue.status || "open";
  return (
    <article className="inbox-row">
      <span className={`row-severity ${statusClass(status)}`} />
      <div className="inbox-row-main">
        <div className="inbox-row-title">
          <StatusBadge value={status} label={status === "needs_confirmation" ? "待确认" : "新线索"} />
          <strong>{clue.summary || clue.id || "未命名线索"}</strong>
          <span className="pill dark">{projectDisplayName(project)}</span>
        </div>
        <p>{clue.dedupe_key || "等待项目巡检补充证据。"}</p>
        <div className="inbox-row-meta">
          {clue.type ? <span className="pill active">{clue.type}</span> : null}
          {clue.severity ? <span className="pill dark">{clue.severity}</span> : null}
          {clue.subject?.id ? <span className="pill dark">{clue.subject.id}</span> : null}
          <span className="pill dark">{formatTime(clue.last_seen_at || clue.created_at)}</span>
        </div>
      </div>
      <div className="inbox-row-actions">
        <button className="button secondary" type="button" onClick={() => onOpenProject(project.project_id)}>打开项目</button>
      </div>
    </article>
  );
}

function GlobalRunsView({ projects, onOpenProject }) {
  const runs = allRunEntries(projects)
    .sort((left, right) => timeValue(right.run.ended_at || right.run.started_at) - timeValue(left.run.ended_at || left.run.started_at));
  return (
    <section className="section">
      <div className="section-head">
        <h2>运行记录</h2>
        <span className="subtle">{runs.length} 条</span>
      </div>
      <div className="global-run-list">
        {runs.length ? runs.map(({ project, run }) => (
          <RunDetailCard key={`${project.project_id}-${run.run_id || run.started_at}`} project={project} run={run} onOpenProject={onOpenProject} />
        )) : <div className="compact-empty">暂无运行记录。</div>}
      </div>
    </section>
  );
}

function GlobalSettingsView({ busy, onSaveNotification, projects, scheduler, settings }) {
  const wecom = settings?.notifications?.wecom || {};
  const scan = settings?.scan || {};
  return (
    <div className="settings-stack">
      <section className="section">
        <div className="section-head">
          <h2>全局设置</h2>
          <StatusBadge value={scheduler?.enabled ? "running" : "blocked"} label={scheduler?.enabled ? "自动调度运行中" : "自动调度未启动"} />
        </div>
        <div className="settings-overview">
          <KV label="项目数" value={projects.length} />
          <KV label="企微通知" value={wecom.enabled ? "已启用" : "未启用"} />
          <KV label="Webhook 来源" value={wecom.webhook_url ? "全局 URL" : wecom.webhook_env || "LOOPFORGE_WECOM_WEBHOOK"} />
          <KV label="日常扫描预算" value={`${scan.daily_minutes || 15} 分钟`} />
          <KV label="预算告警阈值" value={`连续 ${scan.budget_exceeded_alert_after || 2} 轮`} />
          <KV label="配置文件" value={settings?.config_path || "未创建"} />
        </div>
      </section>
      <GlobalNotificationSettings busy={busy} onSave={onSaveNotification} settings={settings} />
    </div>
  );
}

function GlobalNotificationSettings({ busy, onSave, settings }) {
  const wecom = settings?.notifications?.wecom || {};
  const [enabled, setEnabled] = useState(false);
  const [webhookEnv, setWebhookEnv] = useState("LOOPFORGE_WECOM_WEBHOOK");
  const [webhookUrl, setWebhookUrl] = useState("");

  useEffect(() => {
    setEnabled(wecom.enabled === true);
    setWebhookEnv(wecom.webhook_env || "LOOPFORGE_WECOM_WEBHOOK");
    setWebhookUrl(wecom.webhook_url || "");
  }, [wecom.enabled, wecom.webhook_env, wecom.webhook_url]);

  async function submit(event) {
    event.preventDefault();
    await onSave({
      enabled,
      webhook_env: webhookEnv.trim() || "LOOPFORGE_WECOM_WEBHOOK",
      webhook_url: webhookUrl.trim(),
    });
  }

  return (
    <form className="notification-settings section" onSubmit={submit}>
      <div className="task-manager-head">
        <div>
          <h3>企微通知</h3>
          <p className="task-flow-copy">停止态、待验收、合入失败和连续预算不足时发送摘要；无可推进任务不通知。</p>
        </div>
        <StatusBadge value={enabled ? "idle" : "blocked"} label={enabled ? "已启用" : "未启用"} />
      </div>
      <div className="notification-grid">
        <label className="checkbox-line">
          <input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} disabled={busy} />
          <span>启用全局企微通知</span>
        </label>
        <label>
          <span>Webhook 环境变量</span>
          <input value={webhookEnv} onChange={(event) => setWebhookEnv(event.target.value)} placeholder="LOOPFORGE_WECOM_WEBHOOK" disabled={busy || !enabled} />
        </label>
        <label>
          <span>Webhook URL</span>
          <input
            value={webhookUrl}
            onChange={(event) => setWebhookUrl(event.target.value)}
            placeholder="可选；填写后优先于环境变量"
            disabled={busy || !enabled}
          />
        </label>
      </div>
      <div className="action-row">
        <button className="button secondary" type="submit" disabled={busy}>保存全局通知配置</button>
      </div>
    </form>
  );
}

function Metric({ tone, value, label }) {
  return (
    <div className={`metric ${tone}`}>
      <span className="metric-value">{value}</span>
      <span className="metric-label">{label}</span>
    </div>
  );
}

function ProjectImportForm({ busy, defaultOwner, onImport }) {
  const [rootDir, setRootDir] = useState("");
  const [projectType, setProjectType] = useState("go-backend");
  const [projectId, setProjectId] = useState("");
  const [owner, setOwner] = useState(defaultOwner || "");
  const [planningAdapter, setPlanningAdapter] = useState("builtin");
  const [buildTarget, setBuildTarget] = useState("");
  const [projectGroupChildren, setProjectGroupChildren] = useState("");
  const [preview, setPreview] = useState(null);

  useEffect(() => {
    setOwner((current) => current || defaultOwner || "");
  }, [defaultOwner]);

  function resetPreview() {
    setPreview(null);
  }

  async function submit(event) {
    event.preventDefault();
    const request = {
      root_dir: rootDir.trim(),
      project_type: projectType,
      project_id: projectId.trim(),
      owner: owner.trim(),
      planning_adapter: planningAdapter,
      build_target: buildTarget,
    };
    if (projectType === "project-group" && projectGroupChildren.trim()) {
      request.project_group_children = projectGroupChildren
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean)
        .map((line) => {
          const separator = line.indexOf("=");
          return separator > 0
            ? { key: line.slice(0, separator).trim(), path: line.slice(separator + 1).trim() }
            : { key: "", path: line };
        });
    }
    if (!preview) {
      const result = await onImport(request);
      if (result) setPreview(result);
      return;
    }
    const result = await onImport({ ...request, apply: true, plan_hash: preview.plan_hash });
    if (result?.status === "completed") {
      setRootDir("");
      setProjectId("");
      setOwner(defaultOwner || "");
      setProjectGroupChildren("");
      setBuildTarget("");
      setPreview(null);
    }
  }

  return (
    <form className="project-import" onSubmit={submit}>
      <label>
        <span>项目目录</span>
        <input
          value={rootDir}
          onChange={(event) => { setRootDir(event.target.value); resetPreview(); }}
          placeholder="/path/to/project"
          disabled={busy}
        />
      </label>
      <label>
        <span>项目类型</span>
        <select value={projectType} onChange={(event) => { setProjectType(event.target.value); resetPreview(); }} disabled={busy}>
          <option value="project-group">多仓项目组</option>
          <option value="go-backend">Go 后端</option>
          <option value="react-frontend">React 前端</option>
          <option value="flutter-app">Flutter App</option>
          <option value="java-backend">Java 后端</option>
          <option value="python-scripts">Python 脚本</option>
        </select>
      </label>
      <label>
        <span>项目 ID（可选）</span>
        <input value={projectId} onChange={(event) => { setProjectId(event.target.value); resetPreview(); }} placeholder="默认取目录名" disabled={busy} />
      </label>
      <label>
        <span>Owner（必填）</span>
        <input value={owner} onChange={(event) => { setOwner(event.target.value); resetPreview(); }} placeholder="项目负责人标识" disabled={busy} />
        {!owner.trim() ? <small className="warning">Owner 不能为空。</small> : null}
      </label>
      {projectType === "project-group" ? (
        <label className="project-group-children">
          <span>项目组子仓（可选，每行 key=path）</span>
          <textarea
            value={projectGroupChildren}
            onChange={(event) => { setProjectGroupChildren(event.target.value); resetPreview(); }}
            placeholder={"api=/path/to/api\nadmin=/path/to/admin"}
            disabled={busy}
          />
        </label>
      ) : null}
      {projectType === "flutter-app" ? (
        <label>
          <span>Flutter 构建目标</span>
          <select value={buildTarget} onChange={(event) => { setBuildTarget(event.target.value); resetPreview(); }} disabled={busy}>
            <option value="">自动检测</option>
            <option value="apk">Android APK</option>
            <option value="appbundle">Android App Bundle</option>
            <option value="web">Web</option>
            <option value="ios">iOS</option>
            <option value="ipa">iOS IPA</option>
            <option value="macos">macOS</option>
            <option value="linux">Linux</option>
            <option value="windows">Windows</option>
          </select>
        </label>
      ) : null}
      <label>
        <span>研发工作流</span>
        <select value={planningAdapter} onChange={(event) => { setPlanningAdapter(event.target.value); resetPreview(); }} disabled={busy}>
          <option value="builtin">LoopForge builtin</option>
          <option value="openspec">OpenSpec</option>
        </select>
      </label>
      {preview ? (
        <div className={`onboarding-preview ${preview.status}`}>
          <strong>{preview.status === "completed" ? "预检通过" : "预检未通过"}</strong>
          <span>{preview.files?.filter((item) => item.action === "create").length || 0} create · {preview.files?.filter((item) => item.action === "update").length || 0} update · {preview.files?.filter((item) => item.action === "skip").length || 0} skip</span>
          {preview.workflow_initialization ? (
            <small>
              工作流：{preview.workflow_initialization.provider} · {preview.workflow_initialization.action} · AI tools：{preview.workflow_initialization.ai_tools?.join(" + ") || "无"} · Spec template：{preview.workflow_initialization.spec_template}
            </small>
          ) : null}
          {preview.blockers?.map((blocker) => <small key={`${blocker.code}-${blocker.path || ""}`}>{blocker.summary}</small>)}
          {preview.warnings?.map((warning) => <small className="warning" key={`${warning.code}-${warning.path || ""}`}>{warning.summary}</small>)}
        </div>
      ) : null}
      <button className={`button ${preview?.status === "completed" ? "primary" : "secondary"}`} type="submit" disabled={busy || !rootDir.trim() || !owner.trim() || (preview && preview.status !== "completed")}>
        {preview ? "确认生成" : "预检"}
      </button>
    </form>
  );
}

function ProjectList({ projects, scheduler, selectedId, onSelect, onRemove, busy }) {
  if (!projects.length) {
    return <div className="empty-state"><h2>暂无项目</h2><p>检查 projects 配置是否存在。</p></div>;
  }
  return (
    <div className="project-list">
      {projects.map((project) => {
        const latest = project.latest_run;
        const activeTask = project.status?.active_task;
        const activeBlockerReason = currentBlockerReasonFrom(activeTask);
        const issue = project.issue || {};
        const cardDiagnostic = activeTask?.diagnostic || issue.diagnostic;
        const cardDiagnosticRows = compactDiagnosticRows(cardDiagnostic);
        const issueCopy = issue.required_action || issue.summary || "";
        const secondaryIssueCopy = activeBlockerReason && issueCopy && issueCopy !== activeBlockerReason ? issueCopy : "";
        return (
          <article
            className={`project-item ${project.project_id === selectedId ? "selected" : ""}`}
            key={project.project_id}
            onClick={() => onSelect(project.project_id)}
          >
            <div className="row-top">
              <div>
                <h3>{project.name}</h3>
                <p className="project-meta">{project.project_id} · priority {project.priority}</p>
                <p className="project-path" title={project.root_dir}>{project.root_dir}</p>
              </div>
              <div className="project-card-actions">
                <StatusBadge value={project.project_status} label={project.project_status_label} />
                <button
                  className="project-remove-button"
                  type="button"
                  disabled={busy}
                  onClick={(event) => {
                    event.stopPropagation();
                    onRemove(project.project_id);
                  }}
                >
                  移除
                </button>
              </div>
            </div>
            <p className="project-meta">当前任务：{activeTask?.title || "无"}</p>
            <p className="project-meta">任务状态：{taskStateLabel(activeTask?.state, activeTask?.state_label)}</p>
            {activeBlockerReason || issueCopy ? (
              <ProjectIssueSummary
                label={activeBlockerReason ? "阻塞原因" : "当前判断"}
                text={activeBlockerReason || issueCopy}
                secondaryText={secondaryIssueCopy}
              />
            ) : null}
            {cardDiagnosticRows.length ? (
              <div className="project-diagnostic-inline" aria-label="项目诊断摘要">
                {cardDiagnosticRows.map(([label, value]) => (
                  <span key={label}><strong>{label}</strong>{value}</span>
                ))}
              </div>
            ) : null}
            <p className="project-meta">定时频率：{scheduleDisplayLabel(project, scheduler)}</p>
            <p className="project-meta">最近运行：{latest ? `${runLabel(latest)} · ${formatTime(latest.ended_at || latest.started_at)}` : "无"}</p>
          </article>
        );
      })}
    </div>
  );
}

function ProjectIssueSummary({ label, text, secondaryText }) {
  const hasDetails = text.length > 64 || secondaryText;
  return (
    <div className="project-alert" onClick={(event) => event.stopPropagation()}>
      <div className="project-alert-summary">
        <span>{label}</span>
        <strong>{text}</strong>
      </div>
      {hasDetails ? (
        <details className="project-alert-details">
          <summary>查看完整说明</summary>
          <p>{text}</p>
          {secondaryText ? <p><span>当前判断</span>{secondaryText}</p> : null}
        </details>
      ) : null}
    </div>
  );
}

function DetailPane({ project, scheduler, busy, configBusy, taskBusy, taskPayload, navigationRequest, addTask, resolveBlocker, runAction, clueAction, taskAction, updateRuntimeConfig }) {
  const [activeTab, setActiveTab] = useState("overview");
  const [focusedEntry, setFocusedEntry] = useState(null);
  useEffect(() => {
    setActiveTab("overview");
    setFocusedEntry(null);
  }, [project?.project_id]);
  useEffect(() => {
    if (navigationRequest?.projectId !== project?.project_id) return;
    setActiveTab(navigationRequest.tab);
    setFocusedEntry(navigationRequest.entryId ? { tab: navigationRequest.tab, id: navigationRequest.entryId, issuedAt: navigationRequest.issuedAt } : null);
  }, [navigationRequest, project?.project_id]);
  function navigateToEntry(tab, id = "") {
    setActiveTab(tab);
    setFocusedEntry(id ? { tab, id, issuedAt: Date.now() } : null);
  }
  if (!project) {
    return <aside className="detail-pane"><div className="empty-state"><h2>选择一个项目</h2><p>查看 active task、最近运行、事件历史和操作区。</p></div></aside>;
  }
  const latest = project.latest_run;
  const status = project.status || {};
  const currentTask = status.active_task || {};
  const flowTask = status.active_task || latest?.active_task || {};
  const blockerReason = currentBlockerReasonFrom(currentTask);
  const blockingCurrentTask = isBlockedTask(currentTask);
  const currentDiagnostic = currentTask.diagnostic || project.issue?.diagnostic;
  return (
    <aside className="detail-pane">
      <div className="section-head project-detail-head">
        <div className="project-detail-title">
          <div className="project-title-row">
            <h2>{project.name}</h2>
            <ProjectHeaderStatus value={project.project_status} label={project.project_status_label} />
          </div>
          <span className="subtle project-root-path">{project.root_dir}</span>
        </div>
        <div className="project-detail-actions">
          <RunActions
            busy={busy}
            currentTask={currentTask}
            hasQueuedTasks={Boolean(taskPayload?.tasks?.length || status.has_open_backlog)}
            latestRunStatus={latest?.status}
            navigateToEntry={navigateToEntry}
            project={project}
            runAction={runAction}
            setActiveTab={setActiveTab}
          />
        </div>
      </div>
      <ProjectTabs activeTab={activeTab} onSelect={(tab) => navigateToEntry(tab)} />
      <div className="detail-body">
        {activeTab === "overview" ? (
          <ProjectOverviewPanel
            blockerReason={blockerReason}
            blockingCurrentTask={blockingCurrentTask}
            busy={busy}
            currentDiagnostic={currentDiagnostic}
            currentTask={currentTask}
            flowTask={flowTask}
            latest={latest}
            project={project}
            resolveBlocker={resolveBlocker}
            runAction={runAction}
            scheduler={scheduler}
            taskAction={taskAction}
            taskBusy={taskBusy}
            taskPayload={taskPayload}
            focusedEntry={focusedEntry}
            onNavigate={navigateToEntry}
          />
        ) : null}
        {activeTab === "tasks" ? (
          <TaskManager
            addTask={addTask}
            busy={busy || taskBusy}
            project={project}
            taskAction={taskAction}
            taskPayload={taskPayload}
            focusedEntry={focusedEntry}
            activeTaskId={currentTask.id}
            onHandleBlocker={() => navigateToEntry("overview", "recovery")}
          />
        ) : null}
        {activeTab === "clues" ? (
          <ProjectCluesPanel busy={busy || taskBusy} clues={project.clues || []} onClueAction={clueAction} projectId={project.project_id} />
        ) : null}
        {activeTab === "runs" ? (
          <ProjectRunsPanel events={project.events || []} runs={project.runs || []} focusedEntry={focusedEntry} />
        ) : null}
        {activeTab === "docs" ? (
          <DocumentHealthPanel project={project} taskPayload={taskPayload} />
        ) : null}
        {activeTab === "settings" ? (
          <ProjectSettingsPanel busy={busy || configBusy} project={project} scheduler={scheduler} updateRuntimeConfig={updateRuntimeConfig} />
        ) : null}
      </div>
    </aside>
  );
}

function ProjectTabs({ activeTab, onSelect }) {
  return (
    <div className="project-tabs" role="tablist" aria-label="项目详情导航">
      {projectTabs.map((tab) => (
        <button
          aria-selected={activeTab === tab.id}
          className={`project-tab ${activeTab === tab.id ? "active" : ""}`}
          key={tab.id}
          onClick={() => onSelect(tab.id)}
          role="tab"
          type="button"
        >
          {tab.label}
        </button>
      ))}
    </div>
  );
}

function ProjectOverviewPanel({ blockerReason, blockingCurrentTask, busy, currentDiagnostic, currentTask, flowTask, latest, project, resolveBlocker, runAction, taskAction, taskBusy, taskPayload, focusedEntry, onNavigate }) {
  const recoveryRef = useRef(null);
  useEffect(() => {
    if (focusedEntry?.tab !== "overview" || focusedEntry.id !== "recovery") return;
    recoveryRef.current?.scrollIntoView({ block: "center" });
    recoveryRef.current?.querySelector("textarea")?.focus({ preventScroll: true });
  }, [focusedEntry]);
  const tasks = Array.isArray(taskPayload?.tasks) ? taskPayload.tasks : [];
  const needsAction = tasks.filter((task) => inboxTaskStates.has(taskStatus(task)));
  const openClues = (project.clues || []).filter((clue) => ["open", "needs_confirmation"].includes(clue.status || "open"));
  const recentRuns = (project.runs || []).slice().reverse().slice(0, 2);
  const running = project.project_status === "running" || currentTask.agent_status === "running";
  return (
    <div className="project-overview">
      <section className="overview-signals" aria-label="项目总览">
        <div className={`overview-signal primary ${needsAction.length || blockingCurrentTask ? "attention" : ""}`}>
          <span>需要处理</span><strong>{needsAction.length}</strong>
          <p>{blockingCurrentTask ? taskStateLabel(currentTask.state, currentTask.state_label) : needsAction.length ? "查看任务队列" : "当前没有待处理任务"}</p>
        </div>
        <div className="overview-signal"><span>项目任务</span><strong>{tasks.length}</strong><p>按状态查看</p></div>
        <div className="overview-signal"><span>运行状态</span><strong>{running ? "运行中" : "空闲"}</strong><p>{running ? currentTask.title || "等待结果" : "当前没有运行"}</p></div>
        <div className="overview-signal"><span>最近结果</span><strong>{latest ? runLabel(latest) : "暂无"}</strong><p>{latest ? formatTime(latest.ended_at || latest.started_at) : "还没有运行记录"}</p></div>
      </section>
      <section className="overview-queue" aria-label="当前任务队列">
        <div className="overview-section-head"><div><h3>当前任务队列</h3><p>状态、目标与下一步</p></div><button type="button" onClick={() => onNavigate("tasks")}>全部任务 · {tasks.length}</button></div>
        <div className="overview-queue-list">
          {tasks.length ? tasks.slice(0, 5).map((task) => (
            <button className="overview-task-row" key={task.id} type="button" onClick={() => onNavigate("tasks", task.id)}>
              <StatusBadge value={taskStatus(task)} label={taskStateLabel(taskStatus(task), task.state_label)} />
              <span className="overview-task-main"><strong>{task.title || task.id}</strong><small>{taskTargetOrder(task) || "单项目"} · {currentBlockerReasonFrom(task) || task.next_action || task.description || "等待下一步"}</small></span>
              <span className="overview-row-arrow" aria-hidden="true">›</span>
            </button>
          )) : <div className="compact-empty">{taskPayload?.status === "failed" ? taskPayload.summary || "任务列表读取失败" : "当前没有非终态任务。"}</div>}
        </div>
      </section>
      {(blockingCurrentTask || canResumeWithInstruction(currentTask, project)) ? <section className="overview-recovery" aria-label="当前任务处理入口" ref={recoveryRef}>
        {blockerReason ? <BlockerNotice title="当前阻塞原因" reason={blockerReason} compact /> : null}
        {blockingCurrentTask ? <BlockerResolveForm busy={busy} projectId={project.project_id} onResolve={resolveBlocker} /> : null}
        {canResumeWithInstruction(currentTask, project) ? <RunInstructionForm busy={busy} projectId={project.project_id} onResume={runAction} /> : null}
      </section> : null}
      <section className="overview-followup">
        <div className="overview-followup-panel"><div className="overview-section-head"><h3>最近运行</h3><button type="button" onClick={() => onNavigate("runs")}>查看运行记录</button></div>
          {recentRuns.length ? recentRuns.map((run) => <button className="overview-recent-row" type="button" key={`${run.run_id}-${run.started_at}`} onClick={() => onNavigate("runs", run.run_id || "")}><StatusBadge value={run.status} label={runLabel(run)} /><span>{run.task_title || run.summary || run.run_id || "本轮运行"}</span><small>{formatTime(run.ended_at || run.started_at)}</small></button>) : <p className="compact-empty">暂无运行记录。</p>}
        </div>
        <div className="overview-followup-panel"><div className="overview-section-head"><h3>项目线索</h3><button type="button" onClick={() => onNavigate("clues")}>查看线索</button></div><p className="overview-clue-count">{openClues.length} <span>条待处理线索</span></p></div>
      </section>
      {diagnosticRows(currentDiagnostic).length || flowTask.id || flowTask.timeline?.length ? (
        <details className="overview-process"><summary>查看当前任务的诊断与状态流</summary><DiagnosticCard diagnostic={currentDiagnostic} compact /><TaskFlowPanel activeTask={flowTask} busy={busy || taskBusy} onTaskAction={taskAction} projectId={project.project_id} /></details>
      ) : null}
    </div>
  );
}

function ProjectCluesPanel({ busy, clues, onClueAction, projectId, readonly = false }) {
  const [scope, setScope] = useState(DEFAULT_CLUE_SCOPE);
  const filteredClues = visibleClues(clues, scope);
  const groups = groupClues(filteredClues);
  if (!clues.length) {
    return <div className="compact-empty">当前项目没有巡检线索。</div>;
  }
  return (
    <section className="detail-section">
      <div className="section-head inline">
        <h3>线索详情</h3>
        <span className="subtle">{filteredClues.length} / {clues.length} 条</span>
      </div>
      <div className="filter-row">
        <label>
          <span>显示范围</span>
          <select value={scope} onChange={(event) => setScope(event.target.value)}>
            <option value="active">待处理</option>
            <option value="all">全部（含历史）</option>
          </select>
        </label>
      </div>
      {groups.length ? <div className="clue-groups">
        {groups.map((group) => (
          <section className="clue-group" key={group.id}>
            <div className="clue-group-head">
              <h4>{group.label}</h4>
              <span>{group.clues.length}</span>
            </div>
            <div className="clue-list">
              {group.clues.map((clue) => (
                <ClueDetailCard busy={busy} clue={clue} key={clue.id} onClueAction={onClueAction} projectId={projectId} readonly={readonly} />
              ))}
            </div>
          </section>
        ))}
      </div> : <div className="compact-empty">当前范围没有线索。</div>}
    </section>
  );
}

function ClueDetailCard({ busy, clue, onClueAction, projectId, readonly = false }) {
  const evidence = evidenceList(clue.evidence);
  async function decide(decision) {
    const defaultReason = decision === "create_task" ? "确认需要补规格" : "";
    const reason = window.prompt(`${clueDecisionLabel(decision)}原因`, defaultReason);
    if (reason === null) return;
    await onClueAction(projectId, clue.id, decision, { reason: reason.trim() || defaultReason });
  }
  return (
    <article className="clue-card">
      <div className="clue-card-head">
        <div>
          <h4>{clue.summary || clue.id}</h4>
          <p>{clue.dedupe_key || "无 dedupe_key"}</p>
        </div>
        <StatusBadge value={clue.status || "open"} label={clueStatusLabel(clue.status || "open")} />
      </div>
      <div className="clue-meta-grid">
        <DetailRow label="类型" value={clue.type || "unknown"} />
        <DetailRow label="严重级别" value={clue.severity || "medium"} />
        <DetailRow label="Subject" value={clueSubjectText(clue)} />
        <DetailRow label="发现次数" value={clue.seen_count || 1} />
        <DetailRow label="创建时间" value={formatTime(clue.created_at)} />
        <DetailRow label="最后发现" value={formatTime(clue.last_seen_at)} />
        <DetailRow label="关联任务" value={clue.task_id || "无"} />
        <DetailRow label="裁决" value={clue.decision ? `${clueDecisionLabel(clue.decision)} · ${clue.decision_reason || "无备注"}` : "未裁决"} />
      </div>
      <div className="evidence-list">
        <strong>证据</strong>
        {evidence.length ? evidence.map((item, index) => <p key={`${clue.id}-evidence-${index}`}>{item}</p>) : <p className="subtle">暂无证据。</p>}
      </div>
      {readonly ? null : (
        <div className="clue-actions">
          <button className="mini-button" type="button" disabled={busy} onClick={() => decide("keep_open")}>保留</button>
          <button className="mini-button" type="button" disabled={busy} onClick={() => decide("needs_confirmation")}>需要确认</button>
          <button className="mini-button primary-lite" type="button" disabled={busy} onClick={() => decide("create_task")}>升级任务</button>
          <button className="mini-button" type="button" disabled={busy} onClick={() => decide("resolve")}>已解决</button>
          <button className="mini-button danger-lite" type="button" disabled={busy} onClick={() => decide("false_positive")}>误报</button>
        </div>
      )}
    </article>
  );
}

function ProjectRunsPanel({ events, runs, focusedEntry }) {
  const [loopTypeFilter, setLoopTypeFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const sortedRuns = (runs || []).slice().reverse();
  const filteredRuns = sortedRuns.filter((run) => {
    const loopMatches = loopTypeFilter === "all" || (run.loop_type || "dev") === loopTypeFilter;
    const statusMatches = statusFilter === "all" || run.status === statusFilter;
    return loopMatches && statusMatches;
  });
  const loopTypes = Array.from(new Set(sortedRuns.map((run) => run.loop_type || "dev")));
  const statuses = Array.from(new Set(sortedRuns.map((run) => run.status).filter(Boolean)));
  return (
    <section className="detail-section">
      <div className="filter-row">
        <label>
          <span>循环类型</span>
          <select value={loopTypeFilter} onChange={(event) => setLoopTypeFilter(event.target.value)}>
            <option value="all">全部</option>
            {loopTypes.map((value) => <option key={value} value={value}>{loopTypeLabel(value)}</option>)}
          </select>
        </label>
        <label>
          <span>状态</span>
          <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)}>
            <option value="all">全部</option>
            {statuses.map((value) => <option key={value} value={value}>{runLabel({ status: value })}</option>)}
          </select>
        </label>
      </div>
      <HistorySection title="运行记录" emptyTitle="暂无运行记录" emptyBody="点击运行一轮完成当前任务。">
        {filteredRuns.map((run) => <RunDetailCard focusedEntry={focusedEntry} key={`${run.run_id}-${run.started_at}`} run={run} />)}
      </HistorySection>
      <details className="event-history"><summary>事件历史 <span>{events?.length || 0} 条</span></summary>
        <div className="event-history-list">{(events || []).length ? (events || []).slice().reverse().map((event) => (
          <article className="history-item" key={event.event_id}>
            <div className="row-top">
              <strong>{event.event_type}</strong>
              <span className="subtle">{formatTime(event.created_at)}</span>
            </div>
            <p className="history-meta">{event.summary || "无摘要"}</p>
          </article>
        )) : <div className="compact-empty">暂无事件。</div>}</div>
      </details>
    </section>
  );
}

function RunDetailCard({ run, project, onOpenProject, focusedEntry }) {
  const detailsRef = useRef(null);
  useEffect(() => {
    if (focusedEntry?.tab !== "runs" || focusedEntry.id !== run.run_id) return;
    detailsRef.current.open = true;
    detailsRef.current.querySelector("summary")?.focus({ preventScroll: true });
    detailsRef.current.scrollIntoView({ block: "start" });
  }, [focusedEntry, run.run_id]);
  const taskTitle = run.task_title || run.active_task?.title || run.next_task?.title || run.run_id || "本轮运行";
  const summary = run.summary || loopTypeLabel(run.loop_type || "dev");
  const preview = summary.length > 88 ? `${summary.slice(0, 88)}…` : summary;
  return (
    <details className="history-item run-detail-card" ref={detailsRef}>
      <summary className="run-summary-row">
        <StatusBadge value={run.status} label={runLabel(run)} />
        <span className="run-summary-main"><strong>{taskTitle}</strong><small>{project ? `${projectDisplayName(project)} · ` : ""}{preview}</small></span>
        <time>{formatTime(run.ended_at || run.started_at)}</time>
        <span className="run-expand-mark" aria-hidden="true">⌄</span>
      </summary>
      <div className="run-expanded-body">
        <div className="run-expanded-head"><code>{run.run_id || "unknown"}</code><span className="pill active">{loopTypeLabel(run.loop_type || "dev")}</span>{project && onOpenProject ? <button className="mini-button" type="button" onClick={() => onOpenProject(project.project_id, run.run_id || "")}>在项目中查看</button> : null}</div>
        <p className="history-meta">{run.summary || "无摘要"}</p>
        <RunTaskMeta run={run} />
        <RunDecisionPanel run={run} />
        <RunArtifactPanel run={run} />
        <DiagnosticCard diagnostic={run.diagnostic} compact />
        <RunNotificationMeta run={run} />
        {Array.isArray(run.state_path_labels) && run.state_path_labels.length ? (
          <p className="history-meta">本轮完整路径：{run.state_path_labels.join(" 到 ")}</p>
        ) : run.previous_state && run.next_state ? (
          <p className="history-meta">状态迁移：{taskStateLabel(run.previous_state)} 到 {taskStateLabel(run.next_state)}</p>
        ) : null}
        {runBlockerReason(run) ? <BlockerNotice title="本轮阻塞原因" reason={runBlockerReason(run)} compact /> : null}
        {run.required_action ? <p className="history-meta">处理建议：{run.required_action}</p> : null}
      </div>
    </details>
  );
}

function RunDecisionPanel({ run }) {
  const workerResult = run.worker_result || {};
  const validation = run.validation || workerResult.validation || {};
  if (!run.outcome && !run.mapped_status && !workerResult.recommended_status && !validation.status) {
    return null;
  }
  return (
    <div className="run-task-meta" aria-label="运行裁决结果">
      <DetailRow label="裁决结果" value={run.mapped_status || workerResult.recommended_status || run.next_state || "无"} />
      <DetailRow label="Outcome" value={run.outcome || "无"} />
      <DetailRow label="验证状态" value={validation.status || "未记录"} />
      <DetailRow label="Worker Result" value={run.worker_result_source || run.worker_result_path || "未记录"} />
    </div>
  );
}

function RunArtifactPanel({ run }) {
  const rows = runArtifactRows(run);
  if (!rows.length) return null;
  return (
    <div className="artifact-panel" aria-label="运行证据">
      <strong>运行证据</strong>
      <div className="artifact-grid">
        {rows.map(([label, value]) => <DetailRow key={label} label={label} value={value} />)}
      </div>
    </div>
  );
}

function DocumentHealthPanel({ project, taskPayload }) {
  const model = docHealthModel(project, taskPayload);
  return (
    <section className="detail-section">
      <div className="doc-health-metrics" aria-label="文档健康指标">
        {model.metrics.map((metric) => <KV key={metric.label} label={metric.label} value={metric.value} />)}
      </div>
      <div className="doc-health-table" role="table" aria-label="requirements/design/specs/code/test 对齐矩阵">
        <div className="doc-health-row header" role="row">
          <span>任务</span>
          <span>Requirements</span>
          <span>Design</span>
          <span>Specs</span>
          <span>Target</span>
          <span>风险</span>
        </div>
        {model.rows.length ? model.rows.map((row) => (
          <div className="doc-health-row" key={row.id} role="row">
            <span>
              <strong>{row.title}</strong>
              <small>{row.id} · {taskStateLabel(row.status)}</small>
            </span>
            <span>{row.docs.hasRequirements ? row.docs.requirementsList.join("\n") : "缺失"}</span>
            <span>{row.docs.hasDesign ? row.docs.designList.join("\n") : "缺失"}</span>
            <span>{row.docs.specsPresent ? row.docs.specsList.join("\n") : "缺失"}</span>
            <span>{row.targets}</span>
            <span>{row.blocker || (model.activeClues.length ? `${model.activeClues.length} 条未处理线索` : "无")}</span>
          </div>
        )) : (
          <div className="compact-empty">当前没有非终态任务，文档健康可通过项目巡检线索继续观察。</div>
        )}
      </div>
      <ProjectCluesPanel busy={false} clues={model.activeClues} onClueAction={() => {}} projectId={project.project_id} readonly />
    </section>
  );
}

function ProjectSettingsPanel({ busy, project, scheduler, updateRuntimeConfig }) {
  return (
    <section className="detail-section">
      <div className="section-head inline">
        <h3>项目设置</h3>
        <span className="subtle">{scheduleDisplayLabel(project, scheduler)}</span>
      </div>
      <RuntimeSettings project={project} busy={busy} onSave={updateRuntimeConfig} />
    </section>
  );
}

function ProjectHeaderStatus({ value, label }) {
  return (
    <div className={`header-status ${statusClass(value)}`}>
      <span className="header-status-dot" aria-hidden="true" />
      <span>{label || value || "未知"}</span>
    </div>
  );
}

function SettingsDialog({ project, busy, onClose, updateRuntimeConfig }) {
  useEffect(() => {
    function handleKey(event) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [onClose]);

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title" onMouseDown={(event) => event.stopPropagation()}>
        <div className="settings-dialog-head">
          <div>
            <h3 id="settings-title">项目设置</h3>
            <p className="task-flow-copy">{project.name}</p>
          </div>
          <button className="mini-button" type="button" onClick={onClose}>关闭</button>
        </div>
        <RuntimeSettings project={project} busy={busy} onSave={updateRuntimeConfig} />
      </section>
    </div>
  );
}

function RuntimeSettings({ project, busy, onSave }) {
  const [automationMode, setAutomationMode] = useState("off");
  const [scheduleFrequency, setScheduleFrequency] = useState("hourly");
  const [autoCommit, setAutoCommit] = useState(false);
  const [workerHealth, setWorkerHealth] = useState(null);
  const [workerHealthLoading, setWorkerHealthLoading] = useState(false);
  const [workerHealthError, setWorkerHealthError] = useState("");
  const workerRuntime = project.worker_runtime || {};
  const networkProfile = workerRuntime.network || {};
  const [workerProvider, setWorkerProvider] = useState(workerRuntime.provider || "");
  const [providerSettings, setProviderSettings] = useState(() => workerRuntimeSettings(workerRuntime));
  const [workerNetwork, setWorkerNetwork] = useState(() => ({ ...(networkProfile.settings || {}) }));
  const workerProfile = workerRuntime.providers?.[workerProvider]
    || (workerProvider === workerRuntime.provider ? workerRuntime : {});
  const workerSettings = providerSettings[workerProvider] || workerProfile.settings || {};
  const providerOptions = workerRuntime.selectable
    ? workerRuntime.provider_options || []
    : [{ value: workerRuntime.provider || "unknown", label: workerRuntime.provider_label || "当前 Worker" }];
  const runIsActive = project.project_status === "running" || project.status?.active_task?.agent_status === "running";

  useEffect(() => {
    setAutomationMode(automationModeFrom(project));
    setScheduleFrequency(project.schedule_frequency || "hourly");
    setAutoCommit(Boolean(project.auto_commit));
    setWorkerProvider(workerRuntime.provider || "");
    setProviderSettings(workerRuntimeSettings(workerRuntime));
    setWorkerNetwork({ ...(workerRuntime.network?.settings || {}) });
  }, [project.project_id, project.automation_mode, project.schedule_enabled, project.schedule_frequency, project.auto_commit, project.worker_runtime]);

  useEffect(() => {
    let active = true;
    setWorkerHealthLoading(true);
    setWorkerHealthError("");
    api(`/api/v1/worker-health?project_id=${encodeURIComponent(project.project_id)}`)
      .then((result) => {
        if (active) setWorkerHealth(result);
      })
      .catch((error) => {
        if (active) setWorkerHealthError(error.message || "Worker 环境检测失败");
      })
      .finally(() => {
        if (active) setWorkerHealthLoading(false);
      });
    return () => {
      active = false;
    };
  }, [project.project_id]);

  async function refreshWorkerHealth() {
    try {
      setWorkerHealthLoading(true);
      setWorkerHealthError("");
      const result = await api(`/api/v1/worker-health?project_id=${encodeURIComponent(project.project_id)}`);
      setWorkerHealth(result);
    } catch (error) {
      setWorkerHealthError(error.message || "Worker 环境检测失败");
    } finally {
      setWorkerHealthLoading(false);
    }
  }

  function updateWorkerSetting(key, value) {
    setProviderSettings((current) => ({
      ...current,
      [workerProvider]: { ...(current[workerProvider] || workerProfile.settings || {}), [key]: value },
    }));
  }

  function updateWorkerNetwork(key, value) {
    setWorkerNetwork((current) => ({ ...current, [key]: value }));
  }

  async function submit(event) {
    event.preventDefault();
    await onSave(project.project_id, {
      automation_mode: automationMode,
      schedule_enabled: automationMode !== "off",
      schedule_frequency: scheduleFrequency,
      auto_commit: autoCommit,
      worker_network: workerNetwork,
      ...(workerRuntime.selectable ? {
        worker_provider: workerProvider,
        worker_settings: workerSettings,
      } : {}),
    });
  }

  return (
    <form className="runtime-settings" onSubmit={submit}>
      <div className="task-manager-head">
        <div>
          <h3>运行配置</h3>
          <p className="task-flow-copy">按项目控制后台自动化范围；手动运行不受影响。</p>
        </div>
        <div className="settings-section-actions">
          <AutomationModeBadge project={{ automation_mode: automationMode }} />
          <button className="button primary" type="submit" disabled={busy}>保存</button>
        </div>
      </div>
      <div className="runtime-grid">
        <section className="automation-mode-section" aria-label="项目自动化模式">
          <div className="automation-mode-grid" role="radiogroup" aria-label="自动化模式">
            {automationModeOptions.map((option) => (
              <label className={`automation-mode-card mode-${option.value} ${automationMode === option.value ? "selected" : ""}`} key={option.value}>
                <input
                  checked={automationMode === option.value}
                  disabled={busy}
                  name="automation_mode"
                  onChange={() => setAutomationMode(option.value)}
                  type="radio"
                  value={option.value}
                />
                <span className="automation-mode-card-head">
                  <strong>{option.label}</strong>
                  <span>{option.risk}</span>
                </span>
                <small>{option.description}</small>
                <em>{option.loops}</em>
              </label>
            ))}
          </div>
        </section>
        <label className="select-row">
          <span>
            <strong>定时频率</strong>
            <small>{automationMode !== "off" ? "当前项目进入候选队列后，后台自动调度会按这个频率判断是否到期。" : "当前自动化关闭；可先保存频率，开启后再生效。"}</small>
          </span>
          <select value={scheduleFrequency} onChange={(event) => setScheduleFrequency(event.target.value)} disabled={busy}>
            <option value="half_hourly">每半小时</option>
            <option value="hourly">每小时</option>
            <option value="daily">每天</option>
            <option value="weekly">每周</option>
          </select>
        </label>
        <label className="check-row">
          <input
            checked={autoCommit}
            disabled={busy}
            onChange={(event) => setAutoCommit(event.target.checked)}
            type="checkbox"
          />
          <span>
            <strong>允许自动 commit</strong>
            <small>开启后 worker 会遵循项目提交约束，在验证通过后提交。</small>
          </span>
        </label>
        <label className="select-row worker-provider-row">
          <span>
            <strong>Worker Provider</strong>
            <small>{workerRuntime.selectable ? "选择后续运行使用的执行引擎。" : "演示项目的 Worker 由项目固定。"}</small>
          </span>
          <select value={workerProvider} onChange={(event) => setWorkerProvider(event.target.value)} disabled={busy || !workerRuntime.selectable}>
            {providerOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <section className="worker-network-panel" aria-label="Worker 网络代理">
          <div className="worker-network-head">
            <div>
              <strong>Worker 网络代理</strong>
              <small>同时向子进程注入大写和小写的 HTTP_PROXY、HTTPS_PROXY、ALL_PROXY；不会修改系统或 shell 配置。</small>
            </div>
            <span>{workerNetwork.mode === "custom" ? "配置代理" : workerNetwork.mode === "direct" ? "直连" : "继承环境"}</span>
          </div>
          <div className="worker-network-grid">
            {(networkProfile.controls || []).map((control) => (
              <label className="select-row" key={`network-${control.key}`}>
                <span>
                  <strong>{control.label}</strong>
                  <small>{control.description}</small>
                </span>
                {control.type === "select" ? (
                  <select
                    value={workerNetwork[control.key] || control.options?.[0]?.value || ""}
                    onChange={(event) => updateWorkerNetwork(control.key, event.target.value)}
                    disabled={busy}
                  >
                    {(control.options || []).map((option) => (
                      <option key={option.value} value={option.value}>{option.label}</option>
                    ))}
                  </select>
                ) : (
                  <input
                    autoCapitalize="none"
                    autoComplete="off"
                    disabled={busy || workerNetwork.mode !== "custom"}
                    onChange={(event) => updateWorkerNetwork(control.key, event.target.value)}
                    spellCheck="false"
                    type="text"
                    value={workerNetwork[control.key] || ""}
                  />
                )}
              </label>
            ))}
          </div>
          <p>健康检测使用已保存的代理配置；修改后请先保存，再重新检测。</p>
        </section>
        <section className="worker-health-panel" aria-label="Worker 环境检测">
          <div className="worker-health-head">
            <div>
              <strong>Worker 环境检测</strong>
              <small>检查 CLI、登录和可用的服务诊断，不调用模型或消耗额度；额度仍以真实运行结果为准。</small>
            </div>
            <button className="button" type="button" onClick={refreshWorkerHealth} disabled={workerHealthLoading}>
              {workerHealthLoading ? "检测中…" : "重新检测"}
            </button>
          </div>
          {workerHealthError ? <p className="worker-health-error">{workerHealthError}；不影响保存运行配置。</p> : null}
          <div className="worker-health-list">
            {["codex", "claude"].map((provider) => {
              const health = workerHealth?.providers?.[provider];
              const providerLabel = workerRuntime.providers?.[provider]?.label || (provider === "codex" ? "Codex" : "Claude");
              return (
                <article className="worker-health-row" key={provider}>
                  <div className="worker-health-summary">
                    <strong>{providerLabel}</strong>
                    {health ? (
                      <span className={`worker-health-status status-${statusClass(health.status)}`}>
                        {workerHealthStatusLabel(health.status)}
                      </span>
                    ) : (
                      <span className="worker-health-status status-pending">{workerHealthLoading ? "检测中" : "待检测"}</span>
                    )}
                  </div>
                  {health?.version ? <small>{health.version}</small> : null}
                  {health?.binary_path ? <code>{health.binary_path}</code> : null}
                  {health?.issue ? <p>{health.issue}</p> : null}
                  {health?.required_action ? <p className="worker-health-action">{health.required_action}</p> : null}
                </article>
              );
            })}
          </div>
        </section>
        {(workerProfile.controls || []).map((control) => (
          <label className={`select-row worker-control ${control.risk === "high" ? "high-risk" : ""}`} key={`${workerProvider}-${control.key}`}>
            <span>
              <strong>{control.label}</strong>
              <small>{control.description}</small>
            </span>
            <select
              value={workerSettings[control.key] || control.options?.[0]?.value || ""}
              onChange={(event) => updateWorkerSetting(control.key, event.target.value)}
              disabled={busy}
            >
              {(control.options || []).map((option) => (
                <option key={option.value} value={option.value}>{option.label}</option>
              ))}
            </select>
          </label>
        ))}
        <p className="runtime-activation-note">
          {runIsActive
            ? "当前运行不受影响；保存后，下次继续运行会使用新 Worker 配置，并沿用已有任务与工作区状态。"
            : "保存后，下一轮运行会使用所选 Worker 配置，并沿用已有任务与工作区状态。"}
        </p>
      </div>
    </form>
  );
}

function workerHealthStatusLabel(status) {
  return {
    ready: "CLI 就绪",
    missing: "未安装",
    unauthenticated: "未登录",
    error: "检测异常",
  }[status] || "未知";
}

function workerRuntimeSettings(workerRuntime) {
  const entries = Object.entries(workerRuntime?.providers || {}).map(([provider, profile]) => (
    [provider, { ...(profile?.settings || {}) }]
  ));
  if (workerRuntime?.provider && !entries.some(([provider]) => provider === workerRuntime.provider)) {
    entries.push([workerRuntime.provider, { ...(workerRuntime.settings || {}) }]);
  }
  return Object.fromEntries(entries);
}

function RunActions({ project, currentTask, latestRunStatus, hasQueuedTasks, busy, runAction, navigateToEntry, setActiveTab }) {
  const [previewOpen, setPreviewOpen] = useState(false);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [previewResult, setPreviewResult] = useState(null);
  const moreRef = useRef(null);
  const projectId = project.project_id;
  const model = projectRunActionModel({
    projectStatus: project.project_status,
    task: currentTask,
    latestRunStatus,
    hasQueuedTasks,
  });

  function closeMoreMenu() {
    moreRef.current?.removeAttribute("open");
  }

  function handlePrimaryAction() {
    const action = model.primaryAction;
    if (!action) return;
    if (action.kind === "start") {
      runAction(projectId, "start-run");
      return;
    }
    if (action.kind === "resume") {
      runAction(projectId, "resume-once");
      return;
    }
    if (action.kind === "recovery") {
      navigateToEntry("overview", "recovery");
      return;
    }
    if (action.kind === "task") {
      navigateToEntry("tasks", action.taskId);
    }
  }

  async function openRunPlan() {
    closeMoreMenu();
    setPreviewOpen(true);
    setPreviewLoading(true);
    setPreviewError("");
    setPreviewResult(null);
    const result = await runAction(projectId, "preview-run", {}, { quiet: true, refreshAfter: false });
    if (result?.status === "completed" && result.plan) {
      setPreviewResult(result);
    } else {
      setPreviewError(result?.summary || "运行计划读取失败，请稍后重试。");
    }
    setPreviewLoading(false);
  }

  return (
    <>
      <div className="run-control" aria-label="项目操作">
        {model.primaryAction ? (
          <button className="run-control-button execute" type="button" disabled={busy} onClick={handlePrimaryAction}>
            {model.primaryAction.label}
          </button>
        ) : null}
        {model.taskNavigation ? (
          <button className="run-control-button task-link" type="button" disabled={busy} onClick={() => navigateToEntry("tasks", model.taskNavigation.taskId)}>
            {model.taskNavigation.label}
          </button>
        ) : null}
        <details className="project-more-actions" ref={moreRef}>
          <summary className="run-control-button more">更多</summary>
          <div className="project-more-menu">
            <button type="button" disabled={busy} onClick={openRunPlan}>查看运行计划</button>
            <button
              type="button"
              disabled={busy || model.running}
              title={model.running ? "当前项目运行结束后才能巡检" : undefined}
              onClick={() => { closeMoreMenu(); runAction(projectId, "run-once", { loop_type: "scan" }); }}
            >
              巡检本项目
            </button>
            <button type="button" disabled={busy} onClick={() => { closeMoreMenu(); setActiveTab("settings"); }}>项目设置</button>
          </div>
        </details>
      </div>
      {previewOpen ? (
        <RunPlanDialog
          error={previewError}
          loading={previewLoading}
          onClose={() => !previewLoading && setPreviewOpen(false)}
          result={previewResult}
        />
      ) : null}
    </>
  );
}

function RunPlanDialog({ result, loading, error, onClose }) {
  const closeButtonRef = useRef(null);
  const plan = result?.plan || {};
  const task = result?.task || {};
  const targets = Array.isArray(task?.target_plan?.targets) ? task.target_plan.targets : [];
  const sections = [
    { id: "phases", label: "执行阶段", values: plan.phases },
    { id: "inputs", label: "预计读取", values: plan.inputs },
    { id: "writes", label: "预计写入", values: plan.writes },
    { id: "gate", label: "进入条件", values: plan.gate },
  ].filter((section) => Array.isArray(section.values) && section.values.length);
  useEffect(() => {
    function handleKey(event) {
      if (event.key === "Escape" && !loading) onClose();
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [loading, onClose]);
  useEffect(() => {
    if (!loading) closeButtonRef.current?.focus();
  }, [loading]);
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section
        aria-busy={loading}
        aria-describedby="run-plan-description"
        aria-labelledby="run-plan-title"
        aria-modal="true"
        className="settings-dialog run-plan-dialog"
        role="dialog"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="settings-dialog-head">
          <div>
            <h3 id="run-plan-title">运行计划</h3>
            <p className="task-flow-copy" id="run-plan-description">仅查看计划，不会调用执行器或修改任务。</p>
          </div>
          <button ref={closeButtonRef} className="mini-button" type="button" disabled={loading} onClick={onClose}>关闭</button>
        </div>
        <div className="run-plan-body">
          {loading ? <div className="compact-empty">正在计算本次运行会处理什么…</div> : null}
          {error ? <div className="notice error" role="alert">{error}</div> : null}
          {result ? (
            <>
              <div className="run-plan-summary">
                <div><span>任务</span><strong>{task.title || task.id || "当前队列中的下一项"}</strong></div>
                <div><span>运行类型</span><strong>{loopTypeLabel(plan.loop_type)}</strong></div>
                <div><span>执行器</span><strong>{plan.executor || "由项目配置决定"}</strong></div>
                {plan.next_state_if_ready ? <div><span>计划完成后</span><strong>{taskStateLabel(plan.next_state_if_ready)}</strong></div> : null}
              </div>
              {targets.length ? (
                <section className="run-plan-section">
                  <h4>预计影响范围</h4>
                  <div className="run-plan-targets">
                    {targets.map((target, index) => (
                      <article key={target.id || `${target.repo || "target"}-${index}`}>
                        <strong>{target.id || target.repo || `目标 ${index + 1}`}</strong>
                        <code>{target.repo_path || target.repo || "未提供仓库路径"}</code>
                        <span>{target.branch || "当前分支"} → {target.target_branch || "目标分支未登记"}</span>
                        <p>{target.scope || "执行器将在该目标内工作；具体文件改动需在运行后核对。"}</p>
                      </article>
                    ))}
                  </div>
                </section>
              ) : null}
              {sections.map((section) => (
                <section className="run-plan-section" key={section.id}>
                  <h4>{section.label}</h4>
                  <ol>{section.values.map((value, index) => <li key={`${section.id}-${index}`}>{String(value)}</li>)}</ol>
                </section>
              ))}
              {plan.run_dir ? (
                <section className="run-plan-section">
                  <h4>运行记录位置</h4>
                  <code className="run-plan-path">{plan.run_dir}</code>
                </section>
              ) : null}
            </>
          ) : null}
        </div>
      </section>
    </div>
  );
}

function TaskManager({ project, taskPayload, busy, addTask, taskAction, focusedEntry, activeTaskId, onHandleBlocker }) {
  const createRef = useRef(null);
  useEffect(() => {
    if (focusedEntry?.tab !== "tasks" || focusedEntry.id !== "create") return;
    createRef.current?.scrollIntoView({ block: "center" });
    createRef.current?.querySelector("input")?.focus({ preventScroll: true });
  }, [focusedEntry]);
  const [manualTitle, setManualTitle] = useState("");
  const [manualDescription, setManualDescription] = useState("");
  const [prompt, setPrompt] = useState("");
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [historyPayload, setHistoryPayload] = useState(null);
  const [historyLoading, setHistoryLoading] = useState(false);
  const tasks = Array.isArray(taskPayload?.tasks) ? taskPayload.tasks : [];
  const groups = groupTasks(tasks);
  const supported = taskPayload && taskPayload.status !== "failed" && taskPayload.status !== "misconfigured";

  async function submitManual(event) {
    event.preventDefault();
    const ok = await addTask(project.project_id, "manual", { title: manualTitle, description: manualDescription });
    if (ok) {
      setManualTitle("");
      setManualDescription("");
    }
  }

  async function submitAi(event) {
    event.preventDefault();
    const ok = await addTask(project.project_id, "ai", { prompt });
    if (ok) {
      setPrompt("");
    }
  }

  async function toggleHistory() {
    const nextExpanded = !historyExpanded;
    setHistoryExpanded(nextExpanded);
    if (!nextExpanded || historyPayload) return;
    setHistoryLoading(true);
    try {
      setHistoryPayload(await api(`/api/v1/projects/${project.project_id}/tasks/history?limit=20`));
    } catch (error) {
      setHistoryPayload({ status: "failed", summary: error.message, tasks: [], issues: [] });
    } finally {
      setHistoryLoading(false);
    }
  }

  return (
    <section className="task-manager">
      <div className="task-manager-head">
        <div>
          <h3>任务管理</h3>
          <p className="task-flow-copy">按优先级查看正在进行与等待处理的任务。</p>
        </div>
        <StatusBadge value={supported ? "idle" : "failed"} label={supported ? `${tasks.length} 个待处理` : "未接入"} />
      </div>

      {supported ? (
        <>
          {tasks.length ? (
            <div className="task-group-list">
              {groups.map((group) => (
                <section className="task-group" key={group.id}>
                  <div className="task-group-head">
                    <h4>{group.label}</h4>
                    <span>{group.tasks.length}</span>
                  </div>
                  <div className="task-list">
                    {group.tasks.map((task) => (
                      <TaskItem
                        busy={busy}
                        focusedEntry={focusedEntry}
                        key={task.id}
                        activeTaskId={activeTaskId}
                        onHandleBlocker={onHandleBlocker}
                        onTaskAction={taskAction}
                        projectId={project.project_id}
                        task={task}
                      />
                    ))}
                  </div>
                </section>
              ))}
            </div>
          ) : <div className="compact-empty">当前没有待处理任务。完成项可在最近历史任务中查看。</div>}
          <section className="task-history-section">
            <button className="button secondary" type="button" aria-expanded={historyExpanded} onClick={toggleHistory}>
              {historyExpanded ? "收起最近历史任务" : "展开最近历史任务（最多 20 条）"}
            </button>
            {historyExpanded ? (
              <div className="task-history-list">
                {historyLoading ? <div className="compact-empty">历史任务加载中。</div> : null}
                {!historyLoading && historyPayload?.issues?.length ? (
                  <div className="notice error">有 {historyPayload.issues.length} 个历史任务文件无法读取，请查看项目诊断。</div>
                ) : null}
                {!historyLoading && historyPayload?.tasks?.length ? historyPayload.tasks.map((task) => (
                  <TaskItem busy key={task.id} projectId={project.project_id} readOnly task={task} />
                )) : null}
                {!historyLoading && historyPayload && !historyPayload.tasks?.length ? <div className="compact-empty">暂无历史任务。</div> : null}
              </div>
            ) : null}
          </section>
          <div className="task-create-grid">
            <form className="task-create" onSubmit={submitManual} ref={createRef}>
              <h4>添加新任务</h4>
              <label>
                <span>标题</span>
                <input value={manualTitle} onChange={(event) => setManualTitle(event.target.value)} placeholder="例如：补充失败重试策略" disabled={busy} />
              </label>
              <label>
                <span>说明</span>
                <textarea value={manualDescription} onChange={(event) => setManualDescription(event.target.value)} placeholder="补充背景、边界或验收重点" disabled={busy} />
              </label>
              <button className="button primary" type="submit" disabled={busy || !manualTitle.trim()}>添加任务</button>
            </form>

            <form className="task-create" onSubmit={submitAi}>
              <h4>一句话生成</h4>
              <label>
                <span>需求</span>
                <textarea value={prompt} onChange={(event) => setPrompt(event.target.value)} placeholder="例如：让任务失败后自动生成复盘事件" disabled={busy} />
              </label>
              <button className="button secondary" type="submit" disabled={busy || !prompt.trim()}>AI 创建任务</button>
            </form>
          </div>
        </>
      ) : (
        <div className="compact-empty">{taskPayload ? taskPayload.summary || "当前 runner 未暴露任务管理。" : "任务列表加载中。"}</div>
      )}
    </section>
  );
}

function TaskItem({ task, busy, onTaskAction, projectId, readOnly = false, focusedEntry, activeTaskId, onHandleBlocker }) {
  const taskRef = useRef(null);
  const detailsRef = useRef(null);
  useEffect(() => {
    if (focusedEntry?.tab !== "tasks" || focusedEntry.id !== task.id) return;
    detailsRef.current.open = true;
    detailsRef.current.querySelector("summary")?.focus({ preventScroll: true });
    taskRef.current?.scrollIntoView({ block: "start" });
  }, [focusedEntry, task.id]);
  const docsBadges = taskDocsBadges(task);
  const blocker = currentBlockerReasonFrom(task);
  const canHandleCurrentBlocker = !readOnly && task.id === activeTaskId && isBlockedTask(task);
  return (
    <article className="task-item" ref={taskRef}>
      <div className="task-summary-row">
        <div className="task-main">
          <h4>{task.title || task.id}</h4>
          <p className="project-meta">{task.id} · {taskTargetOrder(task) || "单项目"} · {task.priority || "P2"}</p>
        </div>
        <div className="task-order-actions">
          <StatusBadge value={task.state} label={taskStateLabel(task.state, task.state_label)} />
        </div>
      </div>
      <p className="task-summary-copy">{blocker || task.next_action || task.description || "等待下一步处理。"}</p>
      <div className="task-summary-meta">
        <span>更新 {formatTime(task.updated_at || task.created_at)}</span>
        {docsBadges.slice(0, 2).map((badge) => <span className={`pill ${badge.tone}`} key={badge.label}>{badge.label}</span>)}
      </div>
      {!readOnly ? <div className="task-visible-actions">
        {canHandleCurrentBlocker ? <button className="button primary" type="button" onClick={onHandleBlocker}>处理当前阻塞</button> : null}
        <TaskActionButtons busy={busy} onTaskAction={onTaskAction} projectId={projectId} task={task} />
      </div> : null}
      <details className="task-expanded" ref={detailsRef}><summary>查看任务详情</summary>
        <div className="task-details">
          <DetailRow label="优先级" value={task.priority || "P2"} />
          <DetailRow label="Targets" value={taskTargetOrder(task) || "单项目"} />
          <DetailRow label="最近运行" value={task.agent_run_id || "无"} />
          <DetailRow label="来源" value={sourceLabel(task.source)} />
          <DetailRow label="创建" value={formatTime(task.created_at)} />
          <DetailRow label="更新" value={formatTime(task.updated_at)} />
          <DetailRow label="说明" value={task.description || "未填写说明"} />
        </div>
        <div className="task-doc-badges">
          {docsBadges.map((badge) => <span className={`pill ${badge.tone}`} key={badge.label}>{badge.label}</span>)}
        </div>
        <TaskDocumentReferences task={task} />
        <TaskAcceptanceCriteria task={task} />
        {task.sync_blocked ? (
          <BlockerNotice
            title="Git 同步阻塞"
            reason={`本地 ${task.sync_blocked.local_revision || "未知"} 与远端 ${task.sync_blocked.remote_revision || "未知"} 需要人工处理。`}
            compact
          />
        ) : null}
        {blocker ? <BlockerNotice title="阻塞原因" reason={blocker} compact /> : null}
      </details>
    </article>
  );
}

function taskAcceptanceCriteria(task) {
  return Array.isArray(task?.acceptance)
    ? task.acceptance
    : Array.isArray(task?.acceptance_criteria)
      ? task.acceptance_criteria
      : [];
}

function TaskAcceptanceCriteria({ task }) {
  const criteria = taskAcceptanceCriteria(task);
  if (!criteria.length) return null;
  return (
    <section className="task-criteria" aria-label="任务验收项">
      <h5>验收项</h5>
      <ol>
        {criteria.map((criterion, index) => <li key={`${index}-${criterion}`}>{criterion}</li>)}
      </ol>
    </section>
  );
}

function DetailRow({ label, value }) {
  return (
    <div className="detail-row">
      <span>{label}</span>
      <strong>{value || "无"}</strong>
    </div>
  );
}

function taskTargetId(target, index = 0) {
  if (typeof target === "string") return target;
  return target?.id || target?.project || target?.project_id || target?.target || target?.key || `target-${index + 1}`;
}

function taskTargetRows(task) {
  return buildTargetReviewRows(task, { documentPaths: taskDocumentPaths });
}

function taskTargetOrder(task) {
  const ordered = Array.isArray(task?.target_plan?.ordered_target_ids) ? task.target_plan.ordered_target_ids : [];
  if (ordered.length) return ordered.join(" 到 ");
  const rawTargets = Array.isArray(task?.targets) ? task.targets : [];
  if (rawTargets.length) return rawTargets.map((target, index) => taskTargetId(target, index)).join(" 到 ");
  return "";
}

function TaskTargetsTable({ task }) {
  const rows = taskTargetRows(task);
  if (!rows.length) return null;
  const reviewMode = ["ready_for_review", "accepted"].includes(taskStatus(task));
  const validationLabel = (status) => {
    if (status === "passed") return "验证通过";
    if (status === "failed") return "验证失败";
    return "尚未验证";
  };
  return (
    <section className={`target-review-section ${reviewMode ? "review-mode" : ""}`} aria-label={reviewMode ? "本次验收对象" : "Target 详情"}>
      <div className="target-review-heading">
        <div>
          <h4>{reviewMode ? "本次验收对象" : "Target 详情"}</h4>
          <p>{reviewMode
            ? "请按子仓核对功能分支和 Worktree（本次代码目录）；代码提交仅用于确认验收期间代码没有变化。"
            : "查看每个子仓的代码位置、执行结果和验证情况。"}</p>
        </div>
        <span>{rows.length} 个子仓</span>
      </div>
      <div className="target-detail-list">
        {rows.map((target, index) => (
          <article className="target-detail-item" key={target.id}>
            <div className="target-detail-main">
              <div>
                <strong>{String(index + 1).padStart(2, "0")} · {target.id}</strong>
                <span>{target.project}</span>
              </div>
              <StatusBadge value={target.validationStatus} label={validationLabel(target.validationStatus)} />
            </div>
            <div className="target-detail-grid target-review-grid">
              <DetailRow label="子仓目录" value={target.repo} />
              <DetailRow label="功能分支" value={target.branch} />
              <DetailRow label="Worktree（验收目录）" value={target.worktree} />
              <DetailRow
                label="代码提交（仅用于核对）"
                value={<code title={target.codeRevision}>{target.codeRevisionShort}</code>}
              />
              <DetailRow label="合入目标" value={target.targetBranch} />
              <DetailRow label="验证结果" value={target.validationSummary} />
              <DetailRow label="实现结果" value={target.summary} />
              <DetailRow
                label="文档版本（非代码提交）"
                value={<code title={target.docRevision}>{target.docRevision === "未记录" ? target.docRevision : target.docRevision.slice(0, 8)}</code>}
              />
            </div>
            {target.validationCommands.length ? (
              <details className="target-validation-details">
                <summary>查看验证命令（{target.validationCommands.length}）</summary>
                <ul>
                  {target.validationCommands.map((command) => <li key={command}><code>{command}</code></li>)}
                </ul>
              </details>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

function TaskDocumentReferences({ task }) {
  const documentSets = taskDocumentSets(task);
  if (!documentSets.length) return null;
  const documentCount = documentSets.reduce((total, { docs }) => total + taskDocumentPaths(docs).length, 0);
  return (
    <details className="task-document-references">
      <summary>
        <span className="task-document-summary-main">
          <strong>关联文档</strong>
          <small>{documentSets.length} 个范围 · {documentCount} 份文档</small>
        </span>
        <span className="task-document-summary-hint">按目标查看</span>
      </summary>
      <div className="task-document-targets">
        {documentSets.map(({ target, docs }) => {
          const groups = taskDocumentGroups(docs);
          const targetDocumentCount = groups.reduce((total, group) => total + group.documentCount, 0);
          const revision = String(docs.revision || "").trim();
          return (
            <details className="task-document-target" key={`${target}-${revision || "worktree"}`}>
              <summary>
                <span>
                  <strong>{target}</strong>
                  <small>{groups.length} 个阶段 · {targetDocumentCount} 份</small>
                </span>
                <code title={revision || "未记录版本"}>{revision ? revision.slice(0, 8) : "无版本"}</code>
              </summary>
              <div className="task-document-phases">
                {groups.map((group) => (
                  <section className="task-document-phase" key={`${target}-${group.id}`}>
                    <div className="task-document-phase-head">
                      <strong>{group.label}</strong>
                      <span>{group.documentCount}</span>
                    </div>
                    <div className="task-document-kinds">
                      {group.kinds.map((kind) => (
                        <div className="task-document-kind" key={`${group.id}-${kind.id}`}>
                          <span>{documentKindLabels[kind.id] || kind.id}</span>
                          <div>
                            {kind.paths.map((path) => <code className="document-path" key={path}>{path}</code>)}
                          </div>
                        </div>
                      ))}
                    </div>
                  </section>
                ))}
              </div>
            </details>
          );
        })}
      </div>
    </details>
  );
}

function RunTaskMeta({ run }) {
  const taskTitle = run.task_title || run.active_task?.title || run.next_task?.title;
  const taskId = run.task_id || run.active_task?.id || "";
  const state = run.task_state || run.next_state || run.active_task?.state;
  if (!taskTitle && !taskId && !state && !run.trigger) {
    return null;
  }
  return (
    <div className="run-task-meta" aria-label="运行任务信息">
      <DetailRow label="任务" value={taskTitle || "未返回任务标题"} />
      {taskId ? <DetailRow label="任务 ID" value={taskId} /> : null}
      {state ? <DetailRow label="任务状态" value={taskStateLabel(state)} /> : null}
      <DetailRow label="触发方式" value={triggerLabel(run.trigger)} />
    </div>
  );
}

function RunNotificationMeta({ run }) {
  const notification = run.notification || {};
  if (!notification.status) {
    return null;
  }
  const detail = notification.error || notification.reason || notification.response || "";
  return (
    <div className="run-task-meta" aria-label="运行通知信息">
      <DetailRow label="通知状态" value={notificationStatusLabel(notification.status)} />
      <DetailRow label="通知渠道" value={notification.channel || "无"} />
      {detail ? <DetailRow label="通知说明" value={detail} /> : null}
    </div>
  );
}

function BlockerNotice({ title, reason, compact = false }) {
  if (!reason) return null;
  return (
    <div className={`blocker-notice ${compact ? "compact" : ""}`}>
      <strong>{title}</strong>
      <p>{reason}</p>
    </div>
  );
}

function DiagnosticCard({ diagnostic, compact = false }) {
  const rows = diagnosticRows(diagnostic);
  if (!rows.length) return null;
  const reason = diagnostic.reason || diagnostic.summary || "";
  const nextAction = diagnostic.next_action || "";
  return (
    <section className={`diagnostic-card ${compact ? "compact" : ""}`} aria-label="运行诊断">
      <div className="diagnostic-head">
        <strong>运行诊断</strong>
        {diagnostic.can_resume ? <span>可继续</span> : null}
      </div>
      {reason ? <p className="diagnostic-copy">{reason}</p> : null}
      <div className="diagnostic-grid">
        {rows.map(([label, value]) => (
          <DetailRow key={label} label={label} value={value} />
        ))}
      </div>
      {nextAction ? <p className="diagnostic-action">建议：{nextAction}</p> : null}
    </section>
  );
}

function BlockerResolveForm({ projectId, busy, onResolve }) {
  const [reason, setReason] = useState("");

  async function submit(event) {
    event.preventDefault();
    const ok = await onResolve(projectId, reason.trim());
    if (ok) {
      setReason("");
    }
  }

  return (
    <form className="blocker-resolve" onSubmit={submit}>
      <label>
        <span>处理说明</span>
        <textarea
          disabled={busy}
          onChange={(event) => setReason(event.target.value)}
          placeholder="例如：已确认验收口径，允许跨租户吊销 token，并补充到 requirements。"
          value={reason}
        />
      </label>
      <button className="button primary" type="submit" disabled={busy || !reason.trim()}>
        处理后恢复运行
      </button>
    </form>
  );
}

function RunInstructionForm({ projectId, busy, onResume }) {
  const [reason, setReason] = useState("");

  async function submit(event) {
    event.preventDefault();
    const ok = await onResume(projectId, "resume-once", { reason: reason.trim() });
    if (ok) {
      setReason("");
    }
  }

  return (
    <form className="blocker-resolve" onSubmit={submit}>
      <label>
        <span>继续说明</span>
        <textarea
          disabled={busy}
          onChange={(event) => setReason(event.target.value)}
          placeholder="粘贴本轮需要特别遵循的说明；该内容只对下一轮运行有效。"
          value={reason}
        />
      </label>
      <button className="button primary" type="submit" disabled={busy || !reason.trim()}>
        按说明继续运行
      </button>
    </form>
  );
}

function notificationStatusLabel(status) {
  return {
    sent: "已发送",
    skipped: "已跳过",
    failed: "发送失败",
  }[status] || status || "未知";
}

function TaskFlowPanel({ activeTask, busy, onTaskAction, projectId }) {
  const timeline = Array.isArray(activeTask.timeline) ? activeTask.timeline : [];
  const blockerReason = currentBlockerReasonFrom(activeTask);
  if (!activeTask.id && !timeline.length) {
    return (
      <section className="task-flow">
        <div>
          <h3>任务流</h3>
          <p className="task-flow-copy">当前项目没有暴露任务状态机。接入项目 runner 后会显示完整轨迹。</p>
        </div>
      </section>
    );
  }
  return (
    <section className="task-flow">
      <div className="task-flow-head">
        <div>
          <h3>{activeTask.title || activeTask.id || "当前任务"}</h3>
          <p className="task-flow-copy">下一步：{activeTask.next_action || "等待下一轮运行"}</p>
        </div>
        <div className="task-flow-badges">
          <StatusBadge value={activeTask.state} label={taskStateLabel(activeTask.state, activeTask.state_label)} />
        </div>
      </div>
      {timeline.length ? (
        <ol className="timeline" aria-label="任务状态机">
          {timeline.map((step) => (
            <li className={`timeline-step ${step.phase || "waiting"}`} key={step.state}>
              <span className="timeline-index">{step.order}</span>
              <span className="timeline-label">{taskStateLabel(step.state, step.label)}</span>
              <span className="timeline-action">{step.action}</span>
            </li>
          ))}
        </ol>
      ) : null}
      {blockerReason ? <BlockerNotice title="任务流阻塞原因" reason={blockerReason} compact /> : null}
      <TaskActionSurface activeTask={activeTask} busy={busy} onTaskAction={onTaskAction} projectId={projectId} />
    </section>
  );
}

function TaskActionSurface({ activeTask, busy, onTaskAction, projectId }) {
  const status = taskStatus(activeTask);
  const cleanupError = cleanupFailureMessage(activeTask);
  if (!["ready_for_review", "accepted", "merged"].includes(status)) {
    return null;
  }
  return (
    <div className="task-action-surface">
      <div className="task-action-head">
        <div>
          <strong>{status === "ready_for_review" ? "验收" : status === "accepted" ? "本地合入" : "清理"}</strong>
          <p>{cleanupError || activeTask.last_run_summary || activeTask.merge_result?.summary || activeTask.next_action}</p>
        </div>
        <TaskActionButtons busy={busy} onTaskAction={onTaskAction} projectId={projectId} task={activeTask} />
      </div>
      {cleanupError ? <BlockerNotice title="清理失败" reason={cleanupError} compact /> : null}
      <div className="task-details">
        <DetailRow label="targets 顺序" value={taskTargetOrder(activeTask) || "单项目"} />
        <DetailRow label="target_branch" value={activeTask.target_branch || activeTask.merge_result?.target_branch || "main"} />
        <DetailRow label="merge_method" value={activeTask.merge_method || activeTask.merge_result?.merge_method || "squash"} />
        <DetailRow label="branch" value={activeTask.branch || activeTask.feature_branch || activeTask.merge_result?.branch || "未记录"} />
        <DetailRow label="worktree" value={activeTask.worktree_path || activeTask.cleanup_result?.worktree_path || "未记录"} />
        <DetailRow label="验收时间" value={formatTime(activeTask.reviewed_at || activeTask.accepted_at)} />
        <DetailRow label="合入结果" value={activeTask.merge_result?.summary || "待执行"} />
      </div>
      <TaskTargetsTable task={activeTask} />
    </div>
  );
}

const reviewRejectOptions = {
  code: {
    decision: "reject_code",
    title: "驳回代码",
    description: "说明需要继续修改的代码问题。保存后会自动沿原任务继续开发。",
    placeholder: "例如：保存误报原因后线索仍然存在，请修复状态刷新并补充回归测试。",
  },
  spec: {
    decision: "reject_spec",
    title: "驳回需求",
    description: "说明需要调整的需求或规则。保存后会复用反馈解除阻塞并自动继续开发。",
    placeholder: "例如：当前验收口径缺少误报线索在后续巡检中的处理规则。",
  },
};

function ReviewFeedbackDialog({ busy, error, feedback, option, onCancel, onFeedbackChange, onSubmit }) {
  useEffect(() => {
    function handleKey(event) {
      if (event.key === "Escape" && !busy) onCancel();
    }
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [busy, onCancel]);

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onCancel}>
      <section
        aria-label="验收驳回反馈"
        aria-modal="true"
        className="settings-dialog review-feedback-dialog"
        onMouseDown={(event) => event.stopPropagation()}
        role="dialog"
      >
        <div className="settings-dialog-head">
          <div>
            <h3>{option.title}</h3>
            <p className="task-flow-copy">{option.description}</p>
          </div>
          <button className="mini-button" type="button" disabled={busy} onClick={onCancel}>关闭</button>
        </div>
        <form className="review-feedback-form" onSubmit={onSubmit}>
          <label>
            <span>反馈原因</span>
            <textarea
              autoFocus
              disabled={busy}
              onChange={(event) => onFeedbackChange(event.target.value)}
              placeholder={option.placeholder}
              required
              value={feedback}
            />
          </label>
          {error ? <div className="review-feedback-error" role="alert">{error}</div> : null}
          <div className="review-feedback-actions">
            <button className="button secondary" type="button" disabled={busy} onClick={onCancel}>取消</button>
            <button className="button danger" type="submit" disabled={busy || !feedback.trim()}>
              {busy ? "保存中…" : "确认驳回"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}

function TaskActionButtons({ projectId, task, busy, onTaskAction, compact = false }) {
  const [reviewOption, setReviewOption] = useState(null);
  const [reviewFeedback, setReviewFeedback] = useState("");
  const [reviewError, setReviewError] = useState("");
  if (!projectId || !task?.id || !onTaskAction) return null;
  const status = taskStatus(task);
  const actionBusy = busy;

  async function accept() {
    await onTaskAction(projectId, task.id, "review", { decision: "accept" });
  }

  function openReview(option) {
    setReviewFeedback("");
    setReviewError("");
    setReviewOption(option);
  }

  function closeReview() {
    if (actionBusy) return;
    setReviewOption(null);
    setReviewFeedback("");
    setReviewError("");
  }

  async function submitReview(event) {
    event.preventDefault();
    const feedback = reviewFeedback.trim();
    if (!reviewOption || !feedback || actionBusy) return;
    setReviewError("");
    const result = await onTaskAction(projectId, task.id, "review", {
      decision: reviewOption.decision,
      feedback,
    });
    if (result && (result.status !== "failed" || result.review_saved)) {
      setReviewOption(null);
      setReviewFeedback("");
      setReviewError("");
    } else {
      setReviewError(result?.summary || "驳回未保存，请重试。");
    }
  }

  async function merge() {
    if (!window.confirm("执行本地 squash merge？不会 push。")) return;
    await onTaskAction(projectId, task.id, "merge", {});
  }

  async function cleanup() {
    if (!window.confirm("清理本地 feature branch / worktree 并完成任务？")) return;
    const result = await onTaskAction(projectId, task.id, "cleanup", {});
    if (cleanupRequiresDiscard(result) && window.confirm("worktree 有未提交变化。确认永久丢弃这些变化并继续清理？")) {
      await onTaskAction(projectId, task.id, "cleanup", { discard_changes: true });
    }
  }

  async function abandon() {
    const pending = task.cleanup_pending === "abandon";
    const reason = pending ? task.abandoned_reason || "用户放弃该任务。" : window.prompt("放弃原因");
    if (!reason || !String(reason).trim()) return;
    if (!pending && !window.confirm("放弃任务会清理本地 feature branch / worktree；远端 branch 会保留。继续？")) return;
    const result = await onTaskAction(projectId, task.id, "abandon", { reason: String(reason).trim() });
    if (cleanupRequiresDiscard(result) && window.confirm("worktree 有未提交变化。确认永久丢弃这些变化并放弃任务？")) {
      await onTaskAction(projectId, task.id, "abandon", { reason: String(reason).trim(), discard_changes: true });
    }
  }

  const abandonButton = (
    <button aria-label={task.cleanup_pending === "abandon" ? "重试放弃清理" : `放弃任务：${task.title || task.id}`} className="button danger" type="button" disabled={actionBusy} onClick={abandon}>
      {task.cleanup_pending === "abandon" ? "重试放弃清理" : "放弃"}
    </button>
  );
  const moreActions = (
    <details className="task-more-actions">
      <summary>更多操作</summary>
      <div className="task-more-panel">{abandonButton}</div>
    </details>
  );

  if (task.cleanup_pending === "abandon") {
    return <div className={`task-action-buttons ${compact ? "compact" : ""}`}>{abandonButton}</div>;
  }

  if (status === "ready_for_review") {
    return (
      <>
        <div className={`task-action-buttons ${compact ? "compact" : ""}`}>
          <button className="button primary" type="button" disabled={actionBusy} onClick={accept}>验收通过</button>
          <button className="button secondary" type="button" disabled={actionBusy} onClick={() => openReview(reviewRejectOptions.code)}>驳回代码</button>
          <button className="button danger" type="button" disabled={actionBusy} onClick={() => openReview(reviewRejectOptions.spec)}>驳回需求</button>
          {moreActions}
        </div>
        {reviewOption ? (
          <ReviewFeedbackDialog
            busy={actionBusy}
            error={reviewError}
            feedback={reviewFeedback}
            onCancel={closeReview}
            onFeedbackChange={setReviewFeedback}
            onSubmit={submitReview}
            option={reviewOption}
          />
        ) : null}
      </>
    );
  }
  if (status === "accepted") {
    return (
      <div className={`task-action-buttons ${compact ? "compact" : ""}`}>
        <button className="button primary" type="button" disabled={actionBusy} onClick={merge}>合入</button>
        {moreActions}
      </div>
    );
  }
  if (status === "merged") {
    return (
      <div className={`task-action-buttons ${compact ? "compact" : ""}`}>
        <button className="button primary" type="button" disabled={busy} onClick={cleanup}>{busy ? "清理中…" : "清理"}</button>
        {moreActions}
      </div>
    );
  }
  return <div className={`task-action-buttons ${compact ? "compact" : ""}`}>{moreActions}</div>;
}

function cleanupRequiresDiscard(result) {
  if (!result || result.status !== "failed") return false;
  return JSON.stringify(result.cleanup_result || {}).includes("dirty_worktree_cleanup_requires_discard");
}

function KV({ label, value }) {
  return (
    <div className="kv">
      <div className="kv-label">{label}</div>
      <div className="kv-value">{value}</div>
    </div>
  );
}

function HistorySection({ title, emptyTitle, emptyBody, children }) {
  const hasChildren = React.Children.count(children) > 0;
  return (
    <div className="history-section">
      <h3>{title}</h3>
      <div className="history-list">
        {hasChildren ? children : <div className="empty-state"><h2>{emptyTitle}</h2><p>{emptyBody}</p></div>}
      </div>
    </div>
  );
}

const appRoot = import.meta.hot?.data.root || createRoot(document.getElementById("root"));
if (import.meta.hot) import.meta.hot.data.root = appRoot;
appRoot.render(<App />);
