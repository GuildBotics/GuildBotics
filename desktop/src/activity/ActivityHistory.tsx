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
  type FloatingPosition,
} from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useMemo, useRef, useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import {
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  CircleDot,
  Clock3,
  ExternalLink,
  FileText,
  GitBranch,
  GitCommitHorizontal,
  GitMerge,
  GitPullRequest,
  History,
  Search,
  Upload,
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
import { useMemberCliAgentLabel } from "../cliAgent";
import { traceStatusColor } from "../traceStatus";

// Statuses from the workflow-completion / dispatch-lifecycle layers
// (issue #313): a session can look identical to a successful one (clean
// provider span) while actually waiting on a retry, permanently abandoned, or
// missing its completion evidence. These need their own visual state so
// Activity History does not show them as ordinary completed sessions.
const STATUS_ALERT_PRIORITY = ["abandoned", "incomplete", "retry_scheduled"] as const;
type StatusAlert = (typeof STATUS_ALERT_PRIORITY)[number];

function sessionStatusAlert(status: string): StatusAlert | null {
  return (STATUS_ALERT_PRIORITY as readonly string[]).includes(status)
    ? (status as StatusAlert)
    : null;
}

function strongestStatusAlert(sessions: ActivityHistorySession[]): StatusAlert | null {
  return (
    STATUS_ALERT_PRIORITY.find((candidate) =>
      sessions.some((session) => session.status === candidate),
    ) ?? null
  );
}

export type ActivityView = "day" | "week";
export type ActivitySessionMode = ActivityHistorySession["mode"];
export type ActivityBlockMode = ActivitySessionMode | "mixed";
const SESSION_MODE_ORDER: ActivitySessionMode[] = ["workflow", "interactive"];

const HOURS = ["03:00", "06:00", "09:00", "12:00", "15:00", "18:00", "21:00"];
const ACTIVITY_LIMIT = 1000;
const ACTIVITY_BLOCK_MINUTES = 60;
const EVENT_STACK_WINDOW_MS = 60 * 1000;
const EVENT_STACK_GAP_PX = 18;

// Week-view row height mirrors the stacked-bar geometry of `.activity-week-day`
// in styles.css: each bar is WEEK_BLOCK_HEIGHT_PX tall, bars are separated by
// WEEK_BLOCK_GAP_PX, and the column adds WEEK_COLUMN_PADDING_PX top and bottom.
// Keep these in sync with that CSS so the row has no trailing empty space.
const WEEK_BLOCK_HEIGHT_PX = 24;
const WEEK_BLOCK_GAP_PX = 4;
const WEEK_COLUMN_PADDING_PX = 6;
const WEEK_ROW_MIN_HEIGHT_PX = 86;

export type ActivityHistoryMatchState = {
  active: boolean;
  sessionIds: Set<string>;
  eventIds: Set<string>;
};

type ActivityBlock = {
  id: string;
  person_id: string;
  mode: ActivityBlockMode;
  title: string;
  started_at: string;
  ended_at: string;
  display_started_at: string;
  display_ended_at: string;
  sessions: ActivityHistorySession[];
  links: ActivityHistoryLink[];
  rate_limit: ActivityHistorySession["rate_limit"];
  status_alert: StatusAlert | null;
};

export function ActivityHistoryPage() {
  const { t, i18n } = useTranslation();
  const [view, setView] = useState<ActivityView>("day");
  const [baseDate, setBaseDate] = useState(() => startOfLocalDay(new Date()));
  const [query, setQuery] = useState("");
  const forceRefresh = useRef(true);
  const range = useMemo(() => activityRange(baseDate, view), [baseDate, view]);
  const syncRange = useMemo(() => activityRange(baseDate, "week"), [baseDate]);
  const history = useQuery({
    queryKey: ["activity-history", range.start.toISOString(), range.end.toISOString()],
    queryFn: async () => {
      try {
        return await getActivityHistory({
          start: range.start.toISOString(),
          end: range.end.toISOString(),
          limit: ACTIVITY_LIMIT,
          refresh: forceRefresh.current,
          syncStart: syncRange.start.toISOString(),
          syncEnd: syncRange.end.toISOString(),
        });
      } finally {
        forceRefresh.current = false;
      }
    },
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
          <Alert color="danger" title={t("activity.loadError")}>
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
      {view === "day" ? (
        <ActivityTimelineRow
          label={t("activity.events")}
          avatar={<History size={15} />}
          sessions={[]}
          events={sharedEvents}
          range={range}
          view={view}
          now={now}
          matches={matches}
          team
        />
      ) : null}
      {data.members.map((member) => (
        <ActivityTimelineRow
          key={member.person_id}
          label={member.name || member.person_id}
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
  const { t } = useTranslation();
  const blocks = buildActivityBlocks(sessions);
  const weekSessionCount = view === "week" ? maxWeekSessionCount(sessions, range) : 0;
  const eventTop = team ? 10 : view === "week" ? Math.max(48, weekSessionCount * 30 + 16) : 48;
  const visibleEvents = view === "week" ? [] : events;
  const eventTops = stackedEventTops(visibleEvents, eventTop);
  const eventRows = Math.max(
    1,
    ...Array.from(eventTops.values(), (top) => (top - eventTop) / EVENT_STACK_GAP_PX + 1),
  );
  const rowMinHeight = team
    ? Math.max(38, eventTop + eventRows * EVENT_STACK_GAP_PX + 8)
    : view === "week"
      ? weekRowMinHeight(weekSessionCount)
      : Math.max(86, eventTop + eventRows * EVENT_STACK_GAP_PX + 8);
  const activeRateLimit = member ? activeRateLimitForSessions(sessions, now ?? new Date()) : null;
  const activeRateLimitReset = activeRateLimit ? formatRateLimitReset(activeRateLimit) : "";
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
          {member ? <MemberCliAgentRole member={member} /> : null}
          {activeRateLimit ? (
            <span className="activity-member-rate-limit">
              <CircleAlert size={12} aria-hidden="true" />
              <span>{t("activity.rateLimit.label")}</span>
              {activeRateLimitReset ? <span>{activeRateLimitReset}</span> : null}
            </span>
          ) : null}
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
            top={eventTops.get(event.id) ?? eventTop}
          />
        ))}
        {view === "day" && now ? (
          <div className="activity-now-line" style={{ left: `${positionInRange(now, range)}%` }} />
        ) : null}
      </div>
    </div>
  );
}

export function stackedEventTops(
  events: ActivityHistoryEvent[],
  baseTop: number,
): Map<string, number> {
  const tops = new Map<string, number>();
  let previousTimestamp = Number.NEGATIVE_INFINITY;
  let stackIndex = 0;
  for (const event of [...events].sort(
    (left, right) => new Date(left.timestamp).getTime() - new Date(right.timestamp).getTime(),
  )) {
    const timestamp = new Date(event.timestamp).getTime();
    stackIndex = timestamp - previousTimestamp <= EVENT_STACK_WINDOW_MS ? stackIndex + 1 : 0;
    tops.set(event.id, baseTop + stackIndex * EVENT_STACK_GAP_PX);
    previousTimestamp = timestamp;
  }
  return tops;
}

// Sub-label under the member name: the AI CLI tool the member runs (matching the
// setup members list), or "Human" for human members whose config has no tool.
function MemberCliAgentRole({ member }: { member: ActivityHistoryMember }) {
  const { t } = useTranslation();
  const isHuman = member.person_type === "human";
  const cliAgentLabel = useMemberCliAgentLabel(member.person_id, !isHuman);
  const label = isHuman ? t("activity.memberHuman") : cliAgentLabel;
  if (!label) {
    return null;
  }
  return <span className="activity-member-role">{label}</span>;
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
  const { t } = useTranslation();
  const start = new Date(block.display_started_at);
  const end = new Date(block.display_ended_at);
  const left = positionInRange(start, range);
  const right = positionInRange(end, range);
  const width = Math.max(0, right - left);
  const stateClass = searchStateClass(searchActive, matched);
  const visibleTitle = activityBlockVisibleTitle(block.title);
  // Rate limiting takes visual priority (it already has its own affordance);
  // otherwise the strongest completion/dispatch status alert on the block
  // gets its own tinted state so retry-scheduled/abandoned/incomplete
  // sessions never look identical to an ordinary completed session.
  const alertClass = block.rate_limit
    ? "activity-session-rate-limited"
    : block.status_alert
      ? `activity-session-status-${block.status_alert}`
      : "";
  const rateLimitReset = block.rate_limit ? formatRateLimitReset(block.rate_limit) : "";
  const ariaLabel = block.rate_limit
    ? `${t("activity.rateLimit.label")}: ${block.title}${
        rateLimitReset ? ` (${t("activity.rateLimit.reset", { reset: rateLimitReset })})` : ""
      }`
    : block.status_alert
      ? `${t(`activity.status.${block.status_alert}`)}: ${block.title}`
      : block.title;
  return (
    <HoverCard openDelay={150} closeDelay={80} withinPortal position={blockHoverCardPosition(view)}>
      <HoverCard.Target>
        <button
          type="button"
          aria-label={ariaLabel}
          className={`${stateClass} activity-session activity-session-${block.mode} ${alertClass} ${
            view === "week" ? "activity-session-week" : ""
          }`}
          style={{
            ...(view === "day" ? { left: `${left}%`, top: "10px", width: `${width}%` } : {}),
          }}
        >
          {block.rate_limit || block.status_alert ? (
            <CircleAlert className="activity-session-alert-icon" size={13} aria-hidden="true" />
          ) : null}
          <span>{visibleTitle}</span>
        </button>
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
        <button
          type="button"
          aria-label={event.title}
          className={`activity-event-pin activity-event-${event.type} ${searchStateClass(searchActive, matched)}`}
          style={{ left: `${positionInRange(new Date(event.timestamp), range)}%`, top }}
        >
          {eventIcon(event.type)}
        </button>
      </HoverCard.Target>
      <HoverCard.Dropdown className="activity-hover-card">
        <ActivityEventDetail event={event} />
      </HoverCard.Dropdown>
    </HoverCard>
  );
}

function ActivityBlockDetail({ block }: { block: ActivityBlock }) {
  const { t } = useTranslation();
  const executionUrl = activityBlockExecutionUrl(block);
  const rateLimitReset = block.rate_limit ? formatRateLimitReset(block.rate_limit) : "";
  return (
    <Stack gap="xs">
      <Group gap="xs" justify="space-between">
        {executionUrl ? (
          <Link className="activity-block-title-link" to={executionUrl}>
            <Text fw={700} size="sm" lineClamp={2}>
              {block.title}
            </Text>
          </Link>
        ) : (
          <Text fw={700} size="sm" lineClamp={2}>
            {block.title}
          </Text>
        )}
      </Group>
      {block.rate_limit ? (
        <Group gap="xs" wrap="nowrap">
          <Badge color="danger" variant="light" style={{ flexShrink: 0 }}>
            {t("activity.rateLimit.label")}
          </Badge>
          {rateLimitReset ? (
            <Text c="dimmed" size="xs">
              {t("activity.rateLimit.reset", { reset: rateLimitReset })}
            </Text>
          ) : null}
        </Group>
      ) : null}
      {block.status_alert ? (
        <Group gap="xs" wrap="nowrap">
          <Badge
            color={traceStatusColor(block.status_alert)}
            variant="light"
            style={{ flexShrink: 0 }}
          >
            {t(`activity.status.${block.status_alert}`)}
          </Badge>
        </Group>
      ) : null}
      <Stack gap={6}>
        {blockModes(block.sessions).map((mode) => {
          const span = sessionsTimeSpan(block.sessions.filter((session) => session.mode === mode));
          return (
            <Group key={mode} gap="xs" wrap="nowrap">
              <Badge color={sessionModeColor(mode)} variant="light" style={{ flexShrink: 0 }}>
                {t(`activity.modes.${mode}`)}
              </Badge>
              <Text c="dimmed" size="xs">
                <Clock3 size={12} /> {formatTimeRange(span.start, span.end)} ·{" "}
                {formatDuration(spanSeconds(span))}
              </Text>
            </Group>
          );
        })}
      </Stack>
      {block.sessions.length > 1 ? (
        <div className="activity-block-session-list">
          {block.sessions.map((session) => (
            <div key={session.trace_id}>
              <Group gap={6} wrap="nowrap">
                <span
                  className={`activity-session-mode-dot activity-session-mode-dot-${session.mode}`}
                  aria-hidden
                />
                <Text fw={600} lineClamp={1} size="xs">
                  {session.title}
                </Text>
                {sessionStatusAlert(session.status) ? (
                  <Badge
                    color={traceStatusColor(session.status)}
                    variant="light"
                    size="xs"
                    style={{ flexShrink: 0 }}
                  >
                    {t(`activity.status.${session.status}`)}
                  </Badge>
                ) : null}
              </Group>
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
  const orderedLinks = orderedActivityLinks(links);
  if (orderedLinks.length === 0) {
    return (
      <Text c="dimmed" fs="italic" size="xs">
        {t("activity.noLinks")}
      </Text>
    );
  }
  return (
    <div className="activity-link-list">
      {orderedLinks.map((link) => {
        const href = activityLinkHref(link);
        const icon = activityLinkIcon(link.kind);
        const className = `activity-link-kind-${link.kind}`;
        return href && isInternalActivityLink(href) ? (
          <Link className={className} to={href} key={activityLinkKey(link)}>
            {icon}
            <span>{link.label}</span>
          </Link>
        ) : href ? (
          <a
            className={className}
            href={href}
            key={activityLinkKey(link)}
            target="_blank"
            rel="noreferrer"
          >
            {icon}
            <span>{link.label}</span>
          </a>
        ) : (
          <span className={`activity-link-item ${className}`} key={activityLinkKey(link)}>
            {icon}
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

function isInternalActivityLink(href: string): boolean {
  return href.startsWith("/");
}

function activityLinkIcon(kind: ActivityHistoryLink["kind"]): ReactNode {
  if (kind === "doc") {
    return <FileText size={12} />;
  }
  if (kind === "commit") {
    return <GitCommitHorizontal size={12} />;
  }
  if (kind === "pull_request") {
    return <GitPullRequest size={12} />;
  }
  if (kind === "issue") {
    return <CircleDot size={12} />;
  }
  return <ExternalLink size={12} />;
}

export function activityLinkKey(link: ActivityHistoryLink): string {
  return `${link.kind}:${link.label}:${link.url}`;
}

// Same target (kind + url) is one link even if the labels differ (e.g. a PR from
// github attributes vs the same PR referenced by a memory note). Mirrors the
// backend dedupe so merging sessions into a block does not resurrect duplicates.
export function activityLinkDedupeKey(link: ActivityHistoryLink): string {
  return link.url ? `${link.kind}:${link.url}` : `${link.kind}:label:${link.label}`;
}

export function orderedActivityLinks(links: ActivityHistoryLink[]): ActivityHistoryLink[] {
  return [...links].sort(
    (left, right) => activityLinkTimestamp(left) - activityLinkTimestamp(right),
  );
}

function activityLinkTimestamp(link: ActivityHistoryLink): number {
  if (!link.timestamp) {
    return Number.NEGATIVE_INFINITY;
  }
  const value = Date.parse(link.timestamp);
  return Number.isNaN(value) ? Number.NEGATIVE_INFINITY : value;
}

export function activityBlockExecutionUrl(block: ActivityBlock): string {
  const traceIds = uniqueTraceIds(block.sessions);
  if (traceIds.length === 0) {
    return "";
  }
  const search = new URLSearchParams({ tab: "executions" });
  search.set("trace_ids", traceIds.join(","));
  return `/diagnostics?${search.toString()}`;
}

function uniqueTraceIds(sessions: ActivityHistorySession[]): string[] {
  return Array.from(
    new Set(
      sessions.map((session) => session.trace_id.trim()).filter((traceId) => traceId.length > 0),
    ),
  );
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
              session.status,
              session.rate_limit ? "rate_limited" : "",
              session.rate_limit?.retry_after_at ?? "",
              session.rate_limit?.retry_after_text ?? "",
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
    rate_limit: session.rate_limit ?? null,
    status_alert: sessionStatusAlert(session.status),
  };
}

function mergeSessionIntoBlock(
  block: ActivityBlock,
  session: ActivityHistorySession,
  displayEnd: Date,
) {
  block.id = `${block.id}:${session.trace_id}`;
  block.sessions.push(session);
  block.mode = blockMode(block.sessions);
  block.title = blockTitle(block.sessions);
  block.started_at = minIso(block.started_at, session.started_at);
  block.ended_at = maxIso(block.ended_at, session.ended_at);
  block.display_ended_at = maxIso(block.display_ended_at, displayEnd.toISOString());
  block.links = dedupeLinks(block.sessions.flatMap((item) => item.links));
  block.rate_limit = block.sessions.find((item) => item.rate_limit)?.rate_limit ?? null;
  block.status_alert = strongestStatusAlert(block.sessions);
}

function blockTitle(sessions: ActivityHistorySession[]): string {
  if (sessions.length === 1) {
    return sessions[0].title;
  }
  // Prefer a session whose title carries real signal over one that only fell
  // back to its raw command/workflow (e.g. the SKILL's setup step
  // "guildbotics member context"), so a merged block is labelled by its work.
  const primary = sessions.find(hasMeaningfulTitle) ?? sessions[0];
  return `${primary.title} +${sessions.length - 1}`;
}

function hasMeaningfulTitle(session: ActivityHistorySession): boolean {
  return session.title !== session.command && session.title !== session.workflow;
}

export function blockModes(sessions: ActivityHistorySession[]): ActivitySessionMode[] {
  const present = new Set(sessions.map((session) => session.mode));
  return SESSION_MODE_ORDER.filter((mode) => present.has(mode));
}

function blockMode(sessions: ActivityHistorySession[]): ActivityBlockMode {
  const modes = blockModes(sessions);
  return modes.length > 1 ? "mixed" : (modes[0] ?? "workflow");
}

function sessionModeColor(mode: ActivitySessionMode): string {
  return mode === "interactive" ? "teal" : "info";
}

function formatRateLimitReset(
  rateLimit: NonNullable<ActivityHistorySession["rate_limit"]>,
): string {
  if (rateLimit.retry_after_at) {
    return new Date(rateLimit.retry_after_at).toLocaleString([], {
      month: "numeric",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return rateLimit.retry_after_text;
}

function activeRateLimitForSessions(
  sessions: ActivityHistorySession[],
  now: Date,
): NonNullable<ActivityHistorySession["rate_limit"]> | null {
  const nowMs = now.getTime();
  return sessions.reduce<NonNullable<ActivityHistorySession["rate_limit"]> | null>(
    (current, session) => {
      const rateLimit = session.rate_limit;
      if (!rateLimit?.retry_after_at) {
        return current;
      }
      const retryAfterMs = new Date(rateLimit.retry_after_at).getTime();
      if (!Number.isFinite(retryAfterMs) || retryAfterMs <= nowMs) {
        return current;
      }
      if (!current) {
        return rateLimit;
      }
      const currentMs = new Date(current.retry_after_at).getTime();
      return retryAfterMs > currentMs ? rateLimit : current;
    },
    null,
  );
}

function activityBlockVisibleTitle(title: string): string {
  const trimmed = title.trim();
  const withoutPullRequestPrefix = trimmed.replace(
    /^(?:PR|Pull Request)\s*#\d+\s*[:\-–—]?\s*/i,
    "",
  );
  return withoutPullRequestPrefix || trimmed;
}

export function sessionsTimeSpan(sessions: ActivityHistorySession[]): {
  start: string;
  end: string;
} {
  return {
    start: sessions
      .map((session) => session.started_at)
      .reduce((left, right) => minIso(left, right)),
    end: sessions.map((session) => session.ended_at).reduce((left, right) => maxIso(left, right)),
  };
}

function spanSeconds(span: { start: string; end: string }): number {
  return Math.max(0, (new Date(span.end).getTime() - new Date(span.start).getTime()) / 1000);
}

function dedupeLinks(links: ActivityHistoryLink[]): ActivityHistoryLink[] {
  const order: string[] = [];
  const byKey = new Map<string, ActivityHistoryLink>();
  for (const link of links) {
    const key = activityLinkDedupeKey(link);
    const existing = byKey.get(key);
    if (!existing) {
      order.push(key);
      byKey.set(key, link);
      continue;
    }
    // A canonical GitHub label (e.g. "PR #246") always wins over a
    // memory-source label (e.g. "PR #246 <note title>") for the same target,
    // regardless of which session's link appears first when merging.
    if (!isCanonicalGitHubLabel(existing) && isCanonicalGitHubLabel(link)) {
      byKey.set(key, link);
    }
  }
  return order.map((key) => byKey.get(key)!);
}

const CANONICAL_GITHUB_LABEL = /^(?:PR|Issue) #\d+$/;

function isCanonicalGitHubLabel(link: ActivityHistoryLink): boolean {
  if (link.kind !== "pull_request" && link.kind !== "issue") {
    return true;
  }
  return CANONICAL_GITHUB_LABEL.test(link.label);
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
    case "issue_create":
      return <CircleDot size={12} />;
    case "issue_resolve":
      return <CheckCircle2 size={12} />;
    case "external":
      return <GitBranch size={12} />;
  }
}

function eventColor(type: ActivityHistoryEvent["type"]): string {
  return {
    pr_create: "info",
    pr_merge: "info",
    pr_closed: "danger",
    push: "warning",
    issue_create: "info",
    issue_resolve: "success",
    external: "neutral",
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
    column.sort(
      (left, right) => timestampMillis(left.started_at) - timestampMillis(right.started_at),
    );
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

// Week-view columns are read top-to-bottom then left-to-right, so open the
// hover card on the already-scanned left side: it never covers the bars still
// below in the same column nor the next day column to the right. The leftmost
// (Monday) column has no room and floating-ui flips it right, which is
// accepted. Day view keeps the default below-the-bar placement.
export function blockHoverCardPosition(view: ActivityView): FloatingPosition {
  return view === "week" ? "left-start" : "bottom";
}

export function weekRowMinHeight(sessionCount: number): number {
  if (sessionCount <= 0) {
    return WEEK_ROW_MIN_HEIGHT_PX;
  }
  const stackHeight =
    sessionCount * WEEK_BLOCK_HEIGHT_PX +
    (sessionCount - 1) * WEEK_BLOCK_GAP_PX +
    WEEK_COLUMN_PADDING_PX * 2;
  return Math.max(WEEK_ROW_MIN_HEIGHT_PX, stackHeight);
}

function minIso(left: string, right: string): string {
  return timestampMillis(left) <= timestampMillis(right) ? left : right;
}

function maxIso(left: string, right: string): string {
  return timestampMillis(left) >= timestampMillis(right) ? left : right;
}

function timestampMillis(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}
