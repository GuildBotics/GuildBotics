import { Drawer } from "@mantine/core";
import { useWindowEvent } from "@mantine/hooks";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useT } from "../../i18n";
import { TicketView } from "../TicketView";
import { TicketOpener } from "./TicketLink";

/**
 * Whether Escape belongs to where it is pressed: a field being typed in (a
 * title's edit cancels itself), or what Mantine marks as its own (an open
 * dropdown). Closing the drawer there would lose what was typed. Of inputs,
 * only those typed in have a selection (not a checkbox's chip).
 */
const ownsEscape = (el: HTMLElement) =>
  ((el instanceof HTMLTextAreaElement || (el instanceof HTMLInputElement && el.selectionStart !== null)) && !el.readOnly) ||
  el.dataset.mantineStopPropagation === "true";

/**
 * A page whose ticket links open the ticket in a drawer over it, as a check
 * opens from a check list: the page and its URL stay. A ticket linked from the
 * drawer replaces the one it shows.
 */
export function TicketDrawer({ children }: { children: ReactNode }) {
  const t = useT();
  const [ticket, setTicket] = useState<{ repo: string; key: string } | null>(null);
  const content = useRef<HTMLDivElement>(null);
  const open = useCallback((repo: string, key: string) => setTicket({ repo, key }), []);
  const close = useCallback(() => setTicket(null), []);
  // On the window, as Mantine's own, before what Escape closes there loses its mark (an open dropdown)
  // or takes the focus out of the drawer with it (an edit).
  useWindowEvent(
    "keydown",
    (e) => {
      if (ticket && e.key === "Escape" && !e.isComposing && !ownsEscape(e.target as HTMLElement)) close();
    },
    { capture: true },
  );
  // Another ticket opened in the drawer is read from its top, and the focus is
  // kept in the drawer: the link that opened it is gone.
  useEffect(() => {
    if (!ticket) return;
    content.current?.scrollTo?.(0, 0);
    content.current?.focus();
  }, [ticket]);
  return (
    <TicketOpener.Provider value={open}>
      {children}
      <Drawer.Root opened={ticket !== null} onClose={close} position="right" size={960} closeOnEscape={false}>
        <Drawer.Overlay />
        <Drawer.Content ref={content} aria-label={t("ticket.drawer")}>
          {/* The header stays in view, so the drawer can be closed from anywhere in a long ticket. */}
          <Drawer.Header>
            <Drawer.CloseButton aria-label={t("ticket.closeDrawer")} />
          </Drawer.Header>
          <Drawer.Body>{ticket && <TicketView key={`${ticket.repo}#${ticket.key}`} repo={ticket.repo} ticketKey={ticket.key} drawer />}</Drawer.Body>
        </Drawer.Content>
      </Drawer.Root>
    </TicketOpener.Provider>
  );
}
