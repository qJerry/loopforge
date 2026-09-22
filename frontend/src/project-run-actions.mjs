const blockedStates = new Set(["spec_blocked", "prd_blocked", "dev_blocked", "blocked"]);
const reviewStates = new Set(["ready_for_review", "accepted", "merged"]);
const notYetStartedStates = new Set(["open", "claimed"]);
const developmentStates = new Set(["spec_ready", "prd_ready", "coding"]);
const terminalStates = new Set(["completed", "abandoned"]);
const timeoutRunStates = new Set(["timeout_continue", "timeout_blocked", "cancelled", "failed"]);

function taskState(task) {
  return task?.status || task?.state || "";
}

export function projectRunActionModel({
  projectStatus,
  task,
  latestRunStatus,
  hasQueuedTasks = false,
} = {}) {
  const state = taskState(task);
  const hasTask = Boolean(task?.id);
  const running = projectStatus === "running" || task?.agent_status === "running";
  const taskNavigation = hasTask ? { label: "查看当前任务", kind: "task", taskId: task.id } : null;

  if (running) {
    return { primaryAction: null, taskNavigation, running: true };
  }
  if (blockedStates.has(state)) {
    return {
      primaryAction: { label: "处理阻塞", kind: "recovery" },
      taskNavigation: null,
      running: false,
    };
  }
  if (state === "ready_for_review") {
    return {
      primaryAction: { label: "查看验收", kind: "task", taskId: task.id },
      taskNavigation: null,
      running: false,
    };
  }
  if (state === "accepted") {
    return {
      primaryAction: { label: "查看合入", kind: "task", taskId: task.id },
      taskNavigation: null,
      running: false,
    };
  }
  if (state === "merged") {
    return {
      primaryAction: { label: "完成收尾", kind: "task", taskId: task.id },
      taskNavigation: null,
      running: false,
    };
  }
  if (
    hasTask
    && (projectStatus === "timeout" || task?.agent_status === "timeout" || timeoutRunStates.has(latestRunStatus))
  ) {
    return {
      primaryAction: { label: "继续当前任务", kind: "resume" },
      taskNavigation,
      running: false,
    };
  }
  if (developmentStates.has(state)) {
    return {
      primaryAction: { label: state === "coding" ? "继续开发" : "开始开发", kind: "start" },
      taskNavigation,
      running: false,
    };
  }
  if (notYetStartedStates.has(state) || (!hasTask && hasQueuedTasks)) {
    return {
      primaryAction: { label: "开始下一个任务", kind: "start" },
      taskNavigation,
      running: false,
    };
  }
  if (reviewStates.has(state) || terminalStates.has(state) || ["misconfigured", "failed"].includes(projectStatus)) {
    return { primaryAction: null, taskNavigation, running: false };
  }
  return { primaryAction: null, taskNavigation, running: false };
}
