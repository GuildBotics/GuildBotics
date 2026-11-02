import { Badge, Text } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useCallback, useMemo, useState, type DragEvent } from "react";
import * as api from "../api/endpoints";
import { ticketRef, type Card, type Lane } from "../api/types";
import { ErrorMessage, Loading, Refusal } from "../components/common";
import { PageHeader } from "../components/PageHeader";
import { useStored } from "../storage";
import { useT } from "../i18n";
import { useBoard, useCards, usePeople } from "../query/hooks";
import { keys } from "../query/keys";
import { CardFilters } from "./board/CardFilters";
import { BoardCard, ciUrlsOf, moveRefusal, type Notice } from "./board/CardView";
import { LaneAdd, NewTicketModal, useTicketRepos } from "./board/NewTicket";
import {
  applyMove,
  groupByLane,
  isNoop,
  matchesFilter,
  neighbors,
  resolveDropIndex,
  type CardFilter,
} from "./board/ordering";

interface MoveVars {
  card: Card;
  lane: Lane;
  index: number;
}

/** A workspace's page: its board. */
export function BoardView({ workspace }: { workspace: string }) {
  const t = useT();
  const client = useQueryClient();
  const boardQuery = useBoard(workspace);
  const { query: repos, repos: wsRepos, repo, chooseRepo } = useTicketRepos(workspace);
  const [closedShown, showClosed] = useStored("branvil.board.includeClosed");
  const includeClosed = closedShown === "true";
  const state = includeClosed ? "all" : "open";
  const cardsQuery = useCards(workspace, state);
  const [filter, setFilter] = useState<CardFilter>({ assignee: "", label: "", text: "" });
  const [dragging, setDragging] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<{ lane: Lane; raw: number } | null>(null);
  const [notice, setNotice] = useState<Notice | null>(null);
  const [modal, setModal] = useState<{ lane: Lane; title: string } | null>(null);
  const cardsKey = keys.cards(workspace, state);

  const lanes = useMemo(() => boardQuery.data?.lanes ?? [], [boardQuery.data]);
  const cards = useMemo(() => cardsQuery.data?.items ?? [], [cardsQuery.data]);
  // Who can take a ticket: the owner and the agents.
  const people = usePeople();

  const move = useMutation({
    mutationFn: ({ card, lane, index }: MoveVars) => {
      const laneWithout = cards.filter((c) => c.lane === lane && ticketRef(c) !== ticketRef(card));
      return api.tickets.place(card.repository, card.key, { lane, ...neighbors(laneWithout, index) });
    },
    onMutate: async ({ card, lane, index }) => {
      setNotice(null);
      await client.cancelQueries({ queryKey: cardsKey });
      const previous = client.getQueryData<{ items: Card[]; complete: boolean }>(cardsKey);
      if (previous) {
        client.setQueryData(cardsKey, { ...previous, items: applyMove(previous.items, lanes, ticketRef(card), lane, index) });
      }
      return { previous };
    },
    onError: (err, { card }, context) => {
      if (context?.previous) client.setQueryData(cardsKey, context.previous);
      setNotice(moveRefusal(err, card, t));
    },
    onSuccess: (_data, { card, lane }) => setNotice({ tone: "info", text: t("board.moved", { key: ticketRef(card), lane: t(`lane.${lane}`) }) }),
    onSettled: () => void client.invalidateQueries({ queryKey: keys.board(workspace) }),
  });

  // Stable, so the memoised cards do not re-render when the board does: it
  // reads the cards as they are when the move is requested.
  const { mutate } = move;
  const requestMove = useCallback(
    (card: Card, lane: Lane, index: number) => {
      const current = client.getQueryData<{ items: Card[] }>(keys.cards(workspace, state))?.items ?? [];
      if (isNoop(current, ticketRef(card), lane, index)) return;
      mutate({ card, lane, index });
    },
    [client, workspace, state, mutate],
  );

  const groups = useMemo(() => groupByLane(cards, lanes), [cards, lanes]);
  // Where each card stands in its lane (filters hide cards but do not move them).
  const positions = useMemo(() => {
    const at = new Map<Card, number>();
    for (const laneCards of groups.values()) laneCards.forEach((c, i) => at.set(c, i));
    return at;
  }, [groups]);
  const openCount = cards.filter((c) => c.state === "open").length;

  const ciUrls = useMemo(() => ciUrlsOf(cards), [cards]);

  // A drag over the same place again changes nothing (no re-render).
  const aim = (lane: Lane, raw: number) =>
    setDropTarget((prev) => (prev?.lane === lane && prev.raw === raw ? prev : { lane, raw }));

  if (boardQuery.isPending) return <Loading />;
  if (boardQuery.isError) return <ErrorMessage error={boardQuery.error} />;

  const onDrop = (lane: Lane, e: DragEvent) => {
    e.preventDefault();
    const id = dragging ?? e.dataTransfer.getData("text/plain");
    const card = cards.find((c) => ticketRef(c) === id);
    const raw = dropTarget?.lane === lane ? dropTarget.raw : (groups.get(lane)?.length ?? 0);
    setDragging(null);
    setDropTarget(null);
    if (!card) return;
    requestMove(card, lane, resolveDropIndex(groups.get(lane) ?? [], id, raw));
  };

  return (
    <div className="workspace-fill">
      <PageHeader
        title={t("board.title")}
        meta={
          <Text size="sm" c="dimmed">
            {t("board.summary", { workspace: boardQuery.data.name, count: openCount })}
          </Text>
        }
      />

      <CardFilters
        cards={cards}
        people={people}
        filter={filter}
        onFilter={setFilter}
        includeClosed={includeClosed}
        onIncludeClosed={(include) => showClosed(String(include))}
        status={
          <>
            {move.isPending && t("board.moving")}
            {dragging && t("board.dragging")}
            {cardsQuery.isFetching && !cardsQuery.isPending && t("common.refreshing")}
          </>
        }
      />
      {notice &&
        (notice.tone === "error" ? (
          <Refusal>{notice.text}</Refusal>
        ) : (
          <Text size="sm" role="status">
            {notice.text}
          </Text>
        ))}
      {repos.isError && <ErrorMessage error={repos.error} />}

      {cardsQuery.isPending ? (
        <Loading />
      ) : cardsQuery.isError ? (
        <ErrorMessage error={cardsQuery.error} />
      ) : (
        <div className="lanes">
          {lanes.map((lane) => {
            const laneCards = groups.get(lane) ?? [];
            const visible = laneCards.filter((c) => matchesFilter(c, filter));
            const isTarget = dragging !== null && dropTarget?.lane === lane;
            return (
              <section
                key={lane}
                className={`lane${isTarget ? " drop-target" : ""}`}
                aria-labelledby={`lane-${lane}`}
                onDragOver={(e) => {
                  if (!dragging) return;
                  e.preventDefault();
                  if (e.target === e.currentTarget) aim(lane, laneCards.length);
                }}
                onDrop={(e) => onDrop(lane, e)}
              >
                <h2 className="lane-head" id={`lane-${lane}`}>
                  {t(`lane.${lane}`)}{" "}
                  <Badge color="neutral" variant="light" size="sm">
                    {visible.length}
                  </Badge>
                </h2>
                <ol
                  className="lane-cards"
                  onDragOver={(e) => {
                    if (!dragging) return;
                    e.preventDefault();
                    if (e.target === e.currentTarget) aim(lane, laneCards.length);
                  }}
                >
                  {visible.length === 0 ? (
                    <li className="lane-empty">{laneCards.length === 0 ? t("board.emptyLane") : t("board.noMatches")}</li>
                  ) : (
                    visible.map((card) => {
                      const id = ticketRef(card);
                      const pos = positions.get(card) ?? 0;
                      return (
                        <li
                          key={id}
                          draggable={!move.isPending}
                          className={[dragging === id ? "dragging" : "", isTarget && dropTarget?.raw === pos ? "insert-before" : ""].join(" ")}
                          onDragStart={(e) => {
                            e.dataTransfer.setData("text/plain", id);
                            e.dataTransfer.effectAllowed = "move";
                            setDragging(id);
                          }}
                          onDragEnd={() => {
                            setDragging(null);
                            setDropTarget(null);
                          }}
                          onDragOver={(e) => {
                            if (!dragging) return;
                            e.preventDefault();
                            e.stopPropagation();
                            const rect = e.currentTarget.getBoundingClientRect();
                            const after = e.clientY > rect.top + rect.height / 2;
                            aim(lane, after ? pos + 1 : pos);
                          }}
                        >
                          <BoardCard
                            card={card}
                            lanes={lanes}
                            position={pos}
                            laneSize={laneCards.length}
                            busy={move.isPending && move.variables !== undefined && ticketRef(move.variables.card) === id}
                            loadChecks={ciUrls}
                            people={people}
                            onMove={requestMove}
                          />
                        </li>
                      );
                    })
                  )}
                </ol>
                <LaneAdd
                  workspace={workspace}
                  lane={lane}
                  repos={wsRepos}
                  repo={repo}
                  onRepo={chooseRepo}
                  onMore={(l, title) => setModal({ lane: l, title })}
                />
              </section>
            );
          })}
        </div>
      )}
      <NewTicketModal
        workspace={workspace}
        lanes={lanes}
        start={modal}
        repos={wsRepos}
        repo={repo}
        onRepo={chooseRepo}
        onClose={() => setModal(null)}
      />
    </div>
  );
}
