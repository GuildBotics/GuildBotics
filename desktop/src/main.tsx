import React from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter } from "react-router";

import { App } from "./App";
import { workspaceFromPath } from "./api/client";
import { workspacePath } from "./repository/router/routes";
import { RepositoryApp } from "./repository/App";
import { usePathname } from "./repository/router/Router";
import { parseRoute } from "./repository/router/routes";
import { createQueryClient } from "./repository/query/hooks";
import { installDragScroll } from "./repository/dragScroll";
import { EntryPage } from "./session/EntryPage";
import { theme } from "./ui/theme";
import "./i18n";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./ui/styles.css";
import "./ui/components.css";
import "./ui/markdown.css";
import desktopStyles from "./styles.css?inline";
import repositoryStyles from "./repository/styles.css?inline";

const queryClient = createQueryClient();
installDragScroll();

function Screen() {
  const pathname = usePathname();
  const repository = pathname !== "/" && parseRoute(pathname).name !== "not_found";
  const workspace = workspaceFromPath(pathname);
  return (
    <>
      <style>{repository ? repositoryStyles : desktopStyles}</style>
      <EntryPage>
        {repository ? (
          <RepositoryApp />
        ) : (
          <BrowserRouter basename={workspace ? workspacePath(workspace) : undefined}>
            <App scoped={Boolean(workspace)} />
          </BrowserRouter>
        )}
      </EntryPage>
    </>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <Notifications position="top-right" />
      <QueryClientProvider client={queryClient}>
        <Screen />
      </QueryClientProvider>
    </MantineProvider>
  </React.StrictMode>,
);
