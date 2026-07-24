// App-wide navigation guard. A page with unsaved changes registers a guard so
// that in-app navigation (e.g. the sidebar) can prompt before discarding the
// buffer. This works with the component-based `HashRouter` where React Router's
// data-router `useBlocker` is unavailable.

export type NavigationGuard = {
  shouldBlock: () => boolean;
  confirm: (proceed: () => void) => void;
};

let activeGuard: NavigationGuard | null = null;

export function setNavigationGuard(guard: NavigationGuard | null): void {
  activeGuard = guard;
}

// Run `proceed` immediately unless the active guard wants to intercept, in which
// case the guard is responsible for eventually calling `proceed` (or not).
export function requestNavigation(proceed: () => void): void {
  if (activeGuard && activeGuard.shouldBlock()) {
    activeGuard.confirm(proceed);
    return;
  }
  proceed();
}
