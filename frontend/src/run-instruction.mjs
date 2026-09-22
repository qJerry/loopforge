export function canResumeWithInstruction(task, project) {
  const taskStatus = String(task?.status || task?.state || "");
  const projectStatus = String(project?.project_status || "");
  return Boolean(task?.id) && taskStatus === "coding" && ["idle", "timeout"].includes(projectStatus);
}

export function resumeWithInstruction({ request, projectId, reason }) {
  return request(`/api/v1/projects/${encodeURIComponent(projectId)}/resume-once`, {
    method: "POST",
    body: JSON.stringify({ reason: String(reason || "").trim() }),
  });
}
