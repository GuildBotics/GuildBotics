// Dispatch repository routes inside the shared application shell.

import { Button, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";
import { Navigate } from "react-router";
import { Loading } from "./components/common";
import { NewWorkspaceModal } from "./components/NewWorkspace";
import { useT } from "./i18n";
import { Link } from "./router/Router";
import { href, type Route } from "./router/routes";
import { CommitView } from "./views/CommitView";
import { JobView } from "./views/JobView";
import { PullsView } from "./views/PullsView";
import { PullView } from "./views/PullView";
import { ReleasesView } from "./views/ReleasesView";
import { RelationsView } from "./views/RelationsView";
import { TicketView } from "./views/TicketView";
import { TicketDrawer } from "./views/ticket/TicketDrawer";
import type { CurrentWorkspace } from "./workspace";

export function RepositoryPage({
  route,
  current,
}: {
  route: Exclude<Route, { name: "settings" }>;
  current: CurrentWorkspace;
}) {
  const t = useT();
  switch (route.name) {
    case "home":
      return <Home current={current} />;
    case "pulls":
      return <PullsView key={route.workspace} workspace={route.workspace} repo={null} />;
    case "workspace":
      return (
        <TicketDrawer key={route.workspace}>
          <RelationsView workspace={route.workspace} />
        </TicketDrawer>
      );
    case "repo":
      return (
        <PullsView
          key={route.repo}
          workspace={route.repo.split("/")[0] as string}
          repo={route.repo}
        />
      );
    case "releases":
      return <ReleasesView key={route.workspace} workspace={route.workspace} repo={route.repo} />;
    case "ticket":
      return (
        <TicketView key={`${route.repo}#${route.key}`} repo={route.repo} ticketKey={route.key} />
      );
    case "pull":
      return (
        <PullView key={`${route.repo}#${route.number}`} repo={route.repo} number={route.number} />
      );
    case "commit":
      return <CommitView repo={route.repo} sha={route.sha} />;
    case "job":
      return (
        <JobView
          key={`${route.repo}#${route.attemptId}`}
          repo={route.repo}
          attemptId={route.attemptId}
        />
      );
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
function Home({ current }: { current: CurrentWorkspace }) {
  const t = useT();
  const [creating, setCreating] = useState(false);
  if (current.name) return <Navigate to={href.workspace(current.name)} replace />;
  if (current.query.isError) return null;
  if (current.query.isPending) return <Loading />;
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
