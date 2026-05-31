import React from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HashRouter } from "react-router-dom";

import { App } from "./App";
import { startBackend, stopBackend } from "./api/backend";
import "./i18n";
import "@mantine/core/styles.css";
import "./styles.css";

const queryClient = new QueryClient();
const theme = createTheme({
  primaryColor: "dark",
  defaultRadius: "md",
});

startBackend()
  .then(() => {
    ReactDOM.createRoot(document.getElementById("root")!).render(
      <React.StrictMode>
        <MantineProvider theme={theme}>
          <QueryClientProvider client={queryClient}>
            <HashRouter>
              <App />
            </HashRouter>
          </QueryClientProvider>
        </MantineProvider>
      </React.StrictMode>,
    );
  })
  .catch((error: unknown) => {
    document.body.innerHTML = `<pre class="startup-error">${String(error)}</pre>`;
  });

window.addEventListener("beforeunload", () => {
  void stopBackend();
});
