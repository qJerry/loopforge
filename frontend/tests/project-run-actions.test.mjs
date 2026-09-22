import assert from "node:assert/strict";
import test from "node:test";

import { projectRunActionModel } from "../src/project-run-actions.mjs";

test("项目运行中不提供第二个启动入口", () => {
  const model = projectRunActionModel({
    projectStatus: "running",
    task: { id: "task-1", status: "coding", agent_status: "running" },
  });

  assert.equal(model.primaryAction, null);
  assert.deepEqual(model.taskNavigation, { label: "查看当前任务", kind: "task", taskId: "task-1" });
  assert.equal(model.running, true);
});

test("项目空闲时按任务阶段只给出一个推进动作", () => {
  assert.deepEqual(
    projectRunActionModel({ projectStatus: "idle", task: { id: "task-1", status: "open" } }).primaryAction,
    { label: "开始下一个任务", kind: "start" },
  );
  assert.deepEqual(
    projectRunActionModel({ projectStatus: "idle", task: { id: "task-1", status: "spec_ready" } }).primaryAction,
    { label: "开始开发", kind: "start" },
  );
  assert.deepEqual(
    projectRunActionModel({ projectStatus: "idle", task: { id: "task-1", status: "coding" } }).primaryAction,
    { label: "继续开发", kind: "start" },
  );
});

test("阻塞、验收与收尾状态使用对应的人工处理入口", () => {
  assert.deepEqual(
    projectRunActionModel({ projectStatus: "blocked", task: { id: "task-1", status: "dev_blocked" } }).primaryAction,
    { label: "处理阻塞", kind: "recovery" },
  );
  assert.deepEqual(
    projectRunActionModel({ projectStatus: "idle", task: { id: "task-1", status: "ready_for_review" } }).primaryAction,
    { label: "查看验收", kind: "task", taskId: "task-1" },
  );
  assert.deepEqual(
    projectRunActionModel({ projectStatus: "idle", task: { id: "task-1", status: "merged" } }).primaryAction,
    { label: "完成收尾", kind: "task", taskId: "task-1" },
  );
});

test("超时任务只显示继续当前任务", () => {
  const model = projectRunActionModel({
    projectStatus: "timeout",
    latestRunStatus: "timeout_continue",
    task: { id: "task-1", status: "coding", agent_status: "timeout" },
  });

  assert.deepEqual(model.primaryAction, { label: "继续当前任务", kind: "resume" });
  assert.deepEqual(model.taskNavigation, { label: "查看当前任务", kind: "task", taskId: "task-1" });
});
