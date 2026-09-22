export function cleanupFailureMessage(task) {
  if (task?.cleanup_result?.status !== "failed") return "";
  return String(task.cleanup_result.summary || task.blocker_reason || "清理失败，请重试。").trim();
}
