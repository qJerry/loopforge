function reviewPath(projectId, taskId) {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/review`;
}

function continuationRequest(projectId, decision, feedback) {
  const encodedProjectId = encodeURIComponent(projectId);
  if (decision === "reject_code") {
    return {
      path: `/api/v1/projects/${encodedProjectId}/resume-once`,
      options: { method: "POST", body: "{}" },
    };
  }
  if (decision === "reject_spec") {
    return {
      path: `/api/v1/projects/${encodedProjectId}/resolve-and-resume`,
      options: {
        method: "POST",
        body: JSON.stringify({ reason: feedback }),
      },
    };
  }
  return null;
}

export async function submitReviewWithContinuation({ request, projectId, taskId, decision, feedback = "" }) {
  const reviewResult = await request(reviewPath(projectId, taskId), {
    method: "POST",
    body: JSON.stringify({ decision, feedback }),
  });
  if (reviewResult?.status === "failed") return reviewResult;

  const continuation = continuationRequest(projectId, decision, feedback);
  if (!continuation) return reviewResult;

  try {
    const continuationResult = await request(continuation.path, continuation.options);
    if (continuationResult?.status === "failed") {
      return {
        ...continuationResult,
        status: "failed",
        summary: `驳回已保存，但自动续跑失败：${continuationResult.summary || "未知错误"}`,
        review_saved: true,
        review_result: reviewResult,
        continuation_result: continuationResult,
      };
    }
    return {
      ...continuationResult,
      summary: `${reviewResult?.summary || "驳回已保存"}；${continuationResult?.summary || "已自动续跑"}`,
      review_saved: true,
      review_result: reviewResult,
      continuation_result: continuationResult,
    };
  } catch (error) {
    return {
      status: "failed",
      summary: `驳回已保存，但自动续跑失败：${error?.message || "未知错误"}`,
      review_saved: true,
      review_result: reviewResult,
    };
  }
}
