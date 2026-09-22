const blockedTaskStates = new Set(["spec_blocked", "prd_blocked", "dev_blocked", "blocked"]);

export function isBlockedTask(value) {
  if (!value) return false;
  const state = typeof value === "string" ? value : value.state || value.status;
  return blockedTaskStates.has(state);
}
