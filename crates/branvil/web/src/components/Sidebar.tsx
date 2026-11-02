// The sidebar, as GuildBotics Desktop's: the connection, the workspace, the
// pages, and the display language at the bottom.

import { Select, Stack, Text } from "@mantine/core";
import { GitPullRequest, Settings, SquareKanban, Workflow } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useI18n, type Language } from "../i18n";
import { Link, navigate } from "../router/Router";
import { href, type Route } from "../router/routes";
import { NewWorkspaceModal } from "./NewWorkspace";

const NEW_WORKSPACE = "\u0000new";

type Page = "board" | "workspace" | "pulls" | "settings" | null;

function pageOf(route: Route): Page {
  switch (route.name) {
    case "ticket":
      return "workspace";
    case "repo":
    case "pull":
    case "commit":
    case "job":
      return "pulls";
    case "workspace":
    case "board":
    case "pulls":
    case "settings":
      return route.name;
    default:
      return null;
  }
}

function NavItem({ to, active, icon, children }: { to: string; active: boolean; icon: ReactNode; children: ReactNode }) {
  return (
    <Link to={to} className={active ? "nav-item active" : "nav-item"} aria-current={active ? "page" : undefined}>
      {icon}
      <span>{children}</span>
    </Link>
  );
}

export function Sidebar({
  route,
  workspace,
  workspaces,
  onWorkspaceChange,
  showBoard,
}: {
  route: Route;
  workspace: string | null;
  workspaces: string[] | null;
  onWorkspaceChange: (name: string) => void;
  showBoard: boolean;
}) {
  const { t, lang, setLang } = useI18n();
  const [creating, setCreating] = useState(false);
  const page = pageOf(route);
  const choose = (name: string | null) => {
    if (name === NEW_WORKSPACE) {
      setCreating(true);
      return;
    }
    if (!name || name === workspace) return;
    onWorkspaceChange(name);
    // Preserve top-level pages; detail pages return to the task flow,
    // independently of the sidebar's current-location highlight.
    navigate(
      route.name === "pulls" ? href.pulls(name) : route.name === "board" && showBoard ? href.board(name) : route.name === "settings" ? href.settings() : href.workspace(name),
    );
  };
  return (
    <aside className="sidebar">
      <Stack gap={6}>
        <Text component="label" htmlFor="workspace-switch" className="sidebar-field-label">
          {t("workspace.label")}
        </Text>
        <Select
          id="workspace-switch"
          classNames={{ input: "sidebar-input" }}
          value={workspace}
          data={[
            ...(workspaces ?? []).map((n) => ({ value: n, label: n })),
            { group: " ", items: [{ value: NEW_WORKSPACE, label: t("workspace.newMenu") }] },
          ]}
          onChange={choose}
          allowDeselect={false}
        />
      </Stack>
      <nav className="nav" aria-label={t("nav.main")}>
        {workspace && (
          <>
            <NavItem to={href.workspace(workspace)} active={page === "workspace"} icon={<Workflow size={18} />}>
              {t("nav.relations")}
            </NavItem>
            {showBoard && (
              <NavItem to={href.board(workspace)} active={page === "board"} icon={<SquareKanban size={18} />}>
                {t("nav.board")}
              </NavItem>
            )}
            <NavItem to={href.pulls(workspace)} active={page === "pulls"} icon={<GitPullRequest size={18} />}>
              {t("nav.pulls")}
            </NavItem>
            <div className="nav-separator" />
          </>
        )}
        <NavItem to={href.settings()} active={page === "settings"} icon={<Settings size={18} />}>
          {t("nav.settings")}
        </NavItem>
      </nav>
      <Stack gap={6} className="sidebar-language">
        <Text component="label" htmlFor="language-switch" className="sidebar-field-label">
          {t("nav.language")}
        </Text>
        <Select
          id="language-switch"
          classNames={{ input: "sidebar-input" }}
          value={lang}
          data={[
            { value: "ja", label: "日本語" },
            { value: "en", label: "English" },
          ]}
          onChange={(v) => v && setLang(v as Language)}
          allowDeselect={false}
        />
      </Stack>
      <NewWorkspaceModal opened={creating} onClose={() => setCreating(false)} />
    </aside>
  );
}
