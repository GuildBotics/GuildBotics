import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getTicketTimeline, type TicketTimelineResponse } from "../api/client";
import i18n from "../i18n";
import { renderWithProviders } from "../test/render";
import { memberRows, TicketTimelinePage, workModes } from "./TicketTimeline";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getTicketTimeline: vi.fn() };
});

const DAY = new Date(2026, 6, 1);
/** A local time of the shown day. */
const at = (hours: number, minutes = 0) => new Date(2026, 6, 1, hours, minutes).toISOString();
const HUB = "http://hub.test/workspaces/acme/repos/demo";

function workflow(start: string, end: string, outcome: string | null) {
  return {
    trace_id: "resumed-run",
    person_id: "aiko",
    mode: "workflow" as const,
    title: "Work",
    started_at: start,
    ended_at: end,
    outcome,
  };
}

const TIMELINE: TicketTimelineResponse = {
  start: DAY.toISOString(),
  end: new Date(2026, 6, 2).toISOString(),
  // Closed on the day: also tickets no bar of the day belongs to.
  summary: { closed_tickets: 4, merged_pulls: 2 },
  members: [
    { person_id: "aiko", name: "Aiko", person_type: "agent" },
    { person_id: "yuki", name: "Yuki", person_type: "agent" },
    { person_id: "otota", name: "Owner", person_type: "human" },
  ],
  tickets: [
    {
      url: `${HUB}/tickets/445`,
      label: "#445",
      title: "Timeline",
      state: "open",
      created_at: new Date(2026, 5, 30).toISOString(),
      closed_at: null,
      started_at: new Date(2026, 5, 30, 17).toISOString(),
      // It asked the evening before: the day began waiting for the answer.
      previous: {
        kind: "workflow",
        person_id: "aiko",
        outcome: "asking",
        started_at: new Date(2026, 5, 30, 18).toISOString(),
        ended_at: new Date(2026, 5, 30, 18, 40).toISOString(),
      },
      work: [],
      events: [
        { kind: "comment", at: at(12, 10), person_id: "otota", url: `${HUB}/tickets/445#c1` },
      ],
      pulls: [
        {
          url: `${HUB}/pulls/7`,
          label: "!7",
          title: "Draw the timeline",
          state: "open",
          merged: false,
          person_id: "aiko",
          created_at: at(9, 30),
          merged_at: null,
          closed_at: null,
          // One run, resumed after it asked: two bars with the wait between.
          work: [workflow(at(9), at(10, 5), "asking"), workflow(at(12, 19), at(13), null)],
          reviews: [
            {
              trace_id: "review",
              person_id: "yuki",
              mode: "workflow",
              title: "Review",
              started_at: at(13, 30),
              ended_at: at(13, 40),
              outcome: "done",
            },
          ],
          jobs: [
            {
              name: "test",
              attempts: [
                {
                  id: "att_1",
                  job: "test",
                  sha: "abc",
                  attempt: 1,
                  runner: "r1",
                  status: "completed",
                  conclusion: "success",
                  created_at: at(13),
                  started_at: at(13, 20),
                  finished_at: at(13, 25),
                  url: `${HUB}/jobs/att_1`,
                },
              ],
            },
            {
              // Done before test: it did not decide when the CI finished.
              name: "lint",
              attempts: [
                {
                  id: "att_2",
                  job: "lint",
                  sha: "abc",
                  attempt: 1,
                  runner: "r2",
                  status: "completed",
                  conclusion: "success",
                  created_at: at(13),
                  started_at: at(13, 21),
                  finished_at: at(13, 22),
                  url: `${HUB}/jobs/att_2`,
                },
              ],
            },
          ],
        },
      ],
    },
    {
      url: `${HUB}/tickets/446`,
      label: "#446",
      title: "Other work",
      state: "closed",
      created_at: at(8),
      closed_at: at(16),
      // Created at 8:00 but only moved into progress at 10:30.
      started_at: at(10, 30),
      previous: null,
      work: [
        {
          trace_id: "other",
          person_id: "yuki",
          mode: "workflow",
          title: "Run",
          started_at: at(11),
          ended_at: at(12),
          outcome: null,
        },
      ],
      events: [],
      pulls: [],
    },
    {
      url: `${HUB}/tickets/447`,
      label: "#447",
      title: "Talked about",
      state: "open",
      created_at: at(8),
      closed_at: null,
      started_at: null,
      previous: null,
      // A conversation goes on alongside other work.
      work: [
        {
          trace_id: "talk",
          person_id: "yuki",
          mode: "interactive",
          title: "Talk",
          started_at: at(13, 32),
          ended_at: at(13, 38),
          outcome: null,
        },
      ],
      events: [],
      pulls: [],
    },
  ],
};

function detail() {
  return screen.getByRole("status", { name: "選んだ待ち" });
}

/** The chart's 1440 pixels are the minutes of the day. */
function minuteWide(element: HTMLElement) {
  vi.spyOn(element, "getBoundingClientRect").mockReturnValue({
    left: 0,
    width: 1440,
    top: 0,
    height: 32,
  } as DOMRect);
}

describe("TicketTimelinePage", () => {
  beforeEach(async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.setSystemTime(new Date(2026, 6, 1, 20));
    vi.mocked(getTicketTimeline).mockResolvedValue(TIMELINE);
    await i18n.changeLanguage("ja");
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("asks for the selected day and spells out a gap chosen by click, then moves with Tab", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    const wait = await screen.findByRole("button", { name: /待ち 2 時間 14 分（10:05〜12:19）/ });
    expect(getTicketTimeline).toHaveBeenCalledWith({
      start: DAY.toISOString(),
      end: new Date(2026, 6, 2).toISOString(),
    });
    expect(detail()).toBeEmptyDOMElement();

    await user.click(wait);
    expect(
      within(detail())
        .getAllByText(/./)
        .map((line) => line.textContent),
    ).toEqual([
      "#445 待ち 2 時間 14 分（10:05〜12:19）",
      "前：Aiko のワークフロー（09:00〜10:05、質問で終了）",
      "後：Aiko のワークフロー（12:19〜13:00）",
      "あいだ：12:10 Owner のコメント",
    ]);
    expect(wait).toHaveAttribute("aria-pressed", "true");

    // Tab goes on to the next gap; the queue time of a CI job is a gap of its own.
    await user.tab();
    expect(detail()).toHaveTextContent("#445 待ち 20 分（13:00〜13:20）");
    expect(detail()).toHaveTextContent(
      "後：CI のジョブ test（r1、13:20〜13:25、13:00 から待ち、成功で終了）",
    );
    await user.tab({ shift: true });
    await user.tab({ shift: true });
    expect(detail()).toHaveTextContent("#445 待ち 9 時間 0 分（00:00〜09:00）");
    expect(detail()).toHaveTextContent(
      "前：Aiko のワークフロー（6/30 18:00〜6/30 18:40、質問で終了）",
    );
    // Its waits begin when the other ticket moved into progress, not when it was created.
    const other = screen.getByTestId(`track-${HUB}/tickets/446`);
    expect(
      within(other)
        .getAllByRole("button")
        .map((gap) => gap.getAttribute("aria-label")),
    ).toEqual([
      "待ち 30 分（10:30〜11:00） 前：作業中に移動（10:30） 後：Yuki のワークフロー（11:00〜12:00）",
      expect.stringMatching(/^待ち 4 時間 0 分（12:00〜16:00）/),
    ]);

    // The open ticket's last gap goes on.
    await user.click(
      screen.getByRole("button", { name: /待ち 6 時間 20 分（13:40〜、続いている）/ }),
    );
    expect(detail()).toHaveTextContent("前：Yuki のレビュー（13:30〜13:40、完了で終了）");
    await user.keyboard("{Escape}");
    expect(detail()).toBeEmptyDOMElement();
  });

  it("opens a ticket down to its jobs and highlights the ticket's gap from any of its rows", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    const openTicket = await screen.findByRole("button", { name: "#445 Timeline を開く" });
    expect(screen.queryByText("Draw the timeline")).not.toBeInTheDocument();
    await user.click(openTicket);
    expect(openTicket).toHaveAttribute("aria-expanded", "true");
    await user.click(screen.getByRole("button", { name: "!7 Draw the timeline を開く" }));
    for (const row of ["ワークフロー", "レビュー", "CI"])
      expect(screen.getByText(row, { selector: ".timeline-label-text" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "!7 CI を開く" }));
    expect(screen.getByText("test", { selector: ".timeline-label-text" })).toBeInTheDocument();
    // Rows that lay the bars under them over each other leave them unlabelled;
    // the rows of their own show whose they are.
    for (const summary of [`${HUB}/tickets/445`, `${HUB}/pulls/7`, `${HUB}/pulls/7:ci`]) {
      const bars = within(screen.getByTestId(`track-${summary}`)).getAllByRole("img");
      expect(bars.map((bar) => bar.textContent)).toEqual(bars.map(() => ""));
      expect(bars.every((bar) => bar.getAttribute("aria-label"))).toBe(true);
    }
    expect(
      within(screen.getByTestId(`track-${HUB}/pulls/7:work`))
        .getAllByRole("img")
        .map((bar) => bar.textContent),
    ).toEqual(["Aiko", "Aiko"]);
    const job = screen.getByTestId(`track-${HUB}/pulls/7:ci:test`);
    expect(within(job).getByRole("img", { name: /CI のジョブ test（r1、/ })).toBeInTheDocument();
    // Its time in the queue is blank like any wait; its text tells when it began.
    expect(within(job).getByRole("img", { name: /13:00 から待ち/ })).toBeInTheDocument();
    // The job that ended the CI has its own row; the others fold into one.
    expect(screen.queryByTestId(`track-${HUB}/pulls/7:ci:lint`)).not.toBeInTheDocument();
    const others = screen.getByTestId(`track-${HUB}/pulls/7:ci:others`);
    expect(within(others).getByRole("img", { name: /CI のジョブ lint（r2、/ })).toHaveTextContent(
      "",
    );
    await user.click(screen.getByRole("button", { name: "!7 ほかのジョブ（1 件） を開く" }));
    const lint = screen.getByTestId(`track-${HUB}/pulls/7:ci:lint`);
    expect(within(lint).getByRole("img")).toHaveTextContent("r2");

    // Over the reviews row where nothing of the ticket ran: the ticket's gap.
    const reviews = screen.getByTestId(`track-${HUB}/pulls/7:reviews`);
    minuteWide(reviews);
    fireEvent.mouseMove(reviews, { clientX: 11 * 60 });
    expect(detail()).toHaveTextContent("#445 待ち 2 時間 14 分（10:05〜12:19）");
    const bands = document.querySelectorAll(".timeline-gap-band");
    const strong = document.querySelectorAll(".timeline-gap-band-strong");
    // Every row of the chart gets the band; only the ticket's own rows strongly.
    expect(bands.length).toBe(document.querySelectorAll(".timeline-track").length - 1);
    // The closed rows of #446 and #447 get it faintly.
    expect(strong.length).toBe(bands.length - 2);
    // Where a bar of the ticket runs, there is no gap to show.
    fireEvent.mouseMove(reviews, { clientX: 13 * 60 + 35 });
    expect(detail()).toBeEmptyDOMElement();
    fireEvent.mouseMove(reviews, { clientX: 11 * 60 });
    fireEvent.mouseLeave(reviews);
    expect(detail()).toBeEmptyDOMElement();

    // A click on a child row keeps the gap selected.
    fireEvent.click(reviews, { clientX: 11 * 60 });
    expect(detail()).toHaveTextContent("#445 待ち 2 時間 14 分（10:05〜12:19）");
  });

  it("focuses on a ticket over the span of its work, open down to its jobs, and goes back", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    await user.click(await screen.findByRole("link", { name: "#445 Timeline" }));
    // From its start (6/30 17:00) until now (7/1 20:00), with room on each side.
    const asked = vi.mocked(getTicketTimeline).mock.lastCall![0];
    expect(Date.parse(asked.start)).toBeLessThan(new Date(2026, 5, 30, 17).getTime());
    expect(Date.parse(asked.end)).toBeGreaterThan(new Date(2026, 6, 1, 20).getTime());
    expect(Date.parse(asked.end) - Date.parse(asked.start)).toBeLessThan(30 * 60 * 60 * 1000);

    // The ticket itself is linked from the heading; only it is charted.
    const heading = await screen.findByRole("link", { name: "#445 Timeline" });
    expect(heading).toHaveAttribute("href", `${HUB}/tickets/445`);
    expect(screen.queryByText("Other work")).not.toBeInTheDocument();
    for (const row of ["ワークフロー", "レビュー", "CI", "test"])
      expect(screen.getByText(row, { selector: ".timeline-label-text" })).toBeInTheDocument();

    // What its reviewer did meanwhile, on another ticket.
    expect(screen.getByText("同じ時間のメンバーの動き")).toBeInTheDocument();
    const yuki = screen.getByTestId("track-members:yuki");
    // Its runs only: a conversation elsewhere does not keep a member from reviewing.
    expect(
      within(yuki)
        .getAllByRole("img")
        .map((bar) => bar.getAttribute("aria-label")),
    ).toEqual([expect.stringMatching(/^Yuki のワークフロー（#446、/)]);
    expect(within(screen.getByTestId("track-members:aiko")).queryAllByRole("img")).toEqual([]);

    await user.click(screen.getByRole("link", { name: "1 日全体へ戻る" }));
    expect(await screen.findByText("Other work")).toBeInTheDocument();
  });

  it("marks each ticket with how its own work went on", async () => {
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    const mode = async (key: string) => {
      const row = (await screen.findByTestId(`track-${HUB}/tickets/${key}`)).parentElement!;
      return within(row)
        .queryAllByRole("img", { name: /で進めた$/ })
        .map((icon) => icon.getAttribute("aria-label"));
    };
    // #445's reviews are runs too, but only its own work counts.
    expect(await mode("445")).toEqual(["ワークフローで進めた"]);
    expect(await mode("447")).toEqual(["対話で進めた"]);
  });

  it("shows both marks for a ticket worked on both ways, and none where only CI ran", () => {
    const [first, second] = TIMELINE.tickets;
    const day = { start: DAY.getTime(), end: new Date(2026, 6, 2).getTime() };
    expect(
      workModes({ ...first, work: second.work.concat(TIMELINE.tickets[2].work) }, day),
    ).toEqual(["workflow", "interactive"]);
    expect(
      workModes({ ...first, work: [], pulls: first.pulls.map((p) => ({ ...p, work: [] })) }, day),
    ).toEqual([]);
  });

  it("keeps a span that reaches now up to date, but not a past one", async () => {
    const past = `?ticket=${encodeURIComponent(`${HUB}/tickets/446`)}&start=${at(10)}&end=${at(16)}`;
    renderWithProviders(<TicketTimelinePage />, { router: "memory", initialPath: `/${past}` });
    await screen.findAllByText("Other work");
    vi.mocked(getTicketTimeline).mockClear();
    await vi.advanceTimersByTimeAsync(61000);
    expect(getTicketTimeline).not.toHaveBeenCalled();
  });

  it("refreshes the day that is still going on", async () => {
    vi.setSystemTime(new Date(2026, 6, 1, 15));
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    await screen.findByText("Other work");
    vi.mocked(getTicketTimeline).mockClear();
    await vi.advanceTimersByTimeAsync(31000);
    expect(getTicketTimeline).toHaveBeenCalled();
  });

  it("counts only the work shown in the span", async () => {
    const [first, second, third] = TIMELINE.tickets;
    // The run on #446 and the talk on #447 ended before the morning shown.
    const morning = { start: DAY.getTime(), end: new Date(2026, 6, 1, 10, 45).getTime() };
    expect(workModes({ ...first, work: second.work.concat(third.work) }, morning)).toEqual([
      "workflow",
    ]);
    // Yuki's review is after the morning: it is not among the ticket's members then.
    const t = i18n.getFixedT("ja");
    expect(memberRows(t, TIMELINE, first, morning).map((row) => row.key)).toEqual([
      "members",
      "members:aiko",
    ]);

    const before = structuredClone(TIMELINE);
    before.tickets[1].work[0].started_at = new Date(2026, 5, 30, 22).toISOString();
    before.tickets[1].work[0].ended_at = new Date(2026, 5, 30, 23).toISOString();
    vi.mocked(getTicketTimeline).mockResolvedValue(before);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    const row = (await screen.findByTestId(`track-${HUB}/tickets/446`)).parentElement!;
    expect(within(row).queryAllByRole("img", { name: /で進めた$/ })).toEqual([]);
    await user.click(screen.getByRole("button", { name: "#446 Other work を開く" }));
    expect(screen.queryByTestId(`track-${HUB}/tickets/446:work`)).not.toBeInTheDocument();
  });

  it("marks a pull request's merge on its row and its ticket's", async () => {
    const merged = structuredClone(TIMELINE);
    merged.tickets[0].pulls[0].merged_at = at(13, 50);
    vi.mocked(getTicketTimeline).mockResolvedValue(merged);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    const ticket = await screen.findByTestId(`track-${HUB}/tickets/445`);
    expect(within(ticket).getByRole("img", { name: "!7 のマージ（13:50）" })).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "#445 Timeline を開く" }));
    const pull = screen.getByTestId(`track-${HUB}/pulls/7`);
    expect(within(pull).getByRole("img", { name: "!7 のマージ（13:50）" })).toBeInTheDocument();
    // Only on the rows that stand for the pull request.
    await user.click(screen.getByRole("button", { name: "!7 Draw the timeline を開く" }));
    expect(
      within(screen.getByTestId(`track-${HUB}/pulls/7:work`)).queryByRole("img", {
        name: /のマージ/,
      }),
    ).not.toBeInTheDocument();
  });

  it("sums the day up beside its date, but not a focused ticket's span", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    const value = async (term: string) =>
      (await screen.findByText(term, { selector: "dt" })).nextElementSibling?.textContent;
    expect(await value("動いたチケット")).toBe("3");
    expect(await value("クローズしたチケット")).toBe("4");
    expect(await value("マージした PR")).toBe("2");
    await user.click(screen.getByRole("link", { name: "#445 Timeline" }));
    await screen.findByRole("link", { name: "1 日全体へ戻る" });
    expect(screen.queryByText("動いたチケット")).not.toBeInTheDocument();
  });

  it("draws only what the day shows, and a run still going on as running", async () => {
    const day = structuredClone(TIMELINE);
    const yuki = day.tickets[1].work[0];
    day.tickets[1].work.push(
      // Ended the day before: words for a gap, not a bar of this day.
      { ...yuki, trace_id: "before", started_at: at(-6), ended_at: at(-5) },
      { ...yuki, trace_id: "now", started_at: at(15), ended_at: at(15, 30), running: true },
    );
    // Merged the next day.
    day.tickets[0].pulls[0].merged_at = new Date(2026, 6, 2, 10).toISOString();
    vi.mocked(getTicketTimeline).mockResolvedValue(day);
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    const other = await screen.findByTestId(`track-${HUB}/tickets/446`);
    expect(
      within(other)
        .getAllByRole("img")
        .map((bar) => bar.getAttribute("aria-label")),
    ).toEqual([
      "Yuki のワークフロー（11:00〜12:00）",
      "Yuki のワークフロー（15:00〜15:30、実行中）",
    ]);
    const ticket = screen.getByTestId(`track-${HUB}/tickets/445`);
    expect(within(ticket).queryByRole("img", { name: /のマージ/ })).not.toBeInTheDocument();
  });

  it("says when no ticket moved on the day", async () => {
    vi.mocked(getTicketTimeline).mockResolvedValue({ ...TIMELINE, tickets: [] });
    renderWithProviders(<TicketTimelinePage />, { router: "memory" });
    expect(await screen.findByText("この日に動いたチケットはありません。")).toBeInTheDocument();
  });
});
