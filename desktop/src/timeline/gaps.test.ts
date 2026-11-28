import { describe, expect, it } from "vitest";

import type { TicketTimelineTicket } from "../api/client";
import {
  MIN_GAP_MS,
  attemptBar,
  endingJobs,
  findGaps,
  gapAt,
  openSpan,
  ticketGaps,
  type TimedBar,
} from "./gaps";

const MIN = 60 * 1000;
const day = { start: 0, end: 24 * 60 * MIN };
const at = (minutes: number) => new Date(minutes * MIN).toISOString();

function bar(start: number, end: number, person = "aiko"): TimedBar {
  return {
    start: start * MIN,
    end: end * MIN,
    kind: "workflow",
    person_id: person,
    outcome: null,
  };
}

function ticket(overrides: Partial<TicketTimelineTicket> = {}): TicketTimelineTicket {
  return {
    url: "http://hub.test/workspaces/w/repos/r/tickets/1",
    label: "#1",
    title: "Ticket",
    state: "open",
    created_at: at(-60),
    closed_at: null,
    started_at: at(-60),
    previous: null,
    work: [],
    pulls: [],
    events: [],
    ...overrides,
  };
}

describe("findGaps", () => {
  it("counts overlapping bars as one and keeps the bars on each side of a gap", () => {
    const first = bar(60, 120);
    const overlapping = bar(90, 180, "yuki");
    const next = bar(300, 360);
    const earlier = bar(-600, -540);
    const gaps = findGaps([next, first, overlapping], { start: 0, end: 600 * MIN }, earlier, true);
    expect(gaps.map((gap) => [gap.start / MIN, gap.end / MIN])).toEqual([
      [0, 60],
      [180, 300],
      [360, 600],
    ]);
    expect(gaps[0]).toMatchObject({ before: earlier, after: first });
    // The bar that ended last bounds the gap, not the one that began first.
    expect(gaps[1]).toMatchObject({ before: overlapping, after: next, continuing: false });
    expect(gaps[2]).toMatchObject({ before: next, after: null, continuing: true });
  });

  it("leaves out hand-overs shorter than a minute", () => {
    const gaps = findGaps(
      [bar(40, 50), { ...bar(50, 60), start: 50 * MIN + MIN_GAP_MS - 1 }, bar(70, 80)],
      { start: 40 * MIN, end: 80 * MIN },
      null,
      false,
    );
    expect(gaps.map((gap) => [gap.start / MIN, gap.end / MIN])).toEqual([[60, 70]]);
  });

  it("finds no gap where bars cover the whole open span, and ignores bars outside it", () => {
    const gaps = findGaps(
      [bar(-120, -60), bar(-30, 100), bar(200, 300)],
      { start: 0, end: 100 * MIN },
      null,
      true,
    );
    expect(gaps).toEqual([]);
  });
});

describe("the day's first wait", () => {
  const work = {
    trace_id: "run",
    person_id: "aiko",
    mode: "workflow" as const,
    title: "Run",
    started_at: at(600),
    ended_at: at(660),
    outcome: null,
  };

  it("begins when the ticket moved into progress, not with planning before it", () => {
    const planning = {
      ...work,
      mode: "interactive" as const,
      started_at: at(100),
      ended_at: at(150),
    };
    const { gaps } = ticketGaps(
      ticket({ created_at: at(90), started_at: at(300), work: [planning, work] }),
      day,
      700 * MIN,
    );
    expect(gaps.map((gap) => [gap.start / MIN, gap.end / MIN])).toEqual([
      [300, 600],
      [660, 700],
    ]);
    expect(gaps[0]).toMatchObject({ before: null, fromStart: true });
    // A ticket never moved into progress waited for nothing.
    expect(ticketGaps(ticket({ started_at: null, work: [work] }), day, 700 * MIN).gaps).toEqual([]);
  });

  it("follows what ran last before the day on a ticket already being worked on", () => {
    const previous = {
      kind: "workflow" as const,
      person_id: "aiko",
      outcome: "asking",
      started_at: at(-120),
      ended_at: at(-90),
    };
    const { gaps } = ticketGaps(
      ticket({ started_at: at(-200), previous, work: [work] }),
      day,
      700 * MIN,
    );
    expect(gaps[0]).toMatchObject({ start: 0, end: 600 * MIN, fromStart: false });
    expect(gaps[0].before).toMatchObject({ end: -90 * MIN, outcome: "asking", kind: "workflow" });
  });
});

describe("the open span and the ticket's bars", () => {
  it("is the part of the day the ticket was open, up to now", () => {
    expect(openSpan(ticket(), day, 600 * MIN)).toEqual({ start: 0, end: 600 * MIN });
    expect(openSpan(ticket({ started_at: at(90), closed_at: at(120) }), day, day.end)).toEqual({
      start: 90 * MIN,
      end: 120 * MIN,
    });
    expect(openSpan(ticket({ closed_at: at(-10) }), day, day.end)).toBeNull();
    expect(openSpan(ticket({ started_at: null }), day, day.end)).toBeNull();
  });

  it("starts a CI job's bar when a runner took it, so its queue time is a gap", () => {
    const attempt = {
      id: "att",
      job: "test",
      sha: "abc",
      attempt: 1,
      runner: "r1",
      status: "completed",
      conclusion: "success",
      created_at: at(100),
      started_at: at(130),
      finished_at: at(140),
      url: "http://hub.test/jobs/att",
    };
    expect(attemptBar(attempt, day.end)).toMatchObject({
      start: 130 * MIN,
      end: 140 * MIN,
      queued: 100 * MIN,
      outcome: "success",
    });
    expect(attemptBar({ ...attempt, started_at: null, finished_at: null }, day.end)).toBeNull();
    // A running job runs until now.
    expect(attemptBar({ ...attempt, finished_at: null }, 150 * MIN)?.end).toBe(150 * MIN);

    const pull = {
      url: "http://hub.test/workspaces/w/repos/r/pulls/1",
      label: "!1",
      title: "Pull",
      state: "open",
      merged: false,
      person_id: "aiko",
      created_at: at(0),
      merged_at: null,
      closed_at: null,
      work: [
        {
          trace_id: "run",
          person_id: "aiko",
          mode: "workflow" as const,
          title: "Run",
          started_at: at(60),
          ended_at: at(100),
          outcome: "awaiting_checks",
        },
      ],
      reviews: [],
      jobs: [{ name: "test", attempts: [attempt] }],
    };
    const { gaps } = ticketGaps(ticket({ pulls: [pull], closed_at: at(200) }), day, day.end);
    expect(gaps.map((gap) => [gap.start / MIN, gap.end / MIN])).toEqual([
      [0, 60],
      [100, 130],
      [140, 200],
    ]);
    expect(gaps[1].after?.kind).toBe("job");
    // Closed at the end of the last gap: the wait does not go on.
    expect(gaps[2].continuing).toBe(false);
    expect(gapAt(gaps, 110 * MIN)).toBe(1);
    expect(gapAt(gaps, 135 * MIN)).toBe(-1);
  });
});

describe("the jobs that ended the CI", () => {
  const attempt = (
    job: string,
    sha: string,
    finished: number | null,
    started: number | null = 0,
  ) => ({
    id: `${job}-${sha}`,
    job,
    sha,
    attempt: 1,
    runner: "r1",
    status: finished === null ? "in_progress" : "completed",
    conclusion: finished === null ? null : "success",
    created_at: at(0),
    started_at: started === null ? null : at(started),
    finished_at: finished === null ? null : at(finished),
    url: `http://hub.test/jobs/${job}-${sha}`,
  });

  it("are the last to finish on each head, a running one lasting until now", () => {
    const jobs = [
      { name: "lint", attempts: [attempt("lint", "one", 5), attempt("lint", "two", 50)] },
      { name: "test", attempts: [attempt("test", "one", 30), attempt("test", "two", 20)] },
      { name: "e2e", attempts: [attempt("e2e", "two", null), attempt("e2e", "one", 99, null)] },
    ];
    // Head one: test (e2e never started); head two: e2e still runs at 60.
    expect(endingJobs(jobs, 60 * MIN)).toEqual(new Set(["test", "e2e"]));
    expect(endingJobs([], 60 * MIN)).toEqual(new Set());
  });
});
