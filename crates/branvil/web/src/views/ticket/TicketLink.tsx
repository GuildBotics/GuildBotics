import { createContext, useContext, type AnchorHTMLAttributes, type ReactNode } from "react";
import { Link } from "../../router/Router";
import { href } from "../../router/routes";

/** Opens a ticket in the drawer of the page it is linked from. */
export type OpenTicket = (repo: string, key: string) => void;

export const TicketOpener = createContext<OpenTicket | null>(null);

type TicketLinkProps = Omit<AnchorHTMLAttributes<HTMLAnchorElement>, "href"> & { repo: string; ticketKey: string; children: ReactNode };

/** A link to a ticket's page; under a page with a ticket drawer, a plain left click opens the ticket there instead. */
export function TicketLink({ repo, ticketKey, ...rest }: TicketLinkProps) {
  const open = useContext(TicketOpener);
  return <Link to={href.ticket(repo, ticketKey)} go={open ? () => open(repo, ticketKey) : undefined} {...rest} />;
}
