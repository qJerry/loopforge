import assert from "node:assert/strict";
import test from "node:test";

async function loadSubmitReviewWithContinuation() {
  try {
    const module = await import("../src/review-continuation.mjs");
    return module.submitReviewWithContinuation;
  } catch (error) {
    assert.fail(`验收驳回自动续跑模块尚未实现：${error.message}`);
  }
}

function requestRecorder(responses) {
  const calls = [];
  return {
    calls,
    request: async (path, options) => {
      calls.push({ path, options });
      const response = responses[calls.length - 1];
      if (response instanceof Error) throw response;
      return response;
    },
  };
}

test("驳回代码保存后自动继续原任务", async () => {
  const submitReviewWithContinuation = await loadSubmitReviewWithContinuation();
  const recorder = requestRecorder([
    { status: "completed", summary: "代码驳回已保存" },
    { status: "completed", summary: "开发执行完成" },
  ]);

  const result = await submitReviewWithContinuation({
    request: recorder.request,
    projectId: "ad",
    taskId: "task-1",
    decision: "reject_code",
    feedback: "统一按钮样式",
  });

  assert.deepEqual(recorder.calls, [
    {
      path: "/api/v1/projects/ad/tasks/task-1/review",
      options: {
        method: "POST",
        body: JSON.stringify({ decision: "reject_code", feedback: "统一按钮样式" }),
      },
    },
    {
      path: "/api/v1/projects/ad/resume-once",
      options: { method: "POST", body: "{}" },
    },
  ]);
  assert.equal(result.status, "completed");
  assert.equal(result.review_saved, true);
});

test("驳回需求保存后复用反馈解除阻塞并恢复运行", async () => {
  const submitReviewWithContinuation = await loadSubmitReviewWithContinuation();
  const recorder = requestRecorder([
    { status: "completed", summary: "需求驳回已保存" },
    { status: "completed", summary: "已恢复运行" },
  ]);

  await submitReviewWithContinuation({
    request: recorder.request,
    projectId: "ad group",
    taskId: "task/2",
    decision: "reject_spec",
    feedback: "补充账号管理能力",
  });

  assert.deepEqual(recorder.calls, [
    {
      path: "/api/v1/projects/ad%20group/tasks/task%2F2/review",
      options: {
        method: "POST",
        body: JSON.stringify({ decision: "reject_spec", feedback: "补充账号管理能力" }),
      },
    },
    {
      path: "/api/v1/projects/ad%20group/resolve-and-resume",
      options: {
        method: "POST",
        body: JSON.stringify({ reason: "补充账号管理能力" }),
      },
    },
  ]);
});

test("驳回未保存时不触发自动续跑", async () => {
  const submitReviewWithContinuation = await loadSubmitReviewWithContinuation();
  const recorder = requestRecorder([
    { status: "failed", summary: "非法状态跃迁" },
  ]);

  const result = await submitReviewWithContinuation({
    request: recorder.request,
    projectId: "ad",
    taskId: "task-3",
    decision: "reject_spec",
    feedback: "重新确认规则",
  });

  assert.equal(recorder.calls.length, 1);
  assert.equal(result.status, "failed");
  assert.equal(result.review_saved, undefined);
});

test("驳回已保存但续跑失败时返回可区分结果", async () => {
  const submitReviewWithContinuation = await loadSubmitReviewWithContinuation();
  const recorder = requestRecorder([
    { status: "completed", summary: "代码驳回已保存" },
    new Error("连接中断"),
  ]);

  const result = await submitReviewWithContinuation({
    request: recorder.request,
    projectId: "ad",
    taskId: "task-4",
    decision: "reject_code",
    feedback: "修复页面刷新",
  });

  assert.equal(result.status, "failed");
  assert.equal(result.review_saved, true);
  assert.equal(result.summary, "驳回已保存，但自动续跑失败：连接中断");
});
