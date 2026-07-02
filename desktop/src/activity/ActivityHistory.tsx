import {
  Alert,
  Avatar,
  Badge,
  Card,
  Group,
  HoverCard,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
  Title,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import {
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock3,
  ExternalLink,
  GitBranch,
  GitMerge,
  GitPullRequest,
  Search,
  Upload,
  Users,
} from "lucide-react";

import {
  type ActivityHistoryEvent,
  type ActivityHistoryLink,
  type ActivityHistoryMember,
  type ActivityHistoryResponse,
  type ActivityHistorySession,
  getActivityHistory,
  memberAvatarUrl,
} from "../api/client";

export type ActivityView = "day" | "week";

const HOURS = ["03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"];
const ACTIVITY_LIMIT = 1000;
const ACTIVITY_BLOCK_MINUTES = 60;

export type ActivityHistoryMatchState = {
  active: boolean;
  sessionIds: Set<string>;
  eventIds: Set<string>;
};

type ActivityBlock = {
  id: string;
  person_id: string;
  mode: ActivityHistorySession["mode"];
  title: string;
  started_at: string;
  ended_at: string;
  display_started_at: string;
  display_ended_at: string;
  sessions: ActivityHistorySession[];
  links: ActivityHistoryLink[];
};

export function ActivityHistoryPage() {
  const { t, i18n } = useTranslation();
  const [view, setView] = useState<ActivityView>("day");
  const [baseDate, setBaseDate] = useState(() => startOfLocalDay(new Date()));
  const [query, setQuery] = useState("");
  const range = useMemo(() => activityRange(baseDate, view), [baseDate, view]);
  const history = useQuery({
    queryKey: ["activity-history", range.start.toISOString(), range.end.toISOString()],
    queryFn: () =>
      getActivityHistory({
        start: range.start.toISOString(),
        end: range.end.toISOString(),
        limit: ACTIVITY_LIMIT,
      }),
    refetchInterval: 5000,
  });
  const data = history.data ?? emptyActivityHistory(range);
  const matches = useMemo(() => matchActivityHistory(data, query), [data, query]);
  const now = new Date();
  const showNowLine = now >= range.start && now < range.end;
  const locale = i18n.resolvedLanguage === "ja" ? "ja-JP" : "en-US";

  const shiftRange = (direction: -1 | 1) => {
    setBaseDate((current) => addDays(current, direction * (view === "day" ? 1 : 7)));
  };
  const selectDate = (value: string) => {
    if (!value) {
      return;
    }
    const [year, month, day] = value.split("-").map(Number);
    setBaseDate(startOfLocalDay(new Date(year, month - 1, day)));
  };

  return (
    <Stack className="activity-page" gap="lg">
      <Group justify="space-between" align="flex-start">
        <div>
          <Title order={2}>{t("activity.title")}</Title>
          <Text c="dimmed" size="sm">
            {t("activity.subtitle")}
          </Text>
        </div>
      </Group>
      <Card className="activity-card" withBorder radius="md" p="lg">
        <div className="activity-header">
          <TextInput
            className="activity-search"
            aria-label={t("activity.search")}
            placeholder={t("activity.searchPlaceholder")}
            leftSection={<Search size={15} />}
            value={query}
            onChange={(event) => setQuery(event.currentTarget.value)}
          />
          <div className="activity-date-nav">
            <button
              type="button"
              className="activity-icon-button"
              aria-label={t("activity.previous")}
              onClick={() => shiftRange(-1)}
            >
              <ChevronLeft size={16} />
            </button>
            <label className="activity-date-label">
              <span>{formatRangeLabel(range.start, range.end, view, locale)}</span>
              <input
                aria-label={t("activity.date")}
                type="date"
                value={toDateInputValue(baseDate)}
                onChange={(event) => selectDate(event.currentTarget.value)}
              />
            </label>
            <button
              type="button"
              className="activity-icon-button"
              aria-label={t("activity.next")}
              onClick={() => shiftRange(1)}
            >
              <ChevronRight size={16} />
            </button>
          </div>
          <SegmentedControl
            value={view}
            onChange={(value) => setView(value as ActivityView)}
            data={[
              { value: "day", label: t("activity.day") },
              { value: "week", label: t("activity.week") },
            ]}
          />
        </div>
        {history.error ? (
          <Alert color="red" title={t("activity.loadError")}>
            {history.error.message}
          </Alert>
        ) : null}
        <ActivityChart
          data={data}
          matches={matches}
          range={range}
          view={view}
          now={showNowLine ? now : null}
          loading={history.isLoading}
          locale={locale}
        />
      </Card>
    </Stack>
  );
}

function ActivityChart({
  data,
  range,
  view,
  now,
  loading,
  locale,
  matches,
}: {
  data: ActivityHistoryResponse;
  range: { start: Date; end: Date };
  view: ActivityView;
  now: Date | null;
  loading: boolean;
  locale: string;
  matches: ActivityHistoryMatchState;
}) {
  const { t } = useTranslation();
  const sessionsByMember = groupBy(data.sessions, (session) => session.person_id);
  const eventsByMember = groupBy(
    data.events.filter((event) => event.person_id),
    (event) => event.person_id,
  );
  const sharedEvents = data.events.filter((event) => !event.person_id);
  const hasRows = data.sessions.length > 0 || data.events.length > 0;
  return (
    <div className="activity-chart">
      <div className="activity-row activity-row-head">
        <div className="activity-member-cell">{t("activity.memberColumn")}</div>
        <div
          className={
            view === "day"
              ? "activity-timeline-head activity-timeline-head-day"
              : "activity-timeline-head activity-timeline-head-week"
          }
        >
          {view === "day"
            ? HOURS.map((hour) => (
                <span
                  className="activity-time-tick"
                  key={hour}
                  style={{ left: `${(Number(hour.slice(0, 2)) * 100) / 24}%` }}
                >
                  {hour}
                </span>
              ))
            : Array.from({ length: 7 }).map((_, index) => (
                <span key={index}>{formatWeekday(addDays(range.start, index), locale)}</span>
              ))}
        </div>
      </div>
      <ActivityTimelineRow
        label={t("activity.events")}
        role=""
        avatar={<Users size={15} />}
        sessions={[]}
        events={sharedEvents}
        range={range}
        view={view}
        now={now}
        matches={matches}
        team
      />
      {data.members.map((member) => (
        <ActivityTimelineRow
          key={member.person_id}
          label={member.name || member.person_id}
          role={member.roles.join(", ") || member.person_id}
          avatar={member.person_id.substring(0, 2).toUpperCase()}
          sessions={sessionsByMember.get(member.person_id) ?? []}
          events={eventsByMember.get(member.person_id) ?? []}
          range={range}
          view={view}
          now={now}
          matches={matches}
          member={member}
        />
      ))}
      {!loading && !hasRows ? <div className="activity-empty">{t("activity.empty")}</div> : null}
      {loading ? <div className="activity-empty">{t("activity.loading")}</div> : null}
    </div>
  );
}

function ActivityTimelineRow({
  label,
  role,
  avatar,
  sessions,
  events,
  range,
  view,
  now,
  matches,
  member,
  team = false,
}: {
  label: string;
  role: string;
  avatar: string | ReactNode;
  sessions: ActivityHistorySession[];
  events: ActivityHistoryEvent[];
  range: { start: Date; end: Date };
  view: ActivityView;
  now: Date | null;
  matches: ActivityHistoryMatchState;
  member?: ActivityHistoryMember;
  team?: boolean;
}) {
  const blocks = buildActivityBlocks(sessions);
  const weekSessionCount = view === "week" ? maxWeekSessionCount(sessions, range) : 0;
  const rowMinHeight = team ? 38 : view === "week" ? Math.max(86, weekSessionCount * 30 + 44) : 86;
  const eventTop = team ? 10 : view === "week" ? Math.max(48, weekSessionCount * 30 + 16) : 48;
  const visibleEvents = view === "week" && !team ? [] : events;
  return (
    <div className={team ? "activity-row activity-row-events" : "activity-row"}>
      <div className="activity-member-cell">
        <Avatar
          src={member ? memberAvatarUrl(member.person_id) : undefined}
          size={32}
          radius="xl"
          className={team ? "activity-team-avatar" : undefined}
        >
          {avatar}
        </Avatar>
        <div className="activity-member-info">
          <span className="activity-member-name">{label}</span>
          {role ? <span className="activity-member-role">{role}</span> : null}
        </div>
      </div>
      <div className="activity-timeline-cell" style={{ minHeight: rowMinHeight }}>
        <TimelineGrid view={view} range={range} />
        {view === "week" ? (
          <WeekSessionColumns sessions={sessions} range={range} matches={matches} />
        ) : (
          blocks.map((block) => (
            <ActivityBlockBar
              key={block.id}
              block={block}
              range={range}
              view={view}
              matched={block.sessions.some((session) => matches.sessionIds.has(session.trace_id))}
              searchActive={matches.active}
            />
          ))
        )}
        {visibleEvents.map((event) => (
          <EventPin
            key={event.id}
            event={event}
            range={range}
            matched={matches.eventIds.has(event.id)}
            searchActive={matches.active}
            top={eventTop}
          />
        ))}
        {now ? (
          <div className="activity-now-line" style={{ left: `${positionInRange(now, range)}%` }} />
        ) : null}
      </div>
    </div>
  );
}

function TimelineGrid({ view, range }: { view: ActivityView; range: { start: Date; end: Date } }) {
  const columns = view === "day" ? 24 : 7;
  const today = startOfLocalDay(new Date()).getTime();
  return (
    <div
      className={
        view === "day" ? "activity-grid activity-grid-day" : "activity-grid activity-grid-week"
      }
    >
      {Array.from({ length: columns }).map((_, index) => {
        const day = view === "week" ? addDays(range.start, index).getTime() : 0;
        return (
          <span
            className={view === "week" && day === today ? "activity-grid-today" : ""}
            key={index}
          />
        );
      })}
    </div>
  );
}

function WeekSessionColumns({
  sessions,
  range,
  matches,
}: {
  sessions: ActivityHistorySession[];
  range: { start: Date; end: Date };
  matches: ActivityHistoryMatchState;
}) {
  const today = startOfLocalDay(new Date()).getTime();
  const columns = weekActivityBlockSegments(sessions, range);
  return (
    <div className="activity-week-columns">
      {Array.from({ length: 7 }).map((_, index) => {
        const day = addDays(range.start, index).getTime();
        return (
          <div
            className={
              day === today ? "activity-week-day activity-week-today" : "activity-week-day"
            }
            key={index}
          >
            {columns[index].map((block) => (
              <ActivityBlockBar
                key={`${block.id}:${index}`}
                block={block}
                range={{ start: addDays(range.start, index), end: addDays(range.start, index + 1) }}
                view="week"
                matched={block.sessions.some((session) => matches.sessionIds.has(session.trace_id))}
                searchActive={matches.active}
              />
            ))}
          </div>
        );
      })}
    </div>
  );
}

function ActivityBlockBar({
  block,
  range,
  view,
  matched,
  searchActive,
}: {
  block: ActivityBlock;
  range: { start: Date; end: Date };
  view: ActivityView;
  matched: boolean;
  searchActive: boolean;
}) {
  const start = new Date(block.display_started_at);
  const end = new Date(block.display_ended_at);
  const left = positionInRange(start, range);
  const right = positionInRange(end, range);
  const width = Math.max(0, right - left);
  const stateClass = searchStateClass(searchActive, matched);
  return (
    <HoverCard openDelay={150} closeDelay={80} withinPortal>
      <HoverCard.Target>
        <div
          aria-label={block.title}
          className={`${stateClass} ${
            block.mode === "interactive"
              ? "activity-session activity-session-interactive"
              : "activity-session activity-session-workflow"
          } ${view === "week" ? "activity-session-week" : ""}`}
          role="button"
          tabIndex={0}
          style={{
            ...(view === "day" ? { left: `${left}%`, top: "10px", width: `${width}%` } : {}),
          }}
        >
          <span>{block.title}</span>
        </div>
      </HoverCard.Target>
      <HoverCard.Dropdown className="activity-hover-card">
        <ActivityBlockDetail block={block} />
      </HoverCard.Dropdown>
    </HoverCard>
  );
}

function EventPin({
  event,
  range,
  matched,
  searchActive,
  top,
}: {
  event: ActivityHistoryEvent;
  range: { start: Date; end: Date };
  matched: boolean;
  searchActive: boolean;
  top: number;
}) {
  return (
    <HoverCard openDelay={120} closeDelay={80} withinPortal>
      <HoverCard.Target>
        <div
          aria-label={event.title}
          className={`activity-event-pin activity-event-${event.type} ${searchStateClass(searchActive, matched)}`}
          role="button"
          tabIndex={0}
          style={{ left: `${positionInRange(new Date(event.timestamp), range)}%`, top }}
        >
          {eventIcon(event.type)}
        </div>
      </HoverCard.Target>
      <HoverCard.Dropdown className="activity-hover-card">
        <ActivityEventDetail event={event} />
      </HoverCard.Dropdown>
    </HoverCard>
  );
}

function ActivityBlockDetail({ block }: { block: ActivityBlock }) {
  const { t } = useTranslation();
  return (
    <Stack gap="xs">
      <Group gap="xs" justify="space-between">
        <Text fw={700} size="sm" lineClamp={2}>
          {block.title}
        </Text>
        <Badge color={block.mode === "interactive" ? "teal" : "blue"} variant="light">
          {t(`activity.modes.${block.mode}`)}
        </Badge>
      </Group>
      <Text c="dimmed" size="xs">
        <Clock3 size={12} /> {formatTimeRange(block.started_at, block.ended_at)} ·{" "}
        {formatDuration(blockDurationSeconds(block))}
      </Text>
      {block.sessions.length > 1 ? (
        <div className="activity-block-session-list">
          {block.sessions.map((session) => (
            <div key={session.trace_id}>
              <Text fw={600} lineClamp={1} size="xs">
                {session.title}
              </Text>
              <Text c="dimmed" size="xs">
                {formatTimeRange(session.started_at, session.ended_at)}
              </Text>
            </div>
          ))}
        </div>
      ) : null}
      <LinkList links={block.links} />
    </Stack>
  );
}

function ActivityEventDetail({ event }: { event: ActivityHistoryEvent }) {
  const { t } = useTranslation();
  return (
    <Stack gap="xs">
      <Group gap="xs">
        <Badge color={eventColor(event.type)} variant="light">
          {t(`activity.eventTypes.${event.type}`)}
        </Badge>
        <Text c="dimmed" size="xs">
          {formatTime(event.timestamp)}
        </Text>
      </Group>
      <Text fw={700} size="sm">
        {event.title}
      </Text>
      {event.detail ? (
        <Text c="dimmed" size="xs">
          {event.detail}
        </Text>
      ) : null}
      <LinkList
        links={
          event.links.length > 0
            ? event.links
            : event.url
              ? [{ kind: "external", label: event.url, url: event.url }]
              : []
        }
      />
    </Stack>
  );
}

function LinkList({ links }: { links: ActivityHistoryLink[] }) {
  const { t } = useTranslation();
  if (links.length === 0) {
    return (
      <Text c="dimmed" fs="italic" size="xs">
        {t("activity.noLinks")}
      </Text>
    );
  }
  return (
    <div className="activity-link-list">
      {links.map((link) => {
        const href = activityLinkHref(link);
        return href ? (
          <a href={href} key={activityLinkKey(link)} target="_blank" rel="noreferrer">
            <ExternalLink size={12} />
            <span>{link.label}</span>
          </a>
        ) : (
          <span className="activity-link-item" key={activityLinkKey(link)}>
            <ExternalLink size={12} />
            <span>{link.label}</span>
          </span>
        );
      })}
    </div>
  );
}

export function activityLinkHref(link: ActivityHistoryLink): string | null {
  return link.url || null;
}

export function activityLinkKey(link: ActivityHistoryLink): string {
  return `${link.kind}:${link.label}:${link.url}`;
}

export function activityRange(baseDate: Date, view: ActivityView): { start: Date; end: Date } {
  const start = view === "day" ? startOfLocalDay(baseDate) : startOfWeek(baseDate);
  return { start, end: addDays(start, view === "day" ? 1 : 7) };
}

export function matchActivityHistory(
  data: ActivityHistoryResponse,
  rawQuery: string,
): ActivityHistoryMatchState {
  const query = rawQuery.trim().toLowerCase();
  const allSessions = new Set(data.sessions.map((session) => session.trace_id));
  const allEvents = new Set(data.events.map((event) => event.id));
  const members = new Map(data.members.map((member) => [member.person_id, member]));
  if (!query) {
    return { active: false, sessionIds: allSessions, eventIds: allEvents };
  }
  return {
    active: true,
    sessionIds: new Set(
      data.sessions
        .filter((session) => {
          const member = members.get(session.person_id);
          return searchableText(
            [
              session.title,
              session.command,
              session.workflow,
              session.person_id,
              member?.name ?? "",
              ...(member?.roles ?? []),
            ],
            session.links,
          ).includes(query);
        })
        .map((session) => session.trace_id),
    ),
    eventIds: new Set(
      data.events
        .filter((event) => {
          const member = members.get(event.person_id);
          return searchableText(
            [
              event.title,
              event.detail,
              event.url,
              event.person_id,
              member?.name ?? "",
              ...(member?.roles ?? []),
            ],
            event.links,
          ).includes(query);
        })
        .map((event) => event.id),
    ),
  };
}

export function buildActivityBlocks(sessions: ActivityHistorySession[]): ActivityBlock[] {
  const rounded = sessions
    .map((session) => ({
      session,
      displayStart: floorToInterval(new Date(session.started_at), ACTIVITY_BLOCK_MINUTES),
      displayEnd: ceilToInterval(new Date(session.ended_at), ACTIVITY_BLOCK_MINUTES),
    }))
    .map(({ session, displayStart, displayEnd }) => ({
      session,
      displayStart,
      displayEnd:
        displayEnd.getTime() > displayStart.getTime()
          ? displayEnd
          : addMinutes(displayStart, ACTIVITY_BLOCK_MINUTES),
    }))
    .sort(
      (left, right) =>
        left.displayStart.getTime() - right.displayStart.getTime() ||
        left.displayEnd.getTime() - right.displayEnd.getTime(),
    );

  const blocks: ActivityBlock[] = [];
  for (const item of rounded) {
    const last = blocks[blocks.length - 1];
    if (last && item.displayStart.getTime() < new Date(last.display_ended_at).getTime()) {
      mergeSessionIntoBlock(last, item.session, item.displayEnd);
      continue;
    }
    blocks.push(activityBlockFromSession(item.session, item.displayStart, item.displayEnd));
  }
  return blocks;
}

function activityBlockFromSession(
  session: ActivityHistorySession,
  displayStart: Date,
  displayEnd: Date,
): ActivityBlock {
  return {
    id: session.trace_id,
    person_id: session.person_id,
    mode: session.mode,
    title: session.title,
    started_at: session.started_at,
    ended_at: session.ended_at,
    display_started_at: displayStart.toISOString(),
    display_ended_at: displayEnd.toISOString(),
    sessions: [session],
    links: session.links,
  };
}

function mergeSessionIntoBlock(
  block: ActivityBlock,
  session: ActivityHistorySession,
  displayEnd: Date,
) {
  block.id = `${block.id}:${session.trace_id}`;
  block.sessions.push(session);
  block.mode = block.sessions.some((item) => item.mode === "interactive")
    ? "interactive"
    : "workflow";
  block.title = blockTitle(block.sessions);
  block.started_at = minIso(block.started_at, session.started_at);
  block.ended_at = maxIso(block.ended_at, session.ended_at);
  block.display_ended_at = maxIso(block.display_ended_at, displayEnd.toISOString());
  block.links = dedupeLinks(block.sessions.flatMap((item) => item.links));
}

function blockTitle(sessions: ActivityHistorySession[]): string {
  const [first] = sessions;
  return sessions.length === 1 ? first.title : `${first.title} +${sessions.length - 1}`;
}

function blockDurationSeconds(block: ActivityBlock): number {
  return Math.max(
    0,
    (new Date(block.ended_at).getTime() - new Date(block.started_at).getTime()) / 1000,
  );
}

function dedupeLinks(links: ActivityHistoryLink[]): ActivityHistoryLink[] {
  const seen = new Set<string>();
  const deduped: ActivityHistoryLink[] = [];
  for (const link of links) {
    const key = activityLinkKey(link);
    if (seen.has(key)) {
      continue;
    }
    seen.add(key);
    deduped.push(link);
  }
  return deduped;
}

function floorToInterval(date: Date, minutes: number): Date {
  const next = new Date(date);
  next.setSeconds(0, 0);
  next.setMinutes(Math.floor(next.getMinutes() / minutes) * minutes);
  return next;
}

function ceilToInterval(date: Date, minutes: number): Date {
  const floored = floorToInterval(date, minutes);
  return floored.getTime() === date.getTime() ? floored : addMinutes(floored, minutes);
}

function searchableText(parts: string[], links: ActivityHistoryLink[]): string {
  return [...parts, ...links.map((link) => `${link.kind} ${link.label} ${link.url}`)]
    .join(" ")
    .toLowerCase();
}

function emptyActivityHistory(range: { start: Date; end: Date }): ActivityHistoryResponse {
  return {
    start: range.start.toISOString(),
    end: range.end.toISOString(),
    members: [],
    sessions: [],
    events: [],
    unsupported_event_sources: [],
  };
}

function groupBy<T>(items: T[], key: (item: T) => string): Map<string, T[]> {
  const grouped = new Map<string, T[]>();
  for (const item of items) {
    const value = key(item);
    grouped.set(value, [...(grouped.get(value) ?? []), item]);
  }
  return grouped;
}

function startOfLocalDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

function startOfWeek(date: Date): Date {
  const start = startOfLocalDay(date);
  const offset = start.getDay() === 0 ? -6 : 1 - start.getDay();
  return addDays(start, offset);
}

function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function addMinutes(date: Date, minutes: number): Date {
  const next = new Date(date);
  next.setMinutes(next.getMinutes() + minutes);
  return next;
}

function toDateInputValue(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function formatRangeLabel(start: Date, end: Date, view: ActivityView, locale: string): string {
  if (view === "day") {
    return start.toLocaleDateString(locale, {
      year: "numeric",
      month: "short",
      day: "numeric",
      weekday: "short",
    });
  }
  const last = addDays(end, -1);
  return `${start.toLocaleDateString(locale, { month: "short", day: "numeric" })} - ${last.toLocaleDateString(locale, { month: "short", day: "numeric" })}`;
}

function formatWeekday(date: Date, locale: string): string {
  return date.toLocaleDateString(locale, { weekday: "short" });
}

function positionInRange(value: Date, range: { start: Date; end: Date }): number {
  const total = range.end.getTime() - range.start.getTime();
  const offset = value.getTime() - range.start.getTime();
  return Math.min(100, Math.max(0, (offset / total) * 100));
}

function formatTime(value: string): string {
  return new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function formatTimeRange(start: string, end: string): string {
  return `${formatTime(start)} - ${formatTime(end)}`;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) {
    return "<1m";
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

function eventIcon(type: ActivityHistoryEvent["type"]) {
  switch (type) {
    case "pr_create":
      return <GitPullRequest size={12} />;
    case "pr_merge":
      return <GitMerge size={12} />;
    case "pr_closed":
      return <GitPullRequest size={12} />;
    case "push":
      return <Upload size={12} />;
    case "issue_resolve":
      return <CheckCircle2 size={12} />;
    case "external":
      return <GitBranch size={12} />;
  }
}

function eventColor(type: ActivityHistoryEvent["type"]): string {
  return {
    pr_create: "grape",
    pr_merge: "violet",
    pr_closed: "red",
    push: "orange",
    issue_resolve: "green",
    external: "gray",
  }[type];
}

function searchStateClass(active: boolean, matched: boolean): string {
  if (!active) {
    return "";
  }
  return matched ? "activity-highlighted" : "activity-filtered-out";
}

function weekSessionSegments(
  sessions: ActivityHistorySession[],
  range: { start: Date; end: Date },
): ActivityHistorySession[][] {
  const columns = Array.from({ length: 7 }, () => [] as ActivityHistorySession[]);
  for (const session of sessions) {
    const start = new Date(session.started_at);
    const end = new Date(session.ended_at);
    for (let index = 0; index < 7; index += 1) {
      const dayStart = addDays(range.start, index);
      const dayEnd = addDays(dayStart, 1);
      if (start < dayEnd && end > dayStart) {
        columns[index].push(session);
      }
    }
  }
  for (const column of columns) {
    column.sort((left, right) => left.started_at.localeCompare(right.started_at));
  }
  return columns;
}

function weekActivityBlockSegments(
  sessions: ActivityHistorySession[],
  range: { start: Date; end: Date },
): ActivityBlock[][] {
  return weekSessionSegments(sessions, range).map((column) => buildActivityBlocks(column));
}

function maxWeekSessionCount(
  sessions: ActivityHistorySession[],
  range: { start: Date; end: Date },
): number {
  return Math.max(0, ...weekActivityBlockSegments(sessions, range).map((column) => column.length));
}

function minIso(left: string, right: string): string {
  return left <= right ? left : right;
}

function maxIso(left: string, right: string): string {
  return left >= right ? left : right;
}
