import { Card, Stack, Switch, Text, Title } from "@mantine/core";
import { Bell, Cpu, Monitor, SquareKanban, Users } from "lucide-react";
import type { ReactNode } from "react";
import { PanelHeader } from "../components/PanelHeader";
import { useT } from "../i18n";
import { useSession } from "../query/hooks";
import { Link } from "../router/Router";
import { href } from "../router/routes";
import { CiSettings } from "./settings/CiSettings";
import { MembersSettings } from "./settings/MembersSettings";
import { NotificationsSettings } from "./settings/NotificationsSettings";
import { WorkspaceSettings } from "./settings/WorkspaceSettings";

function NavItem({ to, active, icon, children }: { to: string; active: boolean; icon: ReactNode; children: ReactNode }) {
  return (
    <Link to={to} className={active ? "setup-nav-item active" : "setup-nav-item"} aria-current={active ? "page" : undefined}>
      {icon}
      {children}
    </Link>
  );
}

/**
 * Settings, laid out as GuildBotics' setup: sections on the left, one card on
 * the right. The shown workspace (with its repositories) apart from what
 * belongs to all of Branvil (its members, and CI and notifications for the
 * owner). `repo` opens that repository's editor on its CI tab.
 */
export function SettingsView({ section, workspace, repo, showBoard, setShowBoard }: {
  section: string | null;
  workspace: string | null;
  repo: string | null;
  showBoard: boolean;
  setShowBoard: (value: boolean) => void;
}) {
  const t = useT();
  const owner = useSession().data?.me.kind === "owner";
  const current = section === "display" ? "display"
    : (section === "ci" || section === "notifications") && owner ? section
    : section === "members" || !workspace ? "members" : "workspace";
  return (
    <Stack gap="lg">
      <Title order={2}>{t("nav.settings")}</Title>
      <div className="setup-layout">
        <Card p="xs">
          <nav className="setup-nav" aria-label={t("settings.sections")}>
            {workspace && (
              <div className="setup-nav-group">
                <div className="nav-label">{t("settings.workspaceGroup")}</div>
                <NavItem to={href.settings()} active={current === "workspace"} icon={<SquareKanban size={18} />}>
                  {workspace}
                </NavItem>
              </div>
            )}
            <div className="setup-nav-group">
              <div className="nav-label">{t("settings.branvilGroup")}</div>
              <NavItem to={href.settings("display")} active={current === "display"} icon={<Monitor size={18} />}>
                {t("settings.display")}
              </NavItem>
              <NavItem to={href.settings("members")} active={current === "members"} icon={<Users size={18} />}>
                {t("settings.members")}
              </NavItem>
              {owner && (
                <NavItem to={href.settings("ci")} active={current === "ci"} icon={<Cpu size={18} />}>
                  {t("ci.settings.title")}
                </NavItem>
              )}
              {owner && (
                <NavItem to={href.settings("notifications")} active={current === "notifications"} icon={<Bell size={18} />}>
                  {t("settings.notifications")}
                </NavItem>
              )}
            </div>
          </nav>
        </Card>
        <div>
          {current === "display" ? (
            <Card p="lg">
              <PanelHeader title={t("settings.display")} description={t("settings.displayHelp")} />
              <Switch label={t("settings.showBoard")} checked={showBoard} onChange={(event) => setShowBoard(event.currentTarget.checked)} />
            </Card>
          ) : current === "ci" ? (
            <CiSettings />
          ) : current === "notifications" ? (
            <NotificationsSettings />
          ) : current === "workspace" && workspace ? (
            <WorkspaceSettings workspace={workspace} open={repo} />
          ) : (
            <MembersSettings />
          )}
          {!workspace && current === "members" && (
            <Text size="sm" c="dimmed" mt="sm">
              {t("workspace.noneHelp")}
            </Text>
          )}
        </div>
      </div>
    </Stack>
  );
}
