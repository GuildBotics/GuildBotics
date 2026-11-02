import { ActionIcon, Menu } from "@mantine/core";
import { ArrowRight, CornerDownRight, CornerLeftUp, Ellipsis } from "lucide-react";
import { memo, type PointerEvent } from "react";
import { ticketRef, type Card, type Lane } from "../../api/types";
import type { Person } from "../../components/PeoplePicker";
import { useT } from "../../i18n";
import { CardView, ChildrenToggle, CopyUrlItem, DeleteCardItem, InteractiveItem, LaneButton, LazyMenu } from "../board/CardView";

/** A ticket made from a card's menu: its parent, a child of it, or the next one, which waits for it. */
export type Addition = "parent" | "child" | "next";

/**
 * A card of the relations page: the board's card with its lane and assignee
 * changed in place, its children opened or closed, a handle that draws a
 * dependency, and a menu that makes a related ticket. Memoised: the page passes stable props, so a drag over the
 * figure does not re-render it.
 */
export const RelationCard = memo(function RelationCard({
  card,
  lanes,
  people,
  loadChecks,
  open,
  dim,
  busy,
  onToggle,
  onLane,
  onLinkStart,
  onAdd,
}: {
  card: Card;
  lanes: Lane[];
  people: Person[];
  loadChecks: Set<string>;
  /** Whether its children are shown; undefined for a card without children in the figure. */
  open: boolean | undefined;
  /** A nonmatching ancestor kept as context for a matching descendant. */
  dim: boolean;
  /** Its lane is being changed. */
  busy: boolean;
  onToggle: (ref: string, open: boolean) => void;
  onLane: (card: Card, lane: Lane) => void;
  onLinkStart: (card: Card, e: PointerEvent<HTMLElement>) => void;
  onAdd: (card: Card, addition: Addition) => void;
}) {
  const t = useT();
  return (
    <CardView
      card={card}
      people={people}
      loadChecks={loadChecks}
      busy={busy}
      // A parent has the colour of the header it becomes once opened, also while none of its children is shown.
      className={[card.children.length > 0 && "parent", dim && "dim"].filter(Boolean).join(" ")}
      lane={<LaneButton card={card} lanes={lanes} onLane={onLane} disabled={busy} />}
      lead={open === undefined ? undefined : <ChildrenToggle open={open} onToggle={() => onToggle(ticketRef(card), !open)} />}
    >
      <span
        className="rel-handle"
        title={t("relations.linkHandle", { key: ticketRef(card) })}
        // Keeps the card's own (native) drag and text selection from starting.
        onMouseDown={(e) => e.preventDefault()}
        onPointerDown={(e) => {
          e.stopPropagation();
          onLinkStart(card, e);
        }}
      />
      <div className="card-menu">
        <LazyMenu
          trigger={(open) => (
            <ActionIcon variant="subtle" color="gray" size="sm" aria-label={t("board.cardMenu", { key: ticketRef(card) })} onClick={open}>
              <Ellipsis size={16} />
            </ActionIcon>
          )}
        >
          <Menu.Item leftSection={<CornerLeftUp size={14} />} onClick={() => onAdd(card, "parent")}>
            {t("relations.addParent")}
          </Menu.Item>
          <Menu.Item leftSection={<CornerDownRight size={14} />} onClick={() => onAdd(card, "child")}>
            {t("relations.addChild")}
          </Menu.Item>
          <Menu.Item leftSection={<ArrowRight size={14} />} onClick={() => onAdd(card, "next")}>
            {t("relations.addNext")}
          </Menu.Item>
          <Menu.Divider />
          <InteractiveItem card={card} />
          <CopyUrlItem url={card.url} />
          <DeleteCardItem card={card} />
        </LazyMenu>
      </div>
    </CardView>
  );
});
