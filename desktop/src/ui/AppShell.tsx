import { ActionIcon, Tooltip } from "@mantine/core";
import { useMediaQuery, useWindowEvent } from "@mantine/hooks";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { createContext, useContext, useRef, useState, type ReactNode } from "react";
import { useLocation } from "react-router";
import { ownsEscape } from "./escape";
import { useStored } from "./storage";

/** The width below which the menu stays a rail and opens over the page. */
const NARROW = "(max-width: 900px)";
const STORED = "guildbotics.sidebar";

const Compact = createContext(false);

/** Whether the sidebar shows icons only, so its entries name themselves in tooltips. */
export function useSidebarCompact(): boolean {
  return useContext(Compact);
}

/**
 * Shared navigation and page layout. On a wide screen the sidebar collapses to
 * an icon rail and this browser remembers the choice; on a narrow one it is
 * always a rail whose full menu opens over the page until the next navigation.
 */
export function AppShell({
  sidebar,
  children,
  collapseLabel,
  expandLabel,
}: {
  sidebar: ReactNode;
  children: ReactNode;
  collapseLabel: string;
  expandLabel: string;
}) {
  const narrow = useMediaQuery(NARROW, false, { getInitialValueInEffect: false });
  const [stored, store] = useStored(STORED);
  const [overlay, setOverlay] = useState(false);
  const { pathname } = useLocation();
  const [shownPath, setShownPath] = useState(pathname);
  if (shownPath !== pathname || (overlay && !narrow)) {
    setShownPath(pathname);
    setOverlay(false);
  }
  const open = narrow ? overlay : stored !== "collapsed";
  const aside = useRef<HTMLElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  // The open menu is the topmost layer: the page behind it is inert, and an
  // Escape pressed in the menu (or with nothing focused) is its own, taken on
  // the window before the page's listeners and kept from them. A dialog opened
  // from the menu, an open dropdown, or a typed field keeps its own Escape.
  useWindowEvent(
    "keydown",
    (event) => {
      const target = event.target as HTMLElement;
      if (
        !overlay ||
        event.key !== "Escape" ||
        event.isComposing ||
        ownsEscape(target) ||
        (target !== document.body &&
          (!aside.current?.contains(target) || target.closest('[role="dialog"]')))
      )
        return;
      event.stopImmediatePropagation();
      setOverlay(false);
      toggle.current?.focus();
    },
    { capture: true },
  );
  const label = open ? collapseLabel : expandLabel;
  const sidebarClass = ["sidebar", !open && "compact", overlay && "overlay"].filter(Boolean);
  return (
    <Compact.Provider value={!open}>
      <main className={open && !narrow ? "shell" : "shell compact"}>
        <aside ref={aside} className={sidebarClass.join(" ")}>
          <Tooltip label={label} position="right" disabled={open}>
            <ActionIcon
              ref={toggle}
              className="sidebar-toggle"
              variant="subtle"
              color="gray"
              size="lg"
              aria-label={label}
              aria-expanded={open}
              onClick={() =>
                narrow ? setOverlay(!overlay) : store(open ? "collapsed" : "expanded")
              }
            >
              {open ? <PanelLeftClose size={18} /> : <PanelLeftOpen size={18} />}
            </ActionIcon>
          </Tooltip>
          {sidebar}
        </aside>
        {overlay && <div className="sidebar-backdrop" onClick={() => setOverlay(false)} />}
        <section className="workspace" inert={overlay}>
          {children}
        </section>
      </main>
    </Compact.Provider>
  );
}
