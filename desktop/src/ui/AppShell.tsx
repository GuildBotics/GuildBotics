import type { ReactNode } from "react";

/** Visual frame; each application owns its navigation, routes and data. */
export function AppShell({ sidebar, children }: { sidebar: ReactNode; children: ReactNode }) {
  return (
    <main className="shell">
      <aside className="sidebar">{sidebar}</aside>
      <section className="workspace">{children}</section>
    </main>
  );
}
