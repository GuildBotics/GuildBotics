import type { ReactNode } from "react";

/** Shared navigation and page layout. */
export function AppShell({ sidebar, children }: { sidebar: ReactNode; children: ReactNode }) {
  return (
    <main className="shell">
      <aside className="sidebar">{sidebar}</aside>
      <section className="workspace">{children}</section>
    </main>
  );
}
