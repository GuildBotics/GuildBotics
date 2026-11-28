// The time axis of the chart: the span it shows and the ticks along it.
import type { TicketTimelineTicket } from "../api/client";
import type { Span } from "./gaps";

const MINUTE = 60 * 1000;
const DAY = 24 * 60 * MINUTE;
/** Tick intervals, each dividing a day or a whole number of days. */
const STEPS = [10, 30, 60, 180, 360, 720, 1440, 2880, 10080].map((minutes) => minutes * MINUTE);

/**
 * The span of one ticket's work: from when it moved into progress (or was
 * created, if it never did) until it closed, or now, with a little room on
 * each side.
 */
export function focusSpan(ticket: TicketTimelineTicket, now: number): Span {
  const started = Date.parse(ticket.started_at ?? ticket.created_at);
  const closed = ticket.closed_at ? Date.parse(ticket.closed_at) : now;
  const end = Math.max(closed, started + 60 * MINUTE);
  const room = Math.max(10 * MINUTE, (end - started) * 0.05);
  return { start: started - room, end: end + room };
}

function localMidnight(time: number): number {
  const date = new Date(time);
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/** At most `most` ticks inside `span`, on round local times. */
export function ticks(span: Span, most = 8): { times: number[]; step: number } {
  const length = span.end - span.start;
  const step = STEPS.find((s) => length / s <= most) ?? STEPS[STEPS.length - 1];
  const times: number[] = [];
  for (let time = localMidnight(span.start); time < span.end; time += step) {
    if (time > span.start) times.push(time);
  }
  return { times, step };
}

/** A tick's label: the time of day, or the date where a day begins or a tick spans days. */
export function tickLabel(time: number, step: number): string {
  const date = new Date(time);
  const day = `${date.getMonth() + 1}/${date.getDate()}`;
  if (step >= DAY || time === localMidnight(time)) return day;
  return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}
