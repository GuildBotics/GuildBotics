// Shared router links and comment fragments.

import {
  useEffect,
  useRef,
  type AnchorHTMLAttributes,
  type MouseEvent,
  type ReactNode,
} from "react";
import { Link as RouterLink, useLocation } from "react-router";
import { inAppPath } from "./routes";

function fragmentId(hash: string): string | null {
  try {
    return decodeURIComponent(hash.slice(1));
  } catch {
    return null;
  }
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
  const { key, hash } = useLocation();
  const ref = useRef<HTMLElement>(null);
  const active = fragmentId(hash) === id;
  useEffect(() => {
    const element = ref.current;
    if (element && active) reveal(element);
  }, [active, key, hash]);
  return { ref, "data-target": active || undefined };
}

type LinkProps = AnchorHTMLAttributes<HTMLAnchorElement> & {
  to: string;
  children: ReactNode;
  go?: () => void;
};

/** An in-app link: plain left clicks navigate without reloading the page (or `go` instead), others open `to` as a link does. */
export function Link({ to, children, onClick, go, ...rest }: LinkProps) {
  const handle = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey)
      return;
    if (go) {
      e.preventDefault();
      go();
    }
  };
  return (
    <RouterLink to={to} onClick={handle} {...rest}>
      {children}
    </RouterLink>
  );
}

/** A link for a `url` the service returned: in-app when it is ours, external otherwise. */
export function ServiceLink({
  url,
  children,
  className,
}: {
  url: string;
  children: ReactNode;
  className?: string;
}) {
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
