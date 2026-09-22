import assert from "node:assert/strict";
import test from "node:test";

test("默认线索视图只展示待处理状态，全部视图保留误报历史", async () => {
  let visibleClues;
  try {
    ({ visibleClues } = await import("../src/clue-filters.mjs"));
  } catch (error) {
    assert.fail(`线索筛选模块尚未实现：${error.message}`);
  }
  const clues = [
    { id: "open", status: "open" },
    { id: "confirm", status: "needs_confirmation" },
    { id: "false-positive", status: "false_positive" },
    { id: "resolved", status: "resolved" },
  ];

  assert.deepEqual(visibleClues(clues).map((clue) => clue.id), ["open", "confirm"]);
  assert.deepEqual(visibleClues(clues, "all").map((clue) => clue.id), ["open", "confirm", "false-positive", "resolved"]);
});
