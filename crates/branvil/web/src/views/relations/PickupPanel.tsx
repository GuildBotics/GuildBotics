// The pickup order beside the task flow: an agent's ready tickets in the order
// it takes them up, reordered by dragging a row or with the keyboard.

import { ActionIcon, Badge, Group, NativeSelect, Text } from "@mantine/core";
import { X } from "lucide-react";
import { useEffect, useRef, useState, type DragEvent, type KeyboardEvent } from "react";
import { ticketRef, type Card } from "../../api/types";
import { type Translate, useT } from "../../i18n";
import { arrange, nextPickup, stepped, type PickupRow, type Stuck } from "./pickup";

/** A row being moved: the order it started from, where it would go, and the Ready lane then (for the undo). */
interface Moving {
  moved: string;
  start: string[];
  /** Just before which row it goes (null: last); undefined until it is moved, where it stays as it was. */
  before: string | null | undefined;
  lane: string[];
  keyboard: boolean;
}

/** The order a move shows, and what it moves. */
const arranged = (m: Moving, cards: readonly Card[]) => (m.before === undefined ? { order: m.start, group: [] } : arrange(m.start, m.moved, m.before, cards));

/** A row put somewhere else: what moved, in which new order, and the Ready lane before. */
export interface Drop {
  moved: string;
  order: string[];
  group: string[];
  lane: string[];
}

/** Why a ticket will not start: each ticket it waits for and what keeps that one. */
export const reasonText = (stuck: readonly Stuck[], t: Translate) =>
  stuck.map((r) => t(`pickup.why.${r.why}`, { ticket: r.ticket })).join(t("pickup.and"));

export function PickupPanel({
  assignees,
  assignee,
  rows,
  current,
  cards,
  hidden,
  disabled,
  onAssignee,
  onClose,
  onHighlight,
  onPoint,
  onPreview,
  onDrop,
}: {
  /** The assignees with a pickup order, as named in the chooser. */
  assignees: { value: string; label: string }[];
  assignee: string;
  rows: PickupRow[];
  /** The row of the card whose tag opened the panel. */
  current: string | null;
  /** The board's cards: what the moved ticket waits for, and the Ready lane. */
  cards: Card[];
  /** Whether the filters hide a ticket from the figure. */
  hidden: (ref: string) => boolean;
  /** The order cannot be changed (it is being written, or the cards are incomplete). */
  disabled: boolean;
  onAssignee: (name: string) => void;
  onClose: () => void;
  onHighlight: (ref: string | null) => void;
  onPoint: (ref: string) => void;
  /** The rows' order while one is moved (shown before it is written); null when the move ends. */
  onPreview: (order: string[] | null) => void;
  onDrop: (drop: Drop) => void;
}) {
  const t = useT();
  const next = nextPickup(rows);
  const [moving, setMovingState] = useState<Moving | null>(null);
  const movingRef = useRef<Moving | null>(null);
  const [said, setSaid] = useState("");
  const rowEls = useRef(new Map<string, HTMLLIElement>());
  const setMoving = (m: Moving | null) => {
    movingRef.current = m;
    setMovingState(m);
    onPreview(m && arranged(m, cards).order);
  };

  // The row of the card whose tag was clicked is scrolled to.
  useEffect(() => {
    if (current) rowEls.current.get(current)?.scrollIntoView({ block: "nearest" });
  }, [current, assignee]);
  // A row moved with the keyboard keeps the focus as the rows reorder.
  useEffect(() => {
    if (moving?.keyboard) rowEls.current.get(moving.moved)?.focus();
  }, [rows, moving]);

  const start = (moved: string, keyboard: boolean): Moving => {
    const order = rows.map((r) => r.ref);
    const lane = cards.filter((c) => c.state === "open" && c.lane === "ready").map(ticketRef);
    return { moved, start: order, before: undefined, lane, keyboard };
  };
  const positionOf = (m: Moving) => {
    const order = arranged(m, cards).order;
    return { key: m.moved, position: order.indexOf(m.moved) + 1, count: order.length };
  };
  const cancel = () => {
    const m = movingRef.current;
    if (m?.keyboard) setSaid(t("pickup.cancelled", { key: m.moved }));
    setMoving(null);
  };
  const drop = () => {
    const m = movingRef.current;
    if (!m) return;
    const { order, group } = arranged(m, cards);
    if (m.keyboard) setSaid(t("pickup.dropped", positionOf(m)));
    movingRef.current = null;
    setMovingState(null);
    // Unchanged: nothing to write, and nothing to show but the order as it was.
    if (order.every((r, i) => r === m.start[i])) onPreview(null);
    else onDrop({ moved: m.moved, order, group, lane: m.lane });
  };
  /** Moves the row being moved before another of the rows it started among (null: last). */
  const aim = (before: string | null) => {
    const m = movingRef.current;
    if (!m || m.before === before) return m;
    const next = { ...m, before };
    setMoving(next);
    return next;
  };

  const onKeyDown = (ref: string) => (e: KeyboardEvent<HTMLLIElement>) => {
    const m = movingRef.current;
    if (e.key === " ") {
      e.preventDefault();
      if (disabled) return;
      if (!m) {
        const lifted = start(ref, true);
        setMoving(lifted);
        setSaid(t("pickup.lifted", positionOf(lifted)));
      } else if (m.moved === ref) drop();
      return;
    }
    if (!m?.keyboard) return;
    if (e.key === "Escape") {
      // Cancels the move only, not the panel.
      e.preventDefault();
      e.stopPropagation();
      cancel();
    } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      e.preventDefault();
      const here = m.before === undefined ? (m.start[m.start.indexOf(m.moved) + 1] ?? null) : m.before;
      const to = stepped(m.start, m.moved, here, e.key === "ArrowUp" ? -1 : 1, cards);
      setSaid(t("pickup.position", positionOf((to !== undefined && aim(to)) || m)));
    }
  };
  const onDragOver = (ref: string) => (e: DragEvent<HTMLLIElement>) => {
    const m = movingRef.current;
    if (!m || m.keyboard) return;
    e.preventDefault();
    e.stopPropagation();
    // The moved row, or one it pulls along: aiming there would show the order it shows now.
    if (ref === m.moved || arranged(m, cards).group.includes(ref)) return;
    const box = e.currentTarget.getBoundingClientRect();
    const rest = m.start.filter((r) => r !== m.moved);
    aim(e.clientY < box.top + box.height / 2 ? ref : (rest[rest.indexOf(ref) + 1] ?? null));
  };

  return (
    <aside className="pickup-panel" aria-label={t("pickup.title")}>
      <Group className="pickup-head" justify="space-between" wrap="nowrap" gap="xs">
        <Text fw={600} size="sm">
          {t("pickup.title")}
        </Text>
        <ActionIcon variant="subtle" color="gray" size="sm" aria-label={t("pickup.close")} title={t("pickup.close")} onClick={onClose}>
          <X size={16} />
        </ActionIcon>
      </Group>
      <NativeSelect
        className="pickup-assignee"
        size="xs"
        label={t("pickup.assignee")}
        value={assignee}
        data={assignees}
        disabled={moving !== null}
        onChange={(e) => onAssignee(e.currentTarget.value)}
      />
      {rows.length === 0 ? (
        <Text size="sm" c="dimmed" className="pickup-empty">
          {t("pickup.empty")}
        </Text>
      ) : (
        <ol
          className="pickup-list"
          aria-label={t("pickup.list", { name: assignees.find((a) => a.value === assignee)?.label ?? assignee })}
          onDragOver={(e) => {
            if (!movingRef.current || movingRef.current.keyboard) return;
            e.preventDefault();
            // Below the last row: last. Between rows, it stays where it was aimed.
            const last = e.currentTarget.lastElementChild?.getBoundingClientRect();
            if (e.target === e.currentTarget && last && e.clientY > last.bottom) aim(null);
          }}
          onDrop={(e) => {
            if (!movingRef.current) return;
            e.preventDefault();
            drop();
          }}
        >
          {rows.map((row) => {
            const parent = row.card.parent;
            const away = hidden(row.ref);
            return (
              <li
                key={row.ref}
                ref={(el) => {
                  if (el) rowEls.current.set(row.ref, el);
                  else rowEls.current.delete(row.ref);
                }}
                className={[
                  "pickup-row",
                  row.ref === current && "current",
                  moving?.moved === row.ref && "moving",
                ]
                  .filter(Boolean)
                  .join(" ")}
                tabIndex={0}
                draggable={!disabled}
                aria-current={row.ref === current ? "true" : undefined}
                onMouseEnter={() => onHighlight(away ? null : row.ref)}
                onMouseLeave={() => onHighlight(null)}
                onFocus={() => onHighlight(away ? null : row.ref)}
                onBlur={() => onHighlight(null)}
                onClick={() => !away && onPoint(row.ref)}
                onKeyDown={onKeyDown(row.ref)}
                onDragStart={(e) => {
                  e.dataTransfer.setData("text/plain", row.ref);
                  e.dataTransfer.effectAllowed = "move";
                  setMoving(start(row.ref, false));
                }}
                onDragOver={onDragOver(row.ref)}
                onDragEnd={() => movingRef.current && cancel()}
              >
                <span className="pickup-number">{row.n}</span>
                {row.stuck.length > 0 && <span className="pickup-stuck">{t("pickup.stuck", { reason: reasonText(row.stuck, t) })}</span>}
                {row.stuck.length === 0 && row.waits.length > 0 && (
                  <span className="pickup-waits">
                    {t("pickup.waits", {
                      blockers: row.waits.map((w) => t("pickup.waitFor", { ticket: w.ticket }) + (
                        "lane" in w && w.assignee
                          ? t("pickup.waitDetails", { assignee: w.assignee, lane: t(`lane.${w.lane}`) })
                          : ""
                      )).join(t("pickup.and")),
                    })}
                  </span>
                )}
                {row === next && (
                  <Badge size="xs" variant="light">{t("pickup.next")}</Badge>
                )}
                <span className="pickup-ticket">
                  <span className="key">{row.ref}</span> {row.card.title}
                </span>
                {parent && (
                  <span className="pickup-parent">
                    {t("pickup.parent", { ticket: ticketRef(parent) })} {parent.title}
                  </span>
                )}
                {away && <span className="pickup-hidden">{t("pickup.filtered")}</span>}
              </li>
            );
          })}
        </ol>
      )}
      <p className="visually-hidden" aria-live="polite">
        {said}
      </p>
    </aside>
  );
}
