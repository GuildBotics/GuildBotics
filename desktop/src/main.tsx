import React from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { Bootstrap } from "./Bootstrap";
import { stopBackend } from "./api/backend";
import { theme } from "./theme";
import "./i18n";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./styles.css";

const queryClient = new QueryClient();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <Notifications position="top-right" />
      <QueryClientProvider client={queryClient}>
        <Bootstrap />
      </QueryClientProvider>
    </MantineProvider>
  </React.StrictMode>,
);

window.addEventListener("beforeunload", () => {
  void stopBackend();
});
