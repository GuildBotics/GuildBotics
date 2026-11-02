// A minimal History API router: the GUI has few routes and its URL grammar
// (`/{path}/-/{kind}/{id}`) is parsed by routes.ts.

import { useEffect, useRef, useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent, type ReactNode } from "react";
import { inAppPath } from "./routes";

const listeners = new Set<() => void>();

function notify() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0) {
    window.addEventListener("popstate", notify);
    window.addEventListener("hashchange", notify);
  }
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0) {
      window.removeEventListener("popstate", notify);
      window.removeEventListener("hashchange", notify);
    }
  };
}

const snapshot = () => window.location.pathname;

export function navigate(to: string, opts: { replace?: boolean } = {}): void {
  if (opts.replace) window.history.replaceState(null, "", to);
  else window.history.pushState(null, "", to);
  notify();
  if (!to.includes("#")) window.scrollTo?.(0, 0);
  else {
    const id = fragmentId(window.location.hash);
    const element = id === null ? null : document.getElementById(id);
    if (element) reveal(element);
  }
}

/** Leaves the GUI for another address (a local app's redirect). */
export function leave(url: string): void {
  window.location.assign(url);
}

export function usePathname(): string {
  return useSyncExternalStore(subscribe, snapshot, snapshot);
}

const fragment = () => window.location.hash;

function fragmentId(hash: string): string | null {
  try { return decodeURIComponent(hash.slice(1)); } catch { return null; }
}

function reveal(element: HTMLElement) {
  // Replies in resolved threads must be visible before scrolling to them.
  for (let parent = element.parentElement; parent; parent = parent.parentElement) {
    if (parent instanceof HTMLDetailsElement) parent.open = true;
  }
  element.scrollIntoView({ block: "start" });
}

/** Scroll when an asynchronously loaded post appears, or its fragment is visited again. */
export function useFragmentTarget(id: string) {
  const hash = useSyncExternalStore(subscribe, fragment, fragment);
  const ref = useRef<HTMLElement>(null);
  useEffect(() => {
    const element = ref.current;
    if (element && fragmentId(hash) === id) reveal(element);
  }, [hash, id]);
  return { ref, "data-target": fragmentId(hash) === id || undefined };
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & { to: string; children: ReactNode; go?: () => void };

/** An in-app link: plain left clicks navigate without reloading the page (or `go` instead), others open `to` as a link does. */
export function Link({ to, children, onClick, go = () => navigate(to), ...rest }: LinkProps) {
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    go();
  };
  return (
    <a href={to} onClick={handle} {...rest}>
      {children}
    </a>
  );
}

/** A link for a `url` the service returned: in-app when it is ours, external otherwise. */
export function ServiceLink({ url, children, className }: { url: string; children: ReactNode; className?: string }) {
  const path = inAppPath(url);
  if (path !== null) {
    return (
      <Link to={path} className={className}>
        {children}
      </Link>
    );
  }
  // Foreign references are stored as given; only http(s) becomes a link.
  if (!/^https?:\/\//i.test(url)) return <span className={className}>{children}</span>;
  return (
    <a href={url} className={className} target="_blank" rel="noopener noreferrer">
      {children}
    </a>
  );
}
