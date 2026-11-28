// The ticket timeline: each ticket that moved on a day as a Gantt chart, its
// waits left as the blank between bars and spelled out when selected.
import { Alert, Card, Group, Loader, Stack, Text, Title } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { Bot, ChevronDown, ChevronLeft, ChevronRight, MessageSquare } from "lucide-react";
import {
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
  type ReactNode,
} from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router";
import type { TFunction } from "i18next";

import {
  getTicketTimeline,
  type TicketTimelineAttempt,
  type TicketTimelineBar,
  type TicketTimelineResponse,
  type TicketTimelineTicket,
} from "../api/client";
import { DateNav, addDays, startOfLocalDay } from "../activity/DateNav";
import { inAppPath } from "../repository/router/routes";
import { WorkspaceLink } from "../ui/workspaceLinks";
import {
  attemptBar,
  endingJobs,
  gapAt,
  sessionBars,
  shows,
  ticketGaps,
  type Gap,
  type Span,
  type TimedBar,
} from "./gaps";
import { focusSpan, ticks, tickLabel } from "./scale";

const MEMBER_COLORS = ["blue", "grape", "teal", "orange", "pink", "cyan", "lime", "indigo"];

type Names = Map<string, string>;
type Selection = { ticket: number; gap: number };
type Row = {
  key: string;
  ticket: number;
  depth: number;
  label: ReactNode;
  bars: TimedBar[];
  toggle?: { open: boolean; name: string; onToggle: () => void };
  /** The bars of the rows under it laid over each other: unlabelled, as they overlap. */
  summary?: boolean;
  /** Moments without length, such as a merge: drawn as marks, not bars. */
  milestones?: { at: number; label: string }[];
};

/** The search that focuses the chart on one ticket over the span of its work. */
export function focusSearch(ticket: TicketTimelineTicket, now: number): string {
  const span = focusSpan(ticket, now);
  return new URLSearchParams({
    ticket: ticket.url,
    start: new Date(span.start).toISOString(),
    end: new Date(span.end).toISOString(),
  }).toString();
}

export function TicketTimelinePage() {
  const { t, i18n } = useTranslation();
  const [params] = useSearchParams();
  const [day, setDay] = useState(() => startOfLocalDay(new Date()));
  // A ticket's name focuses the chart on that ticket, over the span of its work.
  const focus = params.get("ticket");
  const focusStart = Date.parse(params.get("start") ?? "");
  const focusEnd = Date.parse(params.get("end") ?? "");
  const focused = Boolean(focus) && focusStart < focusEnd;
  const range = useMemo(
    () =>
      focused
        ? { start: new Date(focusStart), end: new Date(focusEnd) }
        : { start: day, end: addDays(day, 1) },
    [focused, focusStart, focusEnd, day],
  );
  const timeline = useQuery({
    queryKey: ["ticket-timeline", range.start.toISOString(), range.end.toISOString()],
    queryFn: () =>
      getTicketTimeline({ start: range.start.toISOString(), end: range.end.toISOString() }),
    // A past span no longer changes; only one that reaches now keeps up.
    refetchInterval: () => (range.end.getTime() > Date.now() ? 30000 : false),
  });
  const locale = i18n.resolvedLanguage === "ja" ? "ja-JP" : "en-US";
  const span = { start: range.start.getTime(), end: range.end.getTime() };
  const ticket = focused ? timeline.data?.tickets.find((item) => item.url === focus) : undefined;
  return (
    <Stack className="activity-page" gap="lg">
      <Title order={2}>{t("timeline.title")}</Title>
      <Card className="timeline-card" withBorder radius="md" p="lg">
        {focused ? (
          <div className="activity-header">
            <Link to="?" className="timeline-back">
              <ChevronLeft size={16} aria-hidden />
              {t("timeline.focus.back")}
            </Link>
            {ticket ? (
              <span className="timeline-focus-title">
                <ItemLink url={ticket.url} label={ticket.label} title={ticket.title} />
              </span>
            ) : null}
            <Text size="sm" c="dimmed">
              {t("timeline.focus.period", {
                start: clock(span.start, 0),
                end: clock(span.end, 0),
              })}
            </Text>
          </div>
        ) : (
          <div className="activity-header">
            <DateNav
              label={day.toLocaleDateString(locale, {
                year: "numeric",
                month: "short",
                day: "numeric",
                weekday: "short",
              })}
              value={day}
              onShift={(direction) => setDay((current) => addDays(current, direction))}
              onSelect={(next) => setDay(startOfLocalDay(next))}
            />
            {timeline.data?.summary ? (
              <dl className="timeline-summary">
                {(
                  [
                    ["moved", timeline.data.tickets.length],
                    ["closed", timeline.data.summary.closed_tickets],
                    ["merged", timeline.data.summary.merged_pulls],
                  ] as const
                ).map(([key, count]) => (
                  <div key={key}>
                    <dt>{t(`timeline.summary.${key}`)}</dt>
                    <dd>{count}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </div>
        )}
        {timeline.error ? (
          <Alert color="danger" title={t("timeline.loadError")}>
            {timeline.error.message}
          </Alert>
        ) : timeline.data ? (
          <TimelineChart
            key={`${range.start.toISOString()}/${focus ?? ""}`}
            data={timeline.data}
            day={span}
            now={timeline.dataUpdatedAt}
            focus={focused ? focus : null}
          />
        ) : (
          <Group>
            <Loader size="sm" />
            <Text size="sm">{t("timeline.loading")}</Text>
          </Group>
        )}
      </Card>
    </Stack>
  );
}

/**
 * The local time of day, in the 24-hour form of the chart's ticks, with the
 * date when it is not on the day of `day`.
 */
function clock(time: number, day = time): string {
  const date = new Date(time);
  const hhmm = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  return date.toDateString() === new Date(day).toDateString()
    ? hhmm
    : `${date.getMonth() + 1}/${date.getDate()} ${hhmm}`;
}

export function durationText(t: TFunction, ms: number): string {
  const total = Math.floor(ms / 60000);
  const hours = Math.floor(total / 60);
  const minutes = total % 60;
  return hours > 0
    ? t("timeline.duration.hours", { hours, minutes })
    : t("timeline.duration.minutes", { minutes });
}

function outcomeText(t: TFunction, outcome: string | null): string {
  return outcome ? t(`timeline.outcome.${outcome}`, { defaultValue: outcome }) : "";
}

export function barText(t: TFunction, bar: TimedBar, names: Names, day = bar.start): string {
  const what =
    bar.kind === "job"
      ? t("timeline.bar.job", { job: bar.job })
      : t(`timeline.bar.${bar.kind}`, { name: names.get(bar.person_id) ?? bar.person_id });
  const details = [
    t("timeline.bar.time", { start: clock(bar.start, day), end: clock(bar.end, day) }),
  ];
  if (bar.runner) details.unshift(bar.runner);
  if (bar.on) details.unshift(bar.on);
  if (bar.queued !== undefined && bar.queued < bar.start)
    details.push(t("timeline.bar.queued", { time: clock(bar.queued) }));
  if (bar.running) details.push(t("timeline.bar.running"));
  else if (bar.outcome)
    details.push(t("timeline.bar.ended", { outcome: outcomeText(t, bar.outcome) }));
  return t("timeline.bar.detail", { what, details: details.join(t("timeline.separator")) });
}

/** The sentences that spell out a gap: its length and time, and what bounded it. */
export function gapLines(
  t: TFunction,
  gap: Gap,
  ticket: TicketTimelineTicket,
  names: Names,
): string[] {
  const duration = durationText(t, gap.end - gap.start);
  const lines = [
    gap.continuing
      ? t("timeline.gap.continuing", { duration, start: clock(gap.start) })
      : t("timeline.gap.label", { duration, start: clock(gap.start), end: clock(gap.end) }),
  ];
  const before = gap.before
    ? barText(t, gap.before, names, gap.start)
    : gap.fromStart
      ? t("timeline.gap.started", { time: clock(gap.start) })
      : null;
  if (before) lines.push(t("timeline.gap.before", { what: before }));
  if (gap.after) lines.push(t("timeline.gap.after", { what: barText(t, gap.after, names) }));
  const meanwhile = ticket.events
    .filter((event) => {
      const at = Date.parse(event.at);
      return at >= gap.start && at <= gap.end;
    })
    .map((event) => {
      const name = names.get(event.person_id) ?? event.person_id;
      const verdict = event.verdict
        ? t(`timeline.verdict.${event.verdict}`, { defaultValue: event.verdict })
        : "";
      return `${clock(Date.parse(event.at))} ${t(`timeline.events.${event.kind}`, { name, verdict })}`;
    });
  if (meanwhile.length)
    lines.push(t("timeline.gap.between", { items: meanwhile.join(t("timeline.separator")) }));
  return lines;
}

function percent(time: number, day: Span): number {
  const ratio = (time - day.start) / (day.end - day.start);
  return Math.min(100, Math.max(0, ratio * 100));
}

function spanStyle(span: Span, day: Span): CSSProperties {
  const left = percent(span.start, day);
  return { left: `${left}%`, width: `${Math.max(0.2, percent(span.end, day) - left)}%` };
}

function TimelineChart({
  data,
  day,
  now,
  focus,
}: {
  data: TicketTimelineResponse;
  /** The span of the day or of the focused ticket's work. */
  day: Span;
  now: number;
  /** The URL of the one ticket shown, opened down to its jobs; none shows every ticket. */
  focus: string | null;
}) {
  const { t } = useTranslation();
  const names: Names = useMemo(
    () => new Map(data.members.map((member) => [member.person_id, member.name])),
    [data.members],
  );
  const colors = useMemo(
    () =>
      new Map(
        data.members.map((member, index) => [
          member.person_id,
          MEMBER_COLORS[index % MEMBER_COLORS.length],
        ]),
      ),
    [data.members],
  );
  const tickets = useMemo(
    () =>
      data.tickets
        .filter((ticket) => !focus || ticket.url === focus)
        .map((ticket) => ({ ticket, ...ticketGaps(ticket, day, now) })),
    [data.tickets, day, now, focus],
  );
  const scale = ticks(day);
  // Focused, the ticket is open down to its jobs; the day's tickets start closed.
  const [open, setOpen] = useState<Set<string>>(
    () =>
      new Set(
        focus
          ? tickets.flatMap(({ ticket }) => [
              ticket.url,
              ...ticket.pulls.flatMap((pull) => [pull.url, `${pull.url}:ci`]),
            ])
          : [],
      ),
  );
  const [selected, setSelected] = useState<Selection | null>(null);
  const [hovered, setHovered] = useState<Selection | null>(null);
  const chart = useRef<HTMLDivElement>(null);
  const active = hovered ?? selected;
  const activeGap = active ? tickets[active.ticket]?.gaps[active.gap] : undefined;

  const toggle = (key: string, name: string): Row["toggle"] => ({
    open: open.has(key),
    name,
    onToggle: () =>
      setOpen((current) => {
        const next = new Set(current);
        if (!next.delete(key)) next.add(key);
        return next;
      }),
  });
  const rows: Row[] = [];
  tickets.forEach(({ ticket, bars }, index) => {
    const key = ticket.url;
    const name = `${ticket.label} ${ticket.title}`;
    const merges = ticket.pulls.flatMap((pull) => {
      const at = Date.parse(pull.merged_at ?? "");
      return Number.isNaN(at)
        ? []
        : [
            {
              at,
              url: pull.url,
              label: t("timeline.milestone.merged", { pull: pull.label, time: clock(at) }),
            },
          ];
    });
    rows.push({
      key,
      ticket: index,
      depth: 0,
      label: (
        <>
          <ModeIcons t={t} ticket={ticket} day={day} />
          {focus ? (
            <>
              <strong>{ticket.label}</strong> {ticket.title}
            </>
          ) : (
            <Link to={`?${focusSearch(ticket, now)}`} title={t("timeline.focus.select", { name })}>
              <strong>{ticket.label}</strong> {ticket.title}
            </Link>
          )}
        </>
      ),
      bars,
      toggle: toggle(key, name),
      summary: true,
      milestones: merges,
    });
    if (!open.has(key)) return;
    const work = sessionBars(ticket.work);
    if (work.some((bar) => shows(bar, day)))
      rows.push({
        key: `${key}:work`,
        ticket: index,
        depth: 1,
        label: t("timeline.rows.work"),
        bars: work,
      });
    for (const pull of ticket.pulls) {
      const work = sessionBars(pull.work);
      const reviews = sessionBars(pull.reviews, true);
      const attempts = pull.jobs.flatMap((job) => job.attempts);
      const jobs = jobBars(attempts, now);
      rows.push({
        key: pull.url,
        ticket: index,
        depth: 1,
        label: <ItemLink url={pull.url} label={pull.label} title={pull.title} />,
        bars: [...work, ...reviews, ...jobs],
        toggle: toggle(pull.url, `${pull.label} ${pull.title}`),
        summary: true,
        milestones: merges.filter((merge) => merge.url === pull.url),
      });
      if (!open.has(pull.url)) continue;
      const modes = new Set(pull.work.map((bar) => bar.mode));
      const workLabel =
        modes.size === 1 ? t(`timeline.rows.${[...modes][0]}`) : t("timeline.rows.work");
      rows.push(
        { key: `${pull.url}:work`, ticket: index, depth: 2, label: workLabel, bars: work },
        {
          key: `${pull.url}:reviews`,
          ticket: index,
          depth: 2,
          label: t("timeline.rows.reviews"),
          bars: reviews,
        },
        {
          key: `${pull.url}:ci`,
          ticket: index,
          depth: 2,
          label: t("timeline.rows.ci"),
          bars: jobs,
          summary: true,
          toggle: pull.jobs.length
            ? toggle(`${pull.url}:ci`, `${pull.label} ${t("timeline.rows.ci")}`)
            : undefined,
        },
      );
      if (!open.has(`${pull.url}:ci`)) continue;
      // The jobs that ended the CI explain its wait; the rest fold into one row.
      const ending = endingJobs(pull.jobs, now);
      const jobRow = (job: (typeof pull.jobs)[number], depth: number): Row => ({
        key: `${pull.url}:ci:${job.name}`,
        ticket: index,
        depth,
        label: job.name,
        bars: jobBars(job.attempts, now),
      });
      for (const job of pull.jobs.filter((job) => ending.has(job.name))) rows.push(jobRow(job, 3));
      const rest = pull.jobs.filter((job) => !ending.has(job.name));
      if (!rest.length) continue;
      const restKey = `${pull.url}:ci:others`;
      const restLabel = t("timeline.rows.otherJobs", { count: rest.length });
      rows.push({
        key: restKey,
        ticket: index,
        depth: 3,
        label: restLabel,
        bars: jobBars(
          rest.flatMap((job) => job.attempts),
          now,
        ),
        summary: true,
        toggle: toggle(restKey, `${pull.label} ${restLabel}`),
      });
      if (open.has(restKey)) rows.push(...rest.map((job) => jobRow(job, 4)));
    }
  });

  if (focus && tickets.length) rows.push(...memberRows(t, data, tickets[0].ticket, day));

  /** The gap of `gaps` under the pointer: the gap button it is on, or the one at its time. */
  const gapUnder = (event: MouseEvent<HTMLDivElement>, gaps: Gap[]) => {
    const own = (event.target as HTMLElement).dataset.gap;
    if (own) return Number(own.split(":")[1]);
    const box = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - box.left) / Math.max(1, box.width);
    return gapAt(gaps, day.start + ratio * (day.end - day.start));
  };
  if (!tickets.length) return <Text c="dimmed">{t("timeline.empty")}</Text>;
  return (
    <>
      <div className="timeline-chart" ref={chart}>
        <div className="timeline-row timeline-row-head">
          <div className="timeline-label">{t("timeline.ticketColumn")}</div>
          <div className="timeline-track timeline-track-head">
            {scale.times.map((time) => (
              <span
                key={time}
                className="activity-time-tick"
                style={{ left: `${percent(time, day)}%` }}
              >
                {tickLabel(time, scale.step)}
              </span>
            ))}
          </div>
        </div>
        {rows.map((row) => {
          const gaps = tickets[row.ticket]?.gaps ?? [];
          return (
            <div
              key={row.key}
              className={row.depth === 0 ? "timeline-row timeline-row-ticket" : "timeline-row"}
            >
              <div className="timeline-label" style={{ paddingLeft: 8 + row.depth * 16 }}>
                {row.toggle ? (
                  <button
                    type="button"
                    className="timeline-toggle"
                    aria-expanded={row.toggle.open}
                    aria-label={t(row.toggle.open ? "timeline.collapse" : "timeline.expand", {
                      name: row.toggle.name,
                    })}
                    onClick={row.toggle.onToggle}
                  >
                    {row.toggle.open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                  </button>
                ) : (
                  <span className="timeline-toggle-space" />
                )}
                <span className="timeline-label-text">{row.label}</span>
              </div>
              <div
                className="timeline-track"
                data-testid={`track-${row.key}`}
                onMouseMove={(event) => {
                  const gap = gapUnder(event, gaps);
                  setHovered(gap < 0 ? null : { ticket: row.ticket, gap });
                }}
                onMouseLeave={() => setHovered(null)}
                onClick={(event) => {
                  const gap = gapUnder(event, gaps);
                  if (gap >= 0) setSelected({ ticket: row.ticket, gap });
                }}
              >
                {scale.times.map((time) => (
                  <span
                    key={time}
                    className="timeline-grid"
                    style={{ left: `${percent(time, day)}%` }}
                  />
                ))}
                {activeGap ? (
                  <div
                    className={
                      active?.ticket === row.ticket
                        ? "timeline-gap-band timeline-gap-band-strong"
                        : "timeline-gap-band"
                    }
                    style={spanStyle(activeGap, day)}
                  />
                ) : null}
                {row.depth === 0
                  ? gaps.map((gap, index) => (
                      <button
                        type="button"
                        key={gap.start}
                        className="timeline-gap"
                        data-gap={`${row.ticket}:${index}`}
                        style={spanStyle(gap, day)}
                        aria-pressed={selected?.ticket === row.ticket && selected.gap === index}
                        aria-label={gapLines(t, gap, tickets[row.ticket].ticket, names).join(" ")}
                        onFocus={() => {
                          // Tab moves the selection away from a resting pointer.
                          setHovered(null);
                          setSelected({ ticket: row.ticket, gap: index });
                        }}
                        onKeyDown={(event) => {
                          if (event.key !== "Escape") return;
                          setSelected(null);
                          event.currentTarget.blur();
                        }}
                      />
                    ))
                  : null}
                {row.milestones
                  ?.filter((milestone) => shows({ start: milestone.at, end: milestone.at }, day))
                  .map((milestone) => (
                    <span
                      key={milestone.at}
                      className="timeline-milestone"
                      role="img"
                      aria-label={milestone.label}
                      title={milestone.label}
                      style={{ left: `${percent(milestone.at, day)}%` }}
                    />
                  ))}
                {/* Only what the span shows: earlier work stays for the gaps' words. */}
                {row.bars
                  .filter((bar) => shows(bar, day))
                  .map((bar) => {
                    const label = barText(t, bar, names, day.start);
                    const color = colors.get(bar.person_id);
                    return (
                      <div
                        key={`${bar.kind}:${bar.start}:${bar.person_id}:${bar.job ?? ""}`}
                        className={
                          bar.kind === "job" ? "timeline-bar timeline-bar-job" : "timeline-bar"
                        }
                        style={{
                          ...spanStyle(bar, day),
                          ...(color ? { background: `var(--mantine-color-${color}-filled)` } : {}),
                        }}
                        title={label}
                        aria-label={label}
                        role="img"
                      >
                        {row.summary ? null : (
                          <span>
                            {bar.kind === "job"
                              ? (bar.runner ?? bar.job)
                              : (names.get(bar.person_id) ?? bar.person_id)}
                          </span>
                        )}
                      </div>
                    );
                  })}
              </div>
            </div>
          );
        })}
      </div>
      <div className="timeline-gap-detail" role="status" aria-label={t("timeline.gap.region")}>
        {activeGap && active
          ? gapLines(t, activeGap, tickets[active.ticket].ticket, names).map((line, index) => (
              <Text key={index} size="sm" fw={index === 0 ? 700 : undefined}>
                {index === 0 ? `${tickets[active.ticket].ticket.label} ${line}` : line}
              </Text>
            ))
          : null}
      </div>
    </>
  );
}

/**
 * What the focused ticket's members ran elsewhere at the time, one row each:
 * a review that waited shows its reviewer's run on another ticket.
 */
export function memberRows(
  t: TFunction,
  data: TicketTimelineResponse,
  ticket: TicketTimelineTicket,
  day: Span,
): Row[] {
  const own = sessionBars(
    [ticket.work, ...ticket.pulls.flatMap((pull) => [pull.work, pull.reviews])].flat(),
  ).filter((bar) => shows(bar, day));
  const people = [...new Set(own.map((bar) => bar.person_id).filter(Boolean))];
  if (!people.length) return [];
  // Only runs: a member keeps working in a conversation alongside other
  // work, so one elsewhere does not mean it was busy.
  const runs = (bars: TicketTimelineBar[]) => bars.filter((bar) => bar.mode === "workflow");
  const elsewhere = data.tickets
    .filter((other) => other.url !== ticket.url)
    .flatMap((other) =>
      [
        ...sessionBars(runs(other.work)),
        ...other.pulls.flatMap((pull) => [
          ...sessionBars(runs(pull.work)),
          ...sessionBars(runs(pull.reviews), true),
        ]),
      ].map((bar) => ({ ...bar, on: other.label })),
    );
  const names = new Map(data.members.map((member) => [member.person_id, member.name]));
  return [
    { key: "members", ticket: -1, depth: 0, label: t("timeline.rows.others"), bars: [] },
    ...people.map((person) => ({
      key: `members:${person}`,
      ticket: -1,
      depth: 1,
      label: t("timeline.rows.elsewhere", { name: names.get(person) ?? person }),
      bars: elsewhere.filter((bar) => bar.person_id === person),
    })),
  ];
}

function jobBars(attempts: TicketTimelineAttempt[], now: number): TimedBar[] {
  return attempts
    .map((attempt) => attemptBar(attempt, now))
    .filter((bar): bar is TimedBar => bar !== null);
}

/**
 * How the ticket's own work went on in `day`: by workflow runs, in
 * conversations, or both. Reviews are always runs, so they do not count.
 */
export function workModes(ticket: TicketTimelineTicket, day: Span): TicketTimelineBar["mode"][] {
  const own = sessionBars([ticket.work, ...ticket.pulls.map((pull) => pull.work)].flat()).filter(
    (bar) => shows(bar, day),
  );
  return (["workflow", "interactive"] as const).filter((mode) =>
    own.some((bar) => bar.kind === mode),
  );
}

function ModeIcons({ t, ticket, day }: { t: TFunction; ticket: TicketTimelineTicket; day: Span }) {
  return workModes(ticket, day).map((mode) => {
    const label = t(`timeline.mode.${mode}`);
    const Icon = mode === "interactive" ? MessageSquare : Bot;
    return (
      <span
        key={mode}
        role="img"
        aria-label={label}
        title={label}
        className={`timeline-mode timeline-mode-${mode}`}
      >
        <Icon size={14} aria-hidden />
      </span>
    );
  });
}

function ItemLink({ url, label, title }: { url: string; label: string; title: string }) {
  const path = inAppPath(url);
  const text = (
    <>
      <strong>{label}</strong> {title}
    </>
  );
  return path ? (
    <WorkspaceLink to={path} title={`${label} ${title}`}>
      {text}
    </WorkspaceLink>
  ) : (
    <a href={url} title={`${label} ${title}`}>
      {text}
    </a>
  );
}
