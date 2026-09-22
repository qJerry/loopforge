import assert from "node:assert/strict";
import test from "node:test";

async function loadRunInstructionModule() {
  try {
    return await import("../src/run-instruction.mjs");
  } catch (error) {
    assert.fail(`实现中任务继续入口尚未实现：${error.message}`);
  }
}

test("实现中且未运行的任务显示继续说明入口", async () => {
  const { canResumeWithInstruction } = await loadRunInstructionModule();

  assert.equal(canResumeWithInstruction({ status: "coding", id: "task-1" }, { project_status: "idle" }), true);
  assert.equal(canResumeWithInstruction({ status: "coding", id: "task-1" }, { project_status: "timeout" }), true);
  assert.equal(canResumeWithInstruction({ status: "dev_blocked", id: "task-1" }, { project_status: "blocked" }), false);
  assert.equal(canResumeWithInstruction({ status: "coding", id: "task-1" }, { project_status: "running" }), false);
});

test("继续说明通过 resume-once 仅提交给下一轮", async () => {
  const { resumeWithInstruction } = await loadRunInstructionModule();
  const calls = [];
  const result = await resumeWithInstruction({
    request: async (path, options) => {
      calls.push({ path, options });
      return { status: "completed", summary: "已继续" };
    },
    projectId: "ad group",
    reason: "继续完成 Rust FC",
  });

  assert.deepEqual(calls, [
    {
      path: "/api/v1/projects/ad%20group/resume-once",
      options: {
        method: "POST",
        body: JSON.stringify({ reason: "继续完成 Rust FC" }),
      },
    },
  ]);
  assert.equal(result.status, "completed");
});
