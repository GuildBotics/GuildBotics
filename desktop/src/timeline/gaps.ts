// Where a ticket's work stood still: the time it was open in the day that no
// run, conversation, review or CI job of it covered.
import type { TicketTimelineAttempt, TicketTimelineBar, TicketTimelineTicket } from "../api/client";

export type Span = { start: number; end: number };

/** A bar of the chart: something of the ticket that was running. */
export type TimedBar = Span & {
  kind: "workflow" | "interactive" | "review" | "job";
  person_id: string;
  outcome: string | null;
  /** The job's name, for a CI job. */
  job?: string;
  runner?: string | null;
  /** When a CI job began to wait for a runner. */
  queued?: number;
  /** The other ticket it ran on, for a member's work elsewhere. */
  on?: string;
  /** A run still going on, until now. */
  running?: boolean;
};

export type Gap = Span & {
  /** The bar that ended where the gap begins, on an earlier day for the day's first wait. */
  before: TimedBar | null;
  /** The gap begins when the ticket's work started (it moved into progress). */
  fromStart: boolean;
  /** The bar that begins where the gap ends; none while it continues. */
  after: TimedBar | null;
  /** The ticket is still open and nothing ran since. */
  continuing: boolean;
};

/** Shorter stillness between two bars is the hand-over, not a wait. */
export const MIN_GAP_MS = 60 * 1000;

function workBar(bar: TicketTimelineBar, kind: TimedBar["kind"]): TimedBar | null {
  const start = Date.parse(bar.started_at);
  const end = Date.parse(bar.ended_at);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return {
    start,
    end: Math.max(start, end),
    kind,
    person_id: bar.person_id,
    outcome: bar.outcome,
    running: bar.running,
  };
}

/** Whether `span` shows in `day`: it overlaps it, or is a moment within it. */
export function shows(span: Span, day: Span): boolean {
  return span.start === span.end
    ? span.start >= day.start && span.start <= day.end
    : span.start < day.end && span.end > day.start;
}

export function sessionBars(bars: TicketTimelineBar[], review = false): TimedBar[] {
  return bars
    .map((bar) => workBar(bar, review ? "review" : bar.mode))
    .filter((bar): bar is TimedBar => bar !== null);
}

/** A CI job runs from its start; the time it was queued before that is a wait. */
export function attemptBar(attempt: TicketTimelineAttempt, now: number): TimedBar | null {
  if (!attempt.started_at) return null;
  const start = Date.parse(attempt.started_at);
  const end = attempt.finished_at ? Date.parse(attempt.finished_at) : now;
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return {
    start,
    end: Math.max(start, end),
    kind: "job",
    person_id: "",
    outcome: attempt.conclusion ?? attempt.status,
    job: attempt.job,
    runner: attempt.runner,
    queued: Date.parse(attempt.created_at),
  };
}

/**
 * The jobs that ended the CI of a pull request's heads: on each head, the job
 * that finished last (or still runs), until which its CI had not finished.
 */
export function endingJobs(
  jobs: { name: string; attempts: TicketTimelineAttempt[] }[],
  now: number,
): Set<string> {
  const last = new Map<string, { end: number; job: string }>();
  for (const job of jobs) {
    for (const attempt of job.attempts) {
      const bar = attemptBar(attempt, now);
      const ended = last.get(attempt.sha);
      if (bar && (!ended || bar.end > ended.end))
        last.set(attempt.sha, { end: bar.end, job: job.name });
    }
  }
  return new Set([...last.values()].map((ended) => ended.job));
}

/** Every bar of the ticket, of every row under it. Spans of being open are none of them. */
export function ticketBars(ticket: TicketTimelineTicket, now: number): TimedBar[] {
  const bars = sessionBars(ticket.work);
  for (const pull of ticket.pulls) {
    bars.push(...sessionBars(pull.work), ...sessionBars(pull.reviews, true));
    for (const job of pull.jobs) {
      for (const attempt of job.attempts) {
        const bar = attemptBar(attempt, now);
        if (bar) bars.push(bar);
      }
    }
  }
  return bars.sort((a, b) => a.start - b.start || a.end - b.end);
}

/**
 * The time in `day` the ticket's work went on, up to now: from when it moved
 * into progress until it closed. Before that it was planned or waited to be
 * picked up, which is no wait of its work; one never started has none.
 */
export function openSpan(ticket: TicketTimelineTicket, day: Span, now: number): Span | null {
  const started = ticket.started_at ? Date.parse(ticket.started_at) : NaN;
  if (Number.isNaN(started)) return null;
  const closed = ticket.closed_at ? Date.parse(ticket.closed_at) : Infinity;
  const start = Math.max(day.start, started);
  const end = Math.min(day.end, now, closed);
  return end > start ? { start, end } : null;
}

/**
 * The gaps of `bars` within `open`: the stretches no bar covers, each with
 * the bar before and after it. Bars overlapping each other count as one.
 * `earlier` is what ran last before `open`.
 */
export function findGaps(
  bars: TimedBar[],
  open: Span,
  earlier: TimedBar | null,
  stillOpen: boolean,
  started = NaN,
): Gap[] {
  const gaps: Gap[] = [];
  let cursor = open.start;
  let before = earlier;
  const sorted = [...bars].sort((a, b) => a.start - b.start || a.end - b.end);
  const push = (end: number, after: TimedBar | null) => {
    if (end - cursor < MIN_GAP_MS) return;
    gaps.push({
      start: cursor,
      end,
      before,
      after,
      fromStart: before === null && cursor === started,
      continuing: after === null && stillOpen,
    });
  };
  for (const bar of sorted) {
    if (bar.end <= cursor) {
      if (bar.end > open.start && (!before || bar.end >= before.end)) before = bar;
      continue;
    }
    if (bar.start >= open.end) break;
    if (bar.start > cursor) push(bar.start, bar);
    cursor = Math.max(cursor, bar.end);
    if (!before || bar.end >= before.end) before = bar;
  }
  if (cursor < open.end) push(open.end, null);
  return gaps;
}

/** What ran last on the ticket before the day, as a bar. */
export function previousBar(ticket: TicketTimelineTicket): TimedBar | null {
  const previous = ticket.previous;
  if (!previous) return null;
  const start = Date.parse(previous.started_at);
  const end = Date.parse(previous.ended_at);
  if (Number.isNaN(start) || Number.isNaN(end)) return null;
  return {
    start,
    end,
    kind: previous.kind,
    person_id: previous.person_id ?? "",
    outcome: previous.outcome,
    job: previous.job,
    runner: previous.runner,
  };
}

export function ticketGaps(
  ticket: TicketTimelineTicket,
  day: Span,
  now: number,
): { bars: TimedBar[]; gaps: Gap[] } {
  const bars = ticketBars(ticket, now);
  const open = openSpan(ticket, day, now);
  if (!open) return { bars, gaps: [] };
  // Open past the gap's end: it went on after the day (or until now).
  const stillOpen = !ticket.closed_at || Date.parse(ticket.closed_at) > open.end;
  const started = Date.parse(ticket.started_at ?? "");
  return { bars, gaps: findGaps(bars, open, previousBar(ticket), stillOpen, started) };
}

/** The gap containing `time`, if any. */
export function gapAt(gaps: Gap[], time: number): number {
  return gaps.findIndex((gap) => time >= gap.start && time < gap.end);
}
