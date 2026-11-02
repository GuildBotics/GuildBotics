import { Button, Group, Text } from "@mantine/core";
import { Plus } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useMemo, useState } from "react";
import * as api from "../api/endpoints";
import { ticketRef, type Card, type Lane, type Ticket } from "../api/types";
import { ErrorMessage, Loading, Refusal } from "../components/common";
import { PageHeader } from "../components/PageHeader";
import { type Translate, useT } from "../i18n";
import { useBoard, useCards, usePeople } from "../query/hooks";
import { useStored } from "../storage";
import { CardFilters } from "./board/CardFilters";
import { ciUrlsOf, moveRefusal, type Notice } from "./board/CardView";
import { NewTicketModal, useTicketRepos } from "./board/NewTicket";
import { hasFilter, type CardFilter } from "./board/ordering";
import { readied, type Change, type Edit } from "./relations/edits";
import { Figure } from "./relations/Figure";
import type { Addition } from "./relations/RelationCard";
import { buildGraph, type Level } from "./relations/graph";

/** A relation written: what it changes, what the notice then says, and what undoes it. */
interface Write {
  /** Written in order. */
  changes: Change[];
  done: string;
  undo?: Change[];
  /** The ticket to bring into view once written (a new one), whether or not the change is made. */
  reveal?: string;
}

/** What the page says after a change: the board's notice with an undo, or a refusal of the service as the ticket page shows it. */
type Said = (Notice & { undo?: Change[] }) | { tone: "error"; error: unknown };

/** What a new ticket made from a card's menu is made as. */
function addingNote(to: Card, as: Addition | undefined, t: Translate): string {
  const key = ticketRef(to);
  if (as === "parent") return to.parent ? t("relations.newParentBetween", { key, parent: ticketRef(to.parent) }) : t("relations.newParentOf", { key });
  return t(as === "child" ? "relations.newChildOf" : "relations.newNextOf", { key });
}

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
  const [filter, setFilter] = useState<CardFilter>({ assignee: "", label: "", text: "" });
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

  // The figure changes once the service has the change: a refusal leaves it as it was.
  const relate = useMutation({
    mutationFn: async ({ changes }: Write) => {
      for (const c of changes) await ("patch" in c ? api.tickets.update(c.repo, c.key, c.patch) : api.tickets.place(c.repo, c.key, { lane: c.lane }));
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

  const { mutate: write } = relate;
  const { mutate: place } = move;
  const onEdit = useCallback((edit: Edit) => write({ changes: edit.changes, done: t(edit.message, edit.params), undo: edit.inverse }), [write, t]);
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
    onFilter({ assignee: "", label: "", text: "" });
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
        filter={filter}
        onFilter={onFilter}
        includeClosed={includeClosed}
        onIncludeClosed={(include) => showClosed(String(include))}
        status={required.isFetching && shown && t("common.refreshing")}
      />
      {notice && "error" in notice && <ErrorMessage error={notice.error} />}
      {notice && "text" in notice && notice.tone === "error" && <Refusal>{notice.text}</Refusal>}
      {notice && "text" in notice && notice.tone === "info" && (
        <Group gap="sm" role="status">
          <Text size="sm">{notice.text}</Text>
          {notice.undo && (
            <Button
              size="compact-sm"
              variant="default"
              disabled={relate.isPending}
              onClick={() => notice.undo && write({ changes: notice.undo, done: t("relations.undone") })}
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
      {!shown ? (
        !required.isError && <Loading />
      ) : (
        <Figure
          graph={graph}
          cards={cards}
          lanes={lanes}
          people={people}
          loadChecks={ciUrls}
          filter={filter}
          moving={move.isPending && move.variables ? ticketRef(move.variables.card) : null}
          editable={complete && !relate.isPending}
          onToggle={onToggle}
          onLane={onLane}
          onPlace={onPlace}
          onEdit={onEdit}
          onAdd={onAdd}
          reveal={reveal}
          onRevealed={onRevealed}
          freshOpen={filtering ? (filteredExpansion.fresh ?? true) : freshOpen}
          onFreshOpen={onFreshOpen}
        />
      )}
      <NewTicketModal
        workspace={workspace}
        lanes={lanes}
        // A ticket made related to a card is ready: only one without relations is new.
        start={adding && { lane: adding.to ? "ready" : "new", title: "" }}
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
