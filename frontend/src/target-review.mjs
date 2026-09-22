function targetId(target, index) {
  if (typeof target === "string") return target;
  return target?.id || target?.project || target?.project_id || target?.target || target?.key || `target-${index + 1}`;
}

function shortRevision(revision) {
  return typeof revision === "string" && revision ? revision.slice(0, 8) : "未记录";
}

export function buildTargetReviewRows(task, { documentPaths = () => [] } = {}) {
  const planTargets = Array.isArray(task?.target_plan?.targets) ? task.target_plan.targets : [];
  const rawTargets = Array.isArray(task?.targets) ? task.targets : [];
  const mergeTargets = Array.isArray(task?.merge_result?.targets) ? task.merge_result.targets : [];
  const cleanupTargets = Array.isArray(task?.cleanup_result?.targets) ? task.cleanup_result.targets : [];
  const resultTargets = cleanupTargets.length ? cleanupTargets : mergeTargets;
  const resultById = new Map(resultTargets.map((target, index) => [targetId(target, index), target]));
  const rawById = new Map(rawTargets.map((target, index) => [targetId(target, index), target]));
  const sourceTargets = planTargets.length ? planTargets : rawTargets;

  return sourceTargets.map((target, index) => {
    const id = targetId(target, index);
    const result = resultById.get(id) || {};
    const planned = typeof target === "string" ? { id, project: id } : target || {};
    const source = rawById.get(id);
    const rawSource = typeof source === "string" ? { id, project: id } : source || {};
    const raw = { ...planned, ...rawSource };
    const git = { ...(raw.git || {}), ...(result.git || {}) };
    const validation = result.validation || raw.validation || {};
    const paths = documentPaths(raw.docs);
    const codeRevision = result.branch_commit
      || result.commit
      || git.branch_revision
      || raw.branch_revision
      || "未记录";

    return {
      id,
      project: raw.project || raw.project_id || result.project || id,
      repo: result.repo_path || raw.repo_path || result.repo || raw.repo || "未记录",
      branch: result.branch || git.feature_branch || raw.branch || raw.feature_branch || "未记录",
      targetBranch: result.target_branch || raw.target_branch || task?.target_branch || "main",
      worktree: result.worktree_path || raw.worktree_path || raw.worktree || "未记录",
      codeRevision,
      codeRevisionShort: shortRevision(codeRevision),
      baseRevision: git.base_revision || "未记录",
      docRevision: raw.docs?.revision || "未记录",
      summary: result.summary
        || raw.execution_summary
        || validation.summary
        || raw.merge_result?.summary
        || raw.cleanup_result?.summary
        || "等待执行结果",
      validationStatus: validation.status || "not_run",
      validationSummary: validation.summary || "尚未记录验证结论",
      validationCommands: Array.isArray(validation.commands) ? validation.commands : [],
      after: Array.isArray(raw.after) ? raw.after.join(", ") : raw.after || "无",
      documents: paths.length ? paths.join("；") : "未关联",
    };
  });
}
