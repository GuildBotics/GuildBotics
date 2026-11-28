import { describe, expect, it } from "vitest";

import type { TicketTimelineTicket } from "../api/client";
import { focusSpan, tickLabel, ticks } from "./scale";

const at = (day: number, hours: number, minutes = 0) =>
  new Date(2026, 6, day, hours, minutes).getTime();

function ticket(overrides: Partial<TicketTimelineTicket>): TicketTimelineTicket {
  return {
    url: "http://hub.test/workspaces/w/repos/r/tickets/1",
    label: "#1",
    title: "Ticket",
    state: "open",
    created_at: new Date(at(1, 8)).toISOString(),
    closed_at: null,
    started_at: null,
    previous: null,
    work: [],
    pulls: [],
    events: [],
    ...overrides,
  };
}

describe("the time axis", () => {
  it("ticks a day every three hours and a few hours every half hour", () => {
    const day = ticks({ start: at(1, 0), end: at(2, 0) });
    expect(day.times.map((time) => tickLabel(time, day.step))).toEqual([
      "03:00",
      "06:00",
      "09:00",
      "12:00",
      "15:00",
      "18:00",
      "21:00",
    ]);
    const hours = ticks({ start: at(1, 9, 5), end: at(1, 12, 20) });
    expect(hours.times.map((time) => tickLabel(time, hours.step))).toEqual([
      "09:30",
      "10:00",
      "10:30",
      "11:00",
      "11:30",
      "12:00",
    ]);
  });

  it("names the date where a day begins and ticks days across a long span", () => {
    const night = ticks({ start: at(1, 18), end: at(2, 9) });
    expect(night.times.map((time) => tickLabel(time, night.step))).toContain("7/2");
    const week = ticks({ start: at(1, 12), end: at(9, 12) });
    expect(week.times.map((time) => tickLabel(time, week.step))).toEqual([
      "7/2",
      "7/3",
      "7/4",
      "7/5",
      "7/6",
      "7/7",
      "7/8",
      "7/9",
    ]);
  });
});

describe("the span of a ticket's work", () => {
  it("runs from its start until it closed, or until now, with room on each side", () => {
    const closed = focusSpan(
      ticket({
        started_at: new Date(at(1, 9)).toISOString(),
        closed_at: new Date(at(1, 12)).toISOString(),
      }),
      at(3, 0),
    );
    // Five percent of three hours is less than the ten minutes' room.
    expect(closed).toEqual({ start: at(1, 8, 50), end: at(1, 12, 10) });
    const open = focusSpan(ticket({ started_at: new Date(at(1, 0)).toISOString() }), at(2, 0));
    expect(open.end - at(2, 0)).toBe(72 * 60 * 1000);
    // Never started: from its creation.
    expect(focusSpan(ticket({}), at(1, 18)).start).toBe(at(1, 8) - 30 * 60 * 1000);
  });
});
