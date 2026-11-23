import { Button, Group, Text } from "@mantine/core";
import { Plus } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../api/endpoints";
import { ticketRef, type Card, type Lane, type Ticket } from "../api/types";
import { ErrorMessage, Loading, Refusal } from "../components/common";
import { PageHeader } from "../components/PageHeader";
import { type Translate, useT } from "../i18n";
import { useBoard, useCards, useMembers, usePeople, useSession } from "../query/hooks";
import { useStored } from "../storage";
import { CardFilters } from "./board/CardFilters";
import { ciUrlsOf, moveRefusal, type Notice } from "./board/CardView";
import { NewTicketModal, useTicketRepos } from "./board/NewTicket";
import { ownsEscape } from "./ticket/TicketDrawer";
import { hasFilter, NO_FILTER, type CardFilter } from "./board/ordering";
import { readied, type Change, type Edit } from "./relations/edits";
import { Figure } from "./relations/Figure";
import type { Addition, PickupTag } from "./relations/RelationCard";
import { buildGraph, type Level } from "./relations/graph";
import { pickupOrders, placements, reordered, restoring, type PickupRow } from "./relations/pickup";
import { PickupPanel, reasonText, type Drop } from "./relations/PickupPanel";

/** A relation written: what it changes, what the notice then says, and what undoes it. */
interface Write {
  /** Written in order. */
  changes: Change[];
  done: string;
  undo?: Undo;
  /** The ticket to bring into view once written (a new one), whether or not the change is made. */
  reveal?: string;
}

/** What undoes a change: the changes, made against the board as it is when Undo is clicked (it may have changed since). */
type Undo = (cards: readonly Card[]) => Change[];

/** What the page says after a change: the board's notice with an undo, or a refusal of the service as the ticket page shows it. */
type Said = (Notice & { undo?: Undo }) | { tone: "error"; error: unknown };

/** What a new ticket made from a card's menu is made as. */
function addingNote(to: Card, as: Addition | undefined, t: Translate): string {
  const key = ticketRef(to);
  if (as === "parent") return to.parent ? t("relations.newParentBetween", { key, parent: ticketRef(to.parent) }) : t("relations.newParentOf", { key });
  return t(as === "child" ? "relations.newChildOf" : "relations.newNextOf", { key });
}

/** A card's tag of its number in its assignee's pickup order, and whether it will not start as things are. */
function tagOf(row: PickupRow, assignee: string, open: boolean, t: Translate): PickupTag {
  const place = t("pickup.tagNext", { name: assignee, n: String(row.n) });
  const detail = row.stuck.length > 0
    ? t("pickup.wontStart", { reason: reasonText(row.stuck, t) })
    : row.waits.length > 0
      ? t("pickup.waitHint", { tickets: row.waits.map((w) => w.ticket).join(t("pickup.and")) })
      : null;
  const state = detail ? `${place}${t("pickup.sentence")}${detail}` : place;
  return { n: row.n, waiting: row.waits.length > 0, stuck: row.stuck.length > 0, label: t("pickup.hint", { state }), open, assignee };
}

/** The pickup order panel: whose order it shows, and the card whose tag opened it. */
type Panel = { assignee: string; current: string | null };

/** A reorder of the pickup order to write, with the board's cards it was dropped on. */
type Reorder = Drop & { cards: Card[] };

/** The parents a viewer opened, as kept in the browser (a JSON array of refs). */
function openParents(stored: string | null): Set<string> {
  try {
    const refs: unknown = JSON.parse(stored ?? "[]");
    return new Set(Array.isArray(refs) ? refs.filter((r): r is string => typeof r === "string") : []);
  } catch {
    return new Set();
  }
}

/** The cards a figure shows: those of its levels, the open frames' and the new tickets' too. */
function cardsOf(level: Level): Card[] {
  return level.flat().flatMap((n) => ("fresh" in n ? n.fresh : [n.card, ...(n.inner ? cardsOf(n.inner) : [])]));
}

/** A workspace's tickets as a figure of their dependencies and parents. */
export function RelationsView({ workspace }: { workspace: string }) {
  const t = useT();
  const client = useQueryClient();
  const boardQuery = useBoard(workspace);
  const [closedShown, showClosed] = useStored("branvil.relations.includeClosed");
  const includeClosed = closedShown === "true";
  const [opened, keepOpened] = useStored(`branvil.relations.open.${workspace}`);
  const open = useMemo(() => openParents(opened), [opened]);
  const [filter, setFilter] = useState<CardFilter>(NO_FILTER);
  const filtering = hasFilter(filter);
  const [filteredExpansion, setFilteredExpansion] = useState<{ parents: Map<string, boolean>; fresh?: boolean }>({ parents: new Map() });
  const onFilter = (next: CardFilter) => {
    setFilter(next);
    setFilteredExpansion({ parents: new Map() });
  };
  const [notice, setNotice] = useState<Said | null>(null);
  /** The new ticket being made: unrelated, or related to a card. */
  const [adding, setAdding] = useState<{ to?: Card; as?: Addition } | null>(null);
  const [reveal, setReveal] = useState<string | null>(null);
  const [freshOpen, setFreshOpen] = useState(false);
  const { query: repos, repos: wsRepos, repo, chooseRepo } = useTicketRepos(workspace);

  // Closed tickets are read only when they are shown, or when one is the
  // parent of an open ticket (then it is shown too): an open ticket below a
  // closed ancestor has a closed parent itself or below an open one.
  const openCards = useCards(workspace, "open");
  const needAll = includeClosed || (openCards.data?.items ?? []).some((c) => c.parent?.state === "closed");
  const allCards = useCards(workspace, "all", needAll);
  // Relations change only once the cards they need are read; until then the
  // open ones are shown, and a failure to read them is shown with a retry.
  const required = needAll ? allCards : openCards;
  const shown = required.data ?? openCards.data;
  const complete = required.data !== undefined;
  const cards = useMemo(() => shown?.items ?? [], [shown]);
  const graph = useMemo(
    () => buildGraph(cards, { includeClosed, open, filter, openOverrides: filteredExpansion.parents }),
    [cards, includeClosed, open, filter, filteredExpansion.parents],
  );
  const lanes = useMemo(() => boardQuery.data?.lanes ?? [], [boardQuery.data]);
  const people = usePeople();
  // Checks are loaded for the pull requests of the cards the figure shows.
  const ciUrls = useMemo(() => ciUrlsOf(cardsOf(graph.level)), [graph]);
  const refresh = () => client.invalidateQueries({ queryKey: ["board"] });

  // The owner's pickup orders: only the owner reads members' kinds, and decides what comes first.
  const owner = useSession().data?.me.kind === "owner";
  const members = useMembers();
  const isAgent = useMemo(() => {
    const agents = new Set((members.data?.items ?? []).filter((m) => m.kind === "agent").map((m) => m.name.toLowerCase()));
    return (name: string) => agents.has(name.toLowerCase());
  }, [members.data]);
  const [panel, setPanel] = useState<Panel | null>(null);
  /** A row being moved, shown before it is written (and until the cards are read again after). */
  const [preview, setPreview] = useState<{ assignee: string; order: string[] } | null>(null);
  const pointedAt = panel?.current ?? null;
  const [highlight, setHighlight] = useState<string | null>(null);
  const [point, setPoint] = useState<{ ref: string } | null>(null);
  // Before the filters: a number must not change with what is shown.
  const orders = useMemo(
    () =>
      owner && members.data ? pickupOrders(preview ? reordered(cards, preview.assignee, preview.order) : cards, isAgent) : null,
    [owner, members.data, preview, cards, isAgent],
  );
  const tags = useMemo(() => {
    if (!orders) return null;
    const tagged = new Map<string, PickupTag>();
    for (const [who, rows] of orders) {
      for (const row of rows) {
        tagged.set(row.ref, tagOf(row, who, pointedAt === row.ref, t));
      }
    }
    return tagged;
  }, [orders, pointedAt, t]);
  // Every change of what the panel shows ends a move shown before it is written: its rows are gone.
  const showPanel = useCallback((next: Panel | null | ((prev: Panel | null) => Panel | null)) => {
    setPanel(next);
    setHighlight(null);
    setPreview(null);
  }, []);
  const closePanel = useCallback(() => showPanel(null), [showPanel]);
  const onTag = useCallback(
    (ref: string, assignee: string) => showPanel((prev) => (prev?.current === ref ? null : { assignee, current: ref })),
    [showPanel],
  );
  useEffect(() => {
    if (!panel) return;
    // Not an Escape that belongs elsewhere: a field typed in, an open dropdown, a drawer or modal.
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape" || e.defaultPrevented || e.isComposing || ownsEscape(e.target as HTMLElement)) return;
      if (!document.querySelector('[role="dialog"]')) closePanel();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panel, closePanel]);

  // The figure changes once the service has the change: a refusal leaves it as it was.
  const relate = useMutation({
    mutationFn: async ({ changes }: Write) => {
      for (const c of changes) await ("patch" in c ? api.tickets.update(c.repo, c.key, c.patch) : api.tickets.place(c.repo, c.key, { lane: c.lane, ...c.at }));
    },
    onMutate: () => setNotice(null),
    onSuccess: (_ticket, { done, undo }) => setNotice({ tone: "info", text: done, undo }),
    onError: (error) => setNotice({ tone: "error", error }),
    // The figure is changed only once the cards are read again with what was written (until then
    // it is not editable), and only then is a new ticket asked into view: before, it would be found
    // where it was (on its own or among the new tickets) and the request used up there.
    onSettled: async (_ticket, _error, { reveal }) => {
      await refresh();
      if (reveal) setReveal(reveal);
    },
  });
  const move = useMutation({
    mutationFn: ({ card, lane, at }: { card: Card; lane: Lane; at?: { before: string } | { after: string } }) =>
      api.tickets.place(card.repository, card.key, { lane, ...at }),
    onMutate: () => setNotice(null),
    onSuccess: (_ticket, { card, lane }) => setNotice({ tone: "info", text: t("board.moved", { key: ticketRef(card), lane: t(`lane.${lane}`) }) }),
    onError: (err, { card }) => setNotice(moveRefusal(err, card, t)),
    onSettled: () => void refresh(),
  });

  // Written in turn; a refusal stops it, and what was written can be undone.
  const reorder = useMutation({
    mutationFn: async ({ moved, order, group, lane, cards: latest }: Reorder): Promise<Said> => {
      const byRef = new Map(latest.map((c) => [ticketRef(c), c]));
      const readyNow = new Set(latest.filter((c) => c.state === "open" && c.lane === "ready").map(ticketRef));
      const plan = placements(order, group, readyNow);
      if ("stale" in plan) return { tone: "info", text: t("pickup.stale", { key: plan.stale }) };
      const done: string[] = [];
      // Put back against the board as it is then: a ticket may have left the Ready lane meanwhile.
      const undo = (): Undo | undefined => {
        const moved = [...done];
        if (moved.length === 0) return undefined;
        return (now) => {
          const nowByRef = new Map(now.map((c) => [ticketRef(c), c]));
          const ready = new Set(now.filter((c) => c.state === "open" && c.lane === "ready").map(ticketRef));
          return restoring(lane, moved, ready).map(({ ref, at }) => {
            const c = nowByRef.get(ref) as Card;
            return { repo: c.repository, key: c.key, lane: "ready" as const, at };
          });
        };
      };
      for (const { ref, at } of plan.writes) {
        const c = byRef.get(ref) as Card;
        try {
          await api.tickets.place(c.repository, c.key, { lane: "ready", ...at });
        } catch (err) {
          return { ...moveRefusal(err, c, t), undo: undo() };
        }
        done.push(ref);
      }
      const others = group.filter((r) => r !== moved);
      const text = others.length > 0 ? t("pickup.reorderedWith", { key: moved, tickets: others.join(", ") }) : t("pickup.reordered", { key: moved });
      return { tone: "info", text, undo: undo() };
    },
    onMutate: () => setNotice(null),
    onSuccess: (said) => setNotice(said),
    onError: (error) => setNotice({ tone: "error", error }),
    // The order shown while it is written stays until the cards are read again with it.
    onSettled: async () => {
      await refresh();
      setPreview(null);
    },
  });

  const { mutate: write } = relate;
  const { mutate: place } = move;
  const onEdit = useCallback((edit: Edit) => write({ changes: edit.changes, done: t(edit.message, edit.params), undo: () => edit.inverse }), [write, t]);
  const onLane = useCallback((card: Card, lane: Lane) => place({ card, lane }), [place]);
  const onPlace = useCallback((card: Card, at: { before: string } | { after: string }) => place({ card, lane: card.lane, at }), [place]);
  const onAdd = useCallback((to: Card, as: Addition) => setAdding({ to, as }), []);
  const onRevealed = useCallback(() => setReveal(null), []);
  // A new ticket made as a child is shown inside its parent: the parents above it are opened.
  useEffect(() => {
    const card = reveal ? cards.find((c) => ticketRef(c) === reveal) : undefined;
    const closed: string[] = [];
    const byRef = new Map(cards.map((c) => [ticketRef(c), c]));
    for (let p = card?.parent; p; p = byRef.get(ticketRef(p))?.parent) if (!open.has(ticketRef(p))) closed.push(ticketRef(p));
    if (closed.length > 0) keepOpened(JSON.stringify([...open, ...closed]));
  }, [reveal, cards, open, keepOpened]);
  const onCreated = (ticket: Ticket) => {
    // Creation leaves filtering so the new card can be revealed, even if it
    // does not match. All creation paths share this successful callback.
    onFilter(NO_FILTER);
    const made = ticketRef(ticket);
    const to = adding?.to;
    if (!to) {
      setReveal(made);
      return;
    }
    const key = ticketRef(to);
    const madeChange = (patch: api.TicketPatch) => ({ repo: ticket.repository, key: ticket.key, patch });
    // The relation, then the new tickets it relates made ready.
    const related = (changes: Change[], done: string) => write({ changes: [...changes, ...readied([ticket, to])], done, reveal: made });
    if (adding.as === "parent") {
      // A parent made for a child keeps it where it was: under the child's parent, if it had one.
      const changes = [...(to.parent ? [madeChange({ parent: to.parent.url })] : []), { repo: to.repository, key: to.key, patch: { parent: ticket.url } }];
      // Opened, so the child is seen in it.
      keepOpened(JSON.stringify([...open, made]));
      related(changes, t("relations.createdParent", { parent: made, child: key }));
    } else if (adding.as === "child") {
      related([madeChange({ parent: to.url })], t("relations.createdChild", { child: made, parent: key }));
    } else {
      related([madeChange({ add_blocked_by: [to.url] })], t("relations.createdNext", { blocked: made, blocker: key }));
    }
  };
  const onToggle = useCallback(
    (ref: string, show: boolean) => {
      if (filtering) {
        setFilteredExpansion((prev) => ({ ...prev, parents: new Map(prev.parents).set(ref, show) }));
        return;
      }
      const next = new Set(open);
      if (show) next.add(ref);
      else next.delete(ref);
      keepOpened(JSON.stringify([...next]));
    },
    [filtering, open, keepOpened],
  );
  const onFreshOpen = useCallback((show: boolean) => {
    if (filtering) setFilteredExpansion((prev) => ({ ...prev, fresh: show }));
    else setFreshOpen(show);
  }, [filtering]);

  if (boardQuery.isPending) return <Loading />;
  if (boardQuery.isError) return <ErrorMessage error={boardQuery.error} />;

  return (
    <div className="workspace-fill">
      <PageHeader
        title={t("relations.title")}
        meta={
          <Text size="sm" c="dimmed">
            {t("board.summary", { workspace: boardQuery.data.name, count: cards.filter((c) => c.state === "open").length })}
          </Text>
        }
        right={
          <Button variant="default" leftSection={<Plus size={16} />} disabled={!repo} onClick={() => setAdding({})}>
            {t("board.add")}
          </Button>
        }
      />
      <CardFilters
        cards={cards}
        people={people}
        repositories={wsRepos.map((r) => r.path)}
        filter={filter}
        onFilter={onFilter}
        includeClosed={includeClosed}
        onIncludeClosed={(include) => showClosed(String(include))}
        status={required.isFetching && shown && t("common.refreshing")}
      />
      {notice && "error" in notice && <ErrorMessage error={notice.error} />}
      {notice && "text" in notice && (
        <Group gap="sm" role={notice.tone === "info" ? "status" : undefined}>
          {notice.tone === "error" ? <Refusal>{notice.text}</Refusal> : <Text size="sm">{notice.text}</Text>}
          {notice.undo && (
            <Button
              size="compact-sm"
              variant="default"
              // Not while the cards are read again: Undo is made against the board as it is.
              disabled={relate.isPending || reorder.isPending || required.isFetching}
              onClick={() => {
                const changes = notice.undo?.(cards) ?? [];
                if (changes.length > 0) write({ changes, done: t("relations.undone") });
                else setNotice({ tone: "info", text: t("pickup.undoGone") });
              }}
            >
              {t("relations.undo")}
            </Button>
          )}
        </Group>
      )}
      {repos.isError && <ErrorMessage error={repos.error} />}
      {required.isError && (
        <Group gap="sm">
          <ErrorMessage error={required.error} />
          <Button size="compact-sm" variant="default" onClick={refresh}>
            {t("common.retry")}
          </Button>
        </Group>
      )}
      {owner && members.isError && <ErrorMessage error={members.error} />}
      {!shown ? (
        !required.isError && <Loading />
      ) : (
        <div className="relations-body">
        <Figure
          graph={graph}
          cards={cards}
          lanes={lanes}
          people={people}
          loadChecks={ciUrls}
          filter={filter}
          moving={move.isPending && move.variables ? ticketRef(move.variables.card) : null}
          editable={complete && !relate.isPending && !reorder.isPending}
          onToggle={onToggle}
          onLane={onLane}
          onPlace={onPlace}
          onEdit={onEdit}
          onAdd={onAdd}
          reveal={reveal}
          onRevealed={onRevealed}
          freshOpen={filtering ? (filteredExpansion.fresh ?? true) : freshOpen}
          onFreshOpen={onFreshOpen}
          tags={tags}
          onTag={onTag}
          highlight={highlight}
          point={point}
        />
        {panel && orders && (
          <PickupPanel
            // A move belongs to the rows it started among.
            key={`${panel.assignee}\n${panel.current}`}
            assignees={[...new Set([...orders.keys(), panel.assignee])].map((value) => ({ value, label: value }))}
            assignee={panel.assignee}
            rows={orders.get(panel.assignee) ?? []}
            current={panel.current}
            cards={cards}
            hidden={(ref) => !graph.shown.has(ref)}
            disabled={!complete || relate.isPending || reorder.isPending || move.isPending}
            onAssignee={(assignee) => showPanel({ assignee, current: null })}
            onClose={closePanel}
            onHighlight={setHighlight}
            onPoint={(ref) => setPoint({ ref })}
            onPreview={(order) => setPreview(order && { assignee: panel.assignee, order })}
            onDrop={(drop) => {
              setPreview({ assignee: panel.assignee, order: drop.order });
              reorder.mutate({ ...drop, cards });
            }}
          />
        )}
        </div>
      )}
      <NewTicketModal
        workspace={workspace}
        lanes={lanes}
        // A ticket made related to a card is ready: only one without relations is new.
        initialLane={adding && (adding.to ? "ready" : "new")}
        note={adding?.to && addingNote(adding.to, adding.as, t)}
        onCreated={onCreated}
        repos={wsRepos}
        repo={repo}
        onRepo={chooseRepo}
        onClose={() => setAdding(null)}
      />
    </div>
  );
}
