import assert from "node:assert/strict";
import test from "node:test";

async function loadCleanupFailureMessage() {
  try {
    const module = await import("../src/cleanup-feedback.mjs");
    return module.cleanupFailureMessage;
  } catch (error) {
    assert.fail(`清理失败反馈模块尚未实现：${error.message}`);
  }
}

test("等待清理任务会持续显示最近一次清理失败原因", async () => {
  const cleanupFailureMessage = await loadCleanupFailureMessage();
  const message = cleanupFailureMessage({
    status: "merged",
    cleanup_result: {
      status: "failed",
      summary: "父账本 ownership marker 不匹配",
    },
  });

  assert.equal(message, "父账本 ownership marker 不匹配");
  assert.equal(cleanupFailureMessage({ state: "merged", cleanup_result: { status: "failed", summary: "清理失败" } }), "清理失败");
  assert.equal(cleanupFailureMessage({ status: "completed", cleanup_result: { status: "completed" } }), "");
});
