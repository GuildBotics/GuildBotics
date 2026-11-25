import React, { useState } from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, useLocation } from "react-router";

import { App } from "./App";
import { workspaceFromPath } from "./api/client";
import { createQueryClient } from "./repository/query/hooks";
import { installDragScroll } from "./repository/dragScroll";
import { EntryPage } from "./session/EntryPage";
import { theme } from "./ui/theme";
import "./i18n";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./styles.css";

const sessionClient = createQueryClient();
installDragScroll();

function Screen() {
  const workspace = workspaceFromPath(useLocation().pathname);
  return <WorkspaceScreen key={workspace ?? ""} />;
}

function WorkspaceScreen() {
  // Observers and pending results belong to the workspace in the URL.
  const [client] = useState(createQueryClient);
  return (
    <EntryPage contentClient={client}>
      <QueryClientProvider client={client}>
        <App />
      </QueryClientProvider>
    </EntryPage>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <Notifications position="top-right" />
      <QueryClientProvider client={sessionClient}>
        <BrowserRouter>
          <Screen />
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </React.StrictMode>,
);
