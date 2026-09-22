import assert from "node:assert/strict";
import test from "node:test";

test("新旧规格与开发阻塞状态都展示人工恢复入口", async () => {
  let isBlockedTask;
  try {
    ({ isBlockedTask } = await import("../src/task-state.mjs"));
  } catch (error) {
    assert.fail(`任务状态判断模块尚未实现：${error.message}`);
  }

  for (const state of ["spec_blocked", "prd_blocked", "dev_blocked", "blocked"]) {
    assert.equal(isBlockedTask({ state }), true, `${state} 应展示人工恢复入口`);
  }
  assert.equal(isBlockedTask({ state: "ready_for_review" }), false);
  assert.equal(isBlockedTask(null), false);
});
