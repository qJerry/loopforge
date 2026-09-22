import test from "node:test";
import assert from "node:assert/strict";

import { buildTargetReviewRows } from "../src/target-review.mjs";

test("待验收对象同时展示子仓、功能分支、worktree、代码提交和验证结果", () => {
  const task = {
    target_branch: "main",
    target_plan: {
      targets: [
        {
          id: "ad-api-go",
          project: "ad-api-go",
          repo: "ad-api-go",
          branch: "feature/ad/task-1/ad-api-go",
          worktree_path: "/workspace/ad/.loopforge-worktrees/ad-api-go/task-1-ad-api-go",
        },
      ],
    },
    targets: [
      {
        id: "ad-api-go",
        project: "ad-api-go",
        repo_path: "/workspace/ad/ad-api-go",
        worktree_path: "/workspace/ad/.loopforge-worktrees/ad-api-go/task-1-ad-api-go",
        git: {
          feature_branch: "feature/ad/task-1/ad-api-go",
          branch_revision: "65e0beb6b86dd940f5181771dc5c63de9b70015c",
          base_revision: "1c5d046538413523861afe2b88af50e6caccfe86",
        },
        docs: {
          revision: "1c5d046538413523861afe2b88af50e6caccfe86",
        },
        execution_summary: "Offer Feed 与调度能力已经完成。",
        validation: {
          status: "passed",
          summary: "全部门禁通过，工作区清洁。",
          commands: ["scripts/test.sh all", "git diff --check"],
        },
      },
    ],
  };

  assert.deepEqual(buildTargetReviewRows(task), [
    {
      id: "ad-api-go",
      project: "ad-api-go",
      repo: "/workspace/ad/ad-api-go",
      branch: "feature/ad/task-1/ad-api-go",
      targetBranch: "main",
      worktree: "/workspace/ad/.loopforge-worktrees/ad-api-go/task-1-ad-api-go",
      codeRevision: "65e0beb6b86dd940f5181771dc5c63de9b70015c",
      codeRevisionShort: "65e0beb6",
      baseRevision: "1c5d046538413523861afe2b88af50e6caccfe86",
      docRevision: "1c5d046538413523861afe2b88af50e6caccfe86",
      summary: "Offer Feed 与调度能力已经完成。",
      validationStatus: "passed",
      validationSummary: "全部门禁通过，工作区清洁。",
      validationCommands: ["scripts/test.sh all", "git diff --check"],
      after: "无",
      documents: "未关联",
    },
  ]);
});
