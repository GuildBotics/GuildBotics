import React from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider, createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { Bootstrap } from "./Bootstrap";
import { stopBackend } from "./api/backend";
import "./i18n";
import "@mantine/core/styles.css";
import "./styles.css";

const queryClient = new QueryClient();
const theme = createTheme({
  primaryColor: "dark",
  defaultRadius: "md",
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MantineProvider theme={theme}>
      <QueryClientProvider client={queryClient}>
        <Bootstrap />
      </QueryClientProvider>
    </MantineProvider>
  </React.StrictMode>,
);

window.addEventListener("beforeunload", () => {
  void stopBackend();
});
