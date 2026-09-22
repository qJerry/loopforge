const ACTIVE_CLUE_STATUSES = new Set(["open", "needs_confirmation"]);

export const DEFAULT_CLUE_SCOPE = "active";

export function visibleClues(clues, scope = DEFAULT_CLUE_SCOPE) {
  const rows = Array.isArray(clues) ? clues : [];
  if (scope === "all") return rows;
  return rows.filter((clue) => ACTIVE_CLUE_STATUSES.has(clue?.status || "open"));
}
