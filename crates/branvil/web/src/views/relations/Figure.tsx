// The relations figure: the levels of cards and frames laid out by CSS, the
// lines between them drawn over it in SVG, and the drags that relate and order tickets.

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type DragEvent, type PointerEvent, type RefObject } from "react";
import { ticketRef, type Card, type Lane } from "../../api/types";
import type { Person } from "../../components/PeoplePicker";
import { useT } from "../../i18n";
import { ChildrenToggle } from "../board/CardView";
import { matchesFilter, type CardFilter } from "../board/ordering";
import { linkEdit, parentEdit, unlinkEdit, type Edit } from "./edits";
import { hasRelations, type Graph, type GraphEdge, type Level } from "./graph";
import { RelationCard, type Addition } from "./RelationCard";

/** How long a gap the card could not go into stays marked after the drag: the dragged card hid it. */
const REFUSAL_MS = 4000;

/** The drop target meaning "the figure's background": out of every parent. */
const BACKGROUND = "\u0000background";

/** The anchor of the frame of new tickets, a drop target as a card's. */
const FRESH = "\u0000fresh";

/** Where in its lane a card is put: just before or after another ticket. */
type Place = { before: string } | { after: string };

/** Where a drop goes: onto a card or frame, or the background (`ref`), or beside a card among its siblings (`side`). */
interface Aim {
  ref: string;
  side?: "before" | "after";
  /** What the drop does (a lane: the card moved to its end); null beside a card where it cannot go (only among tickets of its own lane). */
  drop: Edit | Place | { lane: Lane } | null;
  /** Beside a card where it cannot go: the lanes of the tickets around that gap, the only ones that can go there. */
  refused?: Lane[];
}

/** What the figure is drawn with and reports back. */
interface Props {
  graph: Graph;
  cards: Card[];
  lanes: Lane[];
  people: Person[];
  loadChecks: Set<string>;
  filter: CardFilter;
  /** The ticket whose lane is being changed. */
  moving: string | null;
  /** Whether relations may be changed (false while the tickets shown are incomplete or a change is being written). */
  editable: boolean;
  onToggle: (ref: string, open: boolean) => void;
  onLane: (card: Card, lane: Lane) => void;
  /** Puts a card in its lane just before or after another ticket. */
  onPlace: (card: Card, at: Place) => void;
  onEdit: (edit: Edit) => void;
  onAdd: (card: Card, addition: Addition) => void;
  /** A ticket to bring into view once the figure shows it (a new one); `onRevealed` is called then. */
  reveal: string | null;
  onRevealed: () => void;
  freshOpen: boolean;
  onFreshOpen: (open: boolean) => void;
}

const edgeKey = (e: GraphEdge) => `${e.from}>${e.to}`;

export function Figure({ graph, cards, lanes, people, loadChecks, filter, moving, editable, onToggle, onLane, onPlace, onEdit, onAdd, reveal, onRevealed, freshOpen, onFreshOpen }: Props) {
  const t = useT();
  const scrollRef = useRef<HTMLDivElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const linkingRef = useRef(false);
  const startLinkRef = useRef<(card: Card, e: PointerEvent<HTMLElement>) => void>(() => {});
  const draggedRef = useRef<Element | null>(null);
  const [dragging, setDragging] = useState<Card | null>(null);
  const [target, setTarget] = useState<Aim | null>(null);
  const byRef = useMemo(() => new Map(cards.map((c) => [ticketRef(c), c])), [cards]);

  // A ticket asked for is scrolled to once the figure shows it, and its card glows for a moment;
  // the frame of new tickets is opened when it is among them.
  useEffect(() => {
    const inFresh = graph.level[0]?.some((n) => "fresh" in n && n.fresh.some((c) => ticketRef(c) === reveal));
    if (reveal && !freshOpen && inFresh) return onFreshOpen(true);
    const el = reveal && [...(contentRef.current?.querySelectorAll("[data-ticket]") ?? [])].find((e) => e.getAttribute("data-ticket") === reveal);
    if (!el) return;
    el.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
    el.querySelector(":scope > .rel-card > .card")?.animate?.(
      [{ boxShadow: "0 0 0 4px var(--color-brand)" }, { boxShadow: "0 0 0 4px transparent" }],
      { duration: 2000, easing: "ease-out" },
    );
    onRevealed();
  }, [reveal, graph, freshOpen, onRevealed, onFreshOpen]);

  // A gap the card could not go into stays marked for a while after the drag.
  useEffect(() => {
    if (dragging || !target) return;
    const timer = setTimeout(() => setTarget(null), REFUSAL_MS);
    return () => clearTimeout(timer);
  }, [dragging, target]);

  const onLinkStart = useCallback((card: Card, e: PointerEvent<HTMLElement>) => startLinkRef.current(card, e), []);

  // Cards are put level with what they wait for, and lines follow the cards:
  // done once the layout is and whenever a card or the figure changes size.
  useLayoutEffect(() => {
    const scroll = scrollRef.current;
    const content = contentRef.current;
    if (!scroll || !content) return;
    const draw = () => {
      // A line meets a ticket's own card: an open parent's is its frame's header, as wide as the frame.
      const cardOf = (el: Element) => (el.querySelector(":scope > .rel-card") ?? el).getBoundingClientRect();
      const anchors = new Map([...content.querySelectorAll<HTMLElement>("[data-ticket]")].map((el) => [el.dataset["ticket"] as string, el]));
      // In document order, so what a ticket waits for (in a column to its left) already stands where it stays:
      // a ticket is pushed down so its card's middle meets the topmost of their middles, its column's order kept.
      // Only what comes before it counts: a cycle would push its tickets down without end.
      for (const [ref, el] of anchors) {
        const card = cardOf(el);
        const middles = graph.edges.flatMap((e) => {
          const from = e.to === ref ? anchors.get(e.from) : undefined;
          if (!from || !(from.compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING)) return [];
          const r = cardOf(from);
          return [r.top + r.height / 2];
        });
        const natural = card.top - (parseFloat(el.style.marginTop) || 0);
        const push = middles.length > 0 ? Math.round(Math.min(...middles) - card.height / 2 - natural) : 0;
        el.style.marginTop = push > 0 ? `${push}px` : "";
      }
      const origin = content.getBoundingClientRect();
      const rects = new Map([...anchors].map(([ref, el]) => [ref, cardOf(el)]));
      for (const g of content.querySelectorAll<SVGGElement>("g[data-from]")) {
        const a = rects.get(g.dataset["from"] as string);
        const b = rects.get(g.dataset["to"] as string);
        if (!a || !b) continue;
        const [x1, y1] = [a.right - origin.left, a.top + a.height / 2 - origin.top];
        const [x2, y2] = [b.left - origin.left, b.top + b.height / 2 - origin.top];
        const d = curve(x1, y1, x2, y2);
        for (const p of g.querySelectorAll("path")) p.setAttribute("d", d);
        const remove = g.querySelector("foreignObject");
        remove?.setAttribute("x", String(x2 - 38));
        remove?.setAttribute("y", String(y2 - 12));
      }
    };
    draw();
    const observer = new ResizeObserver(draw);
    observer.observe(scroll);
    observer.observe(content);
    for (const el of content.querySelectorAll("[data-ticket]")) observer.observe(el);
    return () => observer.disconnect();
  }, [graph, freshOpen]);

  // Near the top or bottom of a sibling, a drop puts the card into that gap:
  // the board's order is the order the figure shows, so only a gap next to a
  // ticket of its own lane takes it. Elsewhere a drop target is a card or
  // frame (`data-ticket`) or, inside the figure, its background. A drop is
  // offered only where it changes something. Only a ticket without relations
  // is new: a new one dropped on the background (out of the frame of new
  // tickets) is ready, and one without relations dropped onto the frame new.
  const aimFor = (dragged: Card, e: DragEvent): Aim | null => {
    const el = e.target instanceof Element ? e.target.closest("[data-ticket]") : null;
    const from = draggedRef.current;
    const refOf = (n: Element | null) => n?.getAttribute("data-ticket") ?? "";
    if (el && from && el !== from && el.parentElement === from.parentElement && refOf(el) !== FRESH) {
      const box = el.getBoundingClientRect();
      const band = (el.querySelector(":scope > .rel-card") ?? el).getBoundingClientRect().height / 4;
      const side = e.clientY < box.top + band ? "before" : e.clientY > box.bottom - band ? "after" : null;
      if (side) {
        const [above, below] = side === "before" ? [el.previousElementSibling, el] : [el, el.nextElementSibling];
        if (above === from || below === from) return null;
        const inLane = (n: Element | null) => byRef.get(refOf(n))?.lane === dragged.lane;
        const drop = inLane(below) ? { before: refOf(below) } : inLane(above) ? { after: refOf(above) } : null;
        const lanes = [above, below].flatMap((n) => byRef.get(refOf(n))?.lane ?? []);
        return drop ? { ref: refOf(el), side, drop } : { ref: refOf(el), side, drop, refused: [...new Set(lanes)] };
      }
    }
    const ref = el ? refOf(el) : e.target instanceof Node && canvasRef.current?.contains(e.target) ? BACKGROUND : null;
    if (ref === null) return null;
    if (ref === FRESH) return !hasRelations(dragged) && dragged.lane !== "new" ? { ref, drop: { lane: "new" } } : null;
    const over = ref === BACKGROUND ? null : byRef.get(ref);
    if (over === undefined) return null;
    const drop = parentEdit(dragged, over) ?? (!over && dragged.lane === "new" ? { lane: "ready" as const } : null);
    return drop && { ref, drop };
  };
  const dragProps = (card: Card) => ({
    draggable: editable,
    onDragStart: (e: DragEvent<HTMLElement>) => {
      e.stopPropagation();
      if (linkingRef.current) {
        e.preventDefault();
        return;
      }
      e.dataTransfer.setData("text/plain", ticketRef(card));
      e.dataTransfer.effectAllowed = "move";
      draggedRef.current = e.currentTarget.closest("[data-ticket]");
      setDragging(card);
    },
    onDragEnd: () => {
      setDragging(null);
      setTarget((aim) => (aim?.refused ? aim : null));
    },
  });
  const onDragOver = (e: DragEvent) => {
    if (!dragging) return;
    const aim = aimFor(dragging, e);
    if (aim?.drop) e.preventDefault();
    setTarget((prev) => (prev?.ref === aim?.ref && prev?.side === aim?.side && !prev?.refused === !aim?.refused ? prev : aim));
  };
  const onDrop = (e: DragEvent) => {
    if (!dragging) return;
    e.preventDefault();
    const drop = aimFor(dragging, e)?.drop;
    setDragging(null);
    setTarget(null);
    if (!drop) return;
    if ("changes" in drop) onEdit(drop);
    else if ("lane" in drop) onLane(dragging, drop.lane);
    else onPlace(dragging, drop);
  };

  const node = (card: Card, isOpen: boolean | undefined) => (
    <RelationCard
      busy={moving === ticketRef(card)}
      card={card}
      lanes={lanes}
      people={people}
      loadChecks={loadChecks}
      open={isOpen}
      dim={!matchesFilter(card, filter)}
      onToggle={onToggle}
      onLane={onLane}
      onLinkStart={onLinkStart}
      onAdd={onAdd}
    />
  );
  const anchorClass = (base: string, ref: string) =>
    [
      base,
      target?.ref === ref && (target.side ? `insert-${target.side}${target.refused ? " refused" : ""}` : "drop-target"),
      dragging && ticketRef(dragging) === ref && "dragging",
    ]
      .filter(Boolean)
      .join(" ");
  // An anchor is a card, or an open parent's frame: its card as the header, then its children's level.
  // Opening keeps the card where it was in the tree, so it keeps the focus.
  const anchor = (card: Card, inner: Level | null, isOpen: boolean | undefined) => {
    const ref = ticketRef(card);
    return (
      <div key={ref} className={anchorClass(inner ? "rel-frame" : "rel-node", ref)} data-ticket={ref}>
        <div className="rel-card" {...dragProps(card)}>
          {node(card, isOpen)}
        </div>
        {target?.ref === ref && target.refused && (
          <span className={`rel-refused ${target.side}`} role="status">
            {t("relations.lanesOnly", {
              lanes: target.refused.map((lane) => t(`lane.${lane}`)).join(t("relations.or")),
            })}
          </span>
        )}
        {inner && level(inner)}
      </div>
    );
  };
  // The new tickets without relations: a frame as an open parent's, its header saying what it holds.
  const freshFrame = (fresh: Card[]) => {
    const label = t("relations.fresh", { count: fresh.length });
    return (
      <div key={FRESH} className={anchorClass(freshOpen ? "rel-frame" : "rel-node", FRESH)} data-ticket={FRESH} role="group" aria-label={label}>
        <div className="rel-card">
          <div className="card fresh">
            <div className="card-row">
              <ChildrenToggle open={freshOpen} onToggle={() => onFreshOpen(!freshOpen)} />
              <span className="title">{label}</span>
            </div>
          </div>
        </div>
        {freshOpen && level([fresh.map((card) => ({ card, ref: ticketRef(card), hasChildren: false, inner: null }))])}
      </div>
    );
  };
  const level = (l: Level) => (
    <div className="rel-level">
      {l.map((column, i) => (
        <div className="rel-column" key={i}>
          {column.map((n) => ("fresh" in n ? freshFrame(n.fresh) : anchor(n.card, n.inner, n.hasChildren ? n.inner !== null : undefined)))}
        </div>
      ))}
    </div>
  );

  return (
    <div className="relations-scroll" ref={scrollRef}>
      <div
        ref={contentRef}
        className={`relations-content${dragging ? " relating" : ""}`}
        onDragOver={onDragOver}
        onDrop={onDrop}
      >
        <div ref={canvasRef} className={`relations-canvas${target?.ref === BACKGROUND ? " drop-target" : ""}`}>
          {graph.level.length === 0 ? <p className="relations-empty">{t("relations.empty")}</p> : level(graph.level)}
        </div>
        <Lines edges={graph.edges} editable={editable} onEdit={onEdit} />
        <LinkDraft contentRef={contentRef} startRef={startLinkRef} linkingRef={linkingRef} byRef={byRef} editable={editable} onEdit={onEdit} />
      </div>
    </div>
  );
}

/** A horizontal S-curve from the right of one box to the left of another. */
function curve(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(40, Math.abs(x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

/** The arrowhead of a layer's lines (each layer colours its own). */
const arrow = (id: string) => (
  <defs>
    <marker id={id} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M 0 0 L 10 5 L 0 10 z" />
    </marker>
  </defs>
);

/**
 * The lines, behind the cards so the cards stay readable: they show in the gaps
 * between them. A line hovered there (or whose "×" has the focus) is drawn
 * again in front of the cards, so it can be followed across them. Its own
 * state, so a hover re-renders only the lines.
 */
function Lines({ edges, editable, onEdit }: { edges: GraphEdge[]; editable: boolean; onEdit: (edit: Edit) => void }) {
  const t = useT();
  const [front, setFront] = useState<string | null>(null);
  return (
    <>
      <svg className="rel-lines" role="group" aria-label={t("relations.title")}>
        {arrow("rel-arrow")}
        {edges.map((e) => {
          const key = edgeKey(e);
          const show = () => setFront(key);
          const hide = () => setFront((k) => (k === key ? null : k));
          return (
            <g
              key={key}
              className={`rel-edge${e.dashed ? " dashed" : ""}`}
              data-from={e.from}
              data-to={e.to}
              onPointerEnter={show}
              onPointerLeave={hide}
              onFocus={show}
              onBlur={hide}
            >
              <path className="rel-edge-line" markerEnd="url(#rel-arrow)" />
              <path className="rel-edge-hit" />
              {e.blocker && e.blocked && (
                // Just before the arrow, in the gap left of its card: never under a card.
                <foreignObject width="24" height="24">
                  <button
                    type="button"
                    className="rel-edge-remove"
                    aria-label={t("relations.unlink", { blocked: e.to, blocker: e.from })}
                    title={t("relations.unlink", { blocked: e.to, blocker: e.from })}
                    disabled={!editable}
                    onClick={() => onEdit(unlinkEdit(e.blocker as Card, e.blocked as Card))}
                  >
                    ×
                  </button>
                </foreignObject>
              )}
            </g>
          );
        })}
      </svg>
      <svg className="rel-front" aria-hidden>
        {arrow("rel-arrow-front")}
        {edges.map((e) => (
          <g key={edgeKey(e)} className={`rel-edge${e.dashed ? " dashed" : ""}${front === edgeKey(e) ? " front" : ""}`} data-from={e.from} data-to={e.to}>
            <path className="rel-edge-line" markerEnd="url(#rel-arrow-front)" />
          </g>
        ))}
      </svg>
    </>
  );
}

interface Draft {
  from: Card;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  /** The card under the pointer that the line can be dropped on. */
  to: Card | null;
}

/**
 * The line being drawn from a card's handle to the pointer. Its state is its
 * own, so following the pointer re-renders only it; the figure calls it through `startRef`.
 */
function LinkDraft({
  contentRef,
  startRef,
  linkingRef,
  byRef,
  editable,
  onEdit,
}: {
  contentRef: RefObject<HTMLDivElement | null>;
  startRef: RefObject<(card: Card, e: PointerEvent<HTMLElement>) => void>;
  linkingRef: RefObject<boolean>;
  byRef: Map<string, Card>;
  editable: boolean;
  onEdit: (edit: Edit) => void;
}) {
  const t = useT();
  const [draft, setDraft] = useState<Draft | null>(null);
  const stopRef = useRef<() => void>(() => {});

  useLayoutEffect(() => {
    startRef.current = (from, e) => {
      const content = contentRef.current;
      if (!editable || !content || e.button !== 0) return;
      const at = (x: number, y: number) => {
        const origin = content.getBoundingClientRect();
        return [x - origin.left, y - origin.top] as const;
      };
      const handle = e.currentTarget.getBoundingClientRect();
      const [x1, y1] = at(handle.left + handle.width / 2, handle.top + handle.height / 2);
      const origin = e.currentTarget.closest("[data-ticket]");
      // Not its own frame or a card in its frame: a dependency between a parent and its descendant is not drawn.
      const toUnder = (target: EventTarget | null) => {
        const el = target instanceof Element ? target.closest("[data-ticket]") : null;
        if (!el || !origin || el.contains(origin) || origin.contains(el)) return null;
        const over = byRef.get(el.getAttribute("data-ticket") ?? "");
        return over && linkEdit(from, over) ? over : null;
      };
      const move = (ev: globalThis.PointerEvent) => {
        const [x2, y2] = at(ev.clientX, ev.clientY);
        setDraft({ from, x1, y1, x2, y2, to: toUnder(ev.target) });
      };
      const up = (ev: globalThis.PointerEvent) => {
        const to = toUnder(ev.target);
        stopRef.current();
        const edit = to && linkEdit(from, to);
        if (edit) onEdit(edit);
      };
      stopRef.current = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        window.removeEventListener("pointercancel", stopRef.current);
        content.classList.remove("relating");
        linkingRef.current = false;
        setDraft(null);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
      window.addEventListener("pointercancel", stopRef.current);
      // Lines under the pointer would hide the cards from it.
      content.classList.add("relating");
      linkingRef.current = true;
      setDraft({ from, x1, y1, x2: x1, y2: y1, to: null });
    };
  }, [startRef, contentRef, linkingRef, byRef, editable, onEdit]);
  useEffect(() => () => stopRef.current(), []);

  if (!draft) return null;
  return (
    <>
      <svg className="rel-draft" aria-hidden>
        {arrow("rel-arrow-draft")}
        <path d={curve(draft.x1, draft.y1, draft.x2, draft.y2)} markerEnd="url(#rel-arrow-draft)" />
      </svg>
      {draft.to && (
        <div className="rel-draft-hint" role="status" style={{ left: draft.x2 + 14, top: draft.y2 + 14 }}>
          {t("relations.waits", { blocked: ticketRef(draft.to), blocker: ticketRef(draft.from) })}
        </div>
      )}
    </>
  );
}
