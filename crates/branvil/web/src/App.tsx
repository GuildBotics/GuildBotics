// Application shell: providers, the session gate, the sidebar, and route dispatch.

import { Button, MantineProvider, Stack, Text, Title } from "@mantine/core";
import { QueryClientProvider, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { AlertBand } from "./components/AlertBand";
import { Loading } from "./components/common";
import { NewWorkspaceModal } from "./components/NewWorkspace";
import { Sidebar } from "./components/Sidebar";
import { I18nProvider, useT } from "./i18n";
import { useChangeStream } from "./query/events";
import { useSession } from "./query/hooks";
import { Link, navigate, usePathname } from "./router/Router";
import { href, parseRoute, type Route } from "./router/routes";
import { theme } from "@guildbotics/ui/theme";
import { AppShell } from "@guildbotics/ui/AppShell";
import { CommitView } from "./views/CommitView";
import { GuidanceView } from "./views/GuidanceView";
import { JobView } from "./views/JobView";
import { PullsView } from "./views/PullsView";
import { PullView } from "./views/PullView";
import { RelationsView } from "./views/RelationsView";
import { SettingsView } from "./views/SettingsView";
import { TicketView } from "./views/TicketView";
import { TicketDrawer } from "./views/ticket/TicketDrawer";
import { useCurrentWorkspace } from "./workspace";

export function App({ client, language, test }: { client: QueryClient; language?: "en" | "ja"; test?: boolean }) {
  return (
    <MantineProvider
      theme={test ? { ...theme, respectReducedMotion: true } : theme}
      defaultColorScheme="auto"
      env={test ? "test" : "default"}
    >
      <QueryClientProvider client={client}>
        <I18nProvider initial={language}>
          <Shell />
        </I18nProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
}

function Shell() {
  return <Gate route={parseRoute(usePathname())} />;
}

function Gate({ route }: { route: Route }) {
  const session = useSession();
  if (session.isPending) return <Loading />;
  if (session.isError) return <GuidanceView error={session.error} onRetry={() => void session.refetch()} />;
  if (!session.data) return <GuidanceView />;
  return <SignedIn route={route} />;
}

function SignedIn({ route }: { route: Route }) {
  const client = useQueryClient();
  const streamLost = useChangeStream(client, true);
  const current = useCurrentWorkspace(route);
  return (
    <AppShell sidebar={<Sidebar route={route} workspace={current.name} workspaces={current.names} onWorkspaceChange={current.select} />}>
      <AlertBand streamLost={streamLost} />
      <RouteView route={route} workspace={current.name} workspaces={current.names} />
    </AppShell>
  );
}

function RouteView({ route, workspace, workspaces }: {
  route: Route;
  workspace: string | null;
  workspaces: string[] | null;
}) {
  const t = useT();
  switch (route.name) {
    case "home":
      return <Home workspace={workspace} workspaces={workspaces} />;
    case "settings":
      return <SettingsView section={route.section} workspace={workspace} repo={route.repo ?? null} />;
    case "pulls":
      return <PullsView key={route.workspace} workspace={route.workspace} repo={null} />;
    case "workspace":
      return (
        <TicketDrawer key={route.workspace}>
          <RelationsView workspace={route.workspace} />
        </TicketDrawer>
      );
    case "repo":
      return <PullsView key={route.repo} workspace={route.repo.split("/")[0] as string} repo={route.repo} />;
    case "ticket":
      return <TicketView key={`${route.repo}#${route.key}`} repo={route.repo} ticketKey={route.key} />;
    case "pull":
      return <PullView key={`${route.repo}#${route.number}`} repo={route.repo} number={route.number} />;
    case "commit":
      return <CommitView repo={route.repo} sha={route.sha} />;
    case "job":
      return <JobView key={`${route.repo}#${route.attemptId}`} repo={route.repo} attemptId={route.attemptId} />;
    case "not_found":
      return (
        <Stack gap="sm">
          <Title order={2}>{t("notFound.title")}</Title>
          <Text>
            <code>{route.pathname}</code>
          </Text>
          <Link to={href.home()}>{t("nav.home")}</Link>
        </Stack>
      );
  }
}

/** `/` opens the shown workspace's first page; with no workspace yet, offers to create one. */
function Home({ workspace, workspaces }: { workspace: string | null; workspaces: string[] | null }) {
  const t = useT();
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    if (workspace) navigate(href.workspace(workspace), { replace: true });
  }, [workspace]);
  if (workspaces === null || workspace) return <Loading />;
  return (
    <Stack gap="lg" align="flex-start">
      <Title order={2}>{t("workspace.noneTitle")}</Title>
      <Text size="sm" c="dimmed">
        {t("workspace.noneHelp")}
      </Text>
      <Button onClick={() => setCreating(true)}>{t("workspace.new")}</Button>
      <NewWorkspaceModal opened={creating} onClose={() => setCreating(false)} />
    </Stack>
  );
}
