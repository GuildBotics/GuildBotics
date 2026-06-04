import React, { useCallback, useEffect, useState } from "react";
import ReactDOM from "react-dom/client";
import {
  Alert,
  Button,
  Center,
  Loader,
  MantineProvider,
  Stack,
  Text,
  Title,
  createTheme,
} from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HashRouter } from "react-router-dom";
import { useTranslation } from "react-i18next";

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

type BootStatus =
  | { state: "loading" }
  | { state: "ready" }
  | { state: "error"; message: string };

function Bootstrap() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<BootStatus>({ state: "loading" });

  const connect = useCallback(() => {
    setStatus({ state: "loading" });
    startBackend()
      .then(() => setStatus({ state: "ready" }))
      .catch((error: unknown) => setStatus({ state: "error", message: String(error) }));
  }, []);

  useEffect(() => {
    connect();
  }, [connect]);

  if (status.state === "ready") {
    return (
      <HashRouter>
        <App />
      </HashRouter>
    );
  }

  return (
    <Center className="app-bootstrap">
      {status.state === "loading" ? (
        <Stack align="center" gap="md" maw={420}>
          <Loader size="lg" />
          <Title order={3}>{t("app.loading.title")}</Title>
          <Text c="dimmed" ta="center">
            {t("app.loading.body")}
          </Text>
        </Stack>
      ) : (
        <Alert color="red" title={t("app.loading.failed")} maw={520}>
          <Stack gap="sm">
            <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
              {status.message}
            </Text>
            <Button variant="light" onClick={connect}>
              {t("app.loading.retry")}
            </Button>
          </Stack>
        </Alert>
      )}
    </Center>
  );
}

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
