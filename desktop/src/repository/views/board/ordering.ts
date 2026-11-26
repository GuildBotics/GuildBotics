import { ticketRef, type Card, type Check, type CheckSet, type Lane } from "../../api/types";
import type { Tone } from "../../components/common";

export type CiRollup = "passing" | "failing" | "pending" | "not_registered" | "none";

export const CI_TONE: Record<CiRollup, Tone> = {
  passing: "success",
  failing: "danger",
  pending: "warning",
  not_registered: "warning",
  none: "neutral",
};

const FAILED = new Set<Check["conclusion"]>([
  "failure",
  "cancelled",
  "timed_out",
  "startup_failure",
]);

/** One status for a commit's checks; never reports success for missing checks. */
export function rollup(checks: CheckSet): CiRollup {
  if (checks.checks.length === 0) return checks.checks_expected ? "not_registered" : "none";
  if (checks.checks.some((c) => c.status === "completed" && FAILED.has(c.conclusion)))
    return "failing";
  if (checks.checks.some((c) => c.status !== "completed")) return "pending";
  return "passing";
}

export interface CardFilter {
  assignee: string; // "" any, "-" unassigned, otherwise a member name
  lane: Lane | ""; // "" any
  label: string; // "" any
  repository: string; // "" any, otherwise a repository path
  text: string;
}

export const NO_FILTER: CardFilter = {
  assignee: "",
  lane: "",
  label: "",
  repository: "",
  text: "",
};

export const hasFilter = (f: CardFilter): boolean =>
  !!(f.assignee || f.lane || f.label || f.repository || f.text.trim());

export function matchesFilter(card: Card, f: CardFilter): boolean {
  if (f.assignee && (card.assignee?.name ?? "-") !== f.assignee) return false;
  if (f.lane && card.lane !== f.lane) return false;
  if (f.label && !card.labels.includes(f.label)) return false;
  if (f.repository && card.repository !== f.repository) return false;
  const text = f.text.trim().toLowerCase();
  if (text && !`${ticketRef(card)} ${card.title} ${card.body}`.toLowerCase().includes(text))
    return false;
  return true;
}
