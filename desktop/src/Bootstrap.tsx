import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Center, Loader, Stack, Text, Title } from "@mantine/core";
import { HashRouter } from "react-router";
import { useTranslation } from "react-i18next";

import { App } from "./App";
import {
  BackendClosedError,
  canRestartApp,
  getBootstrapLog,
  letQuitsThrough,
  onBackendClosed,
  restartApp,
  startBackend,
} from "./api/backend";

type BootStatus =
  | { state: "loading" }
  | { state: "ready" }
  | {
      state: "error";
      title: "failed" | "stopped";
      error: unknown;
      logPath: string;
      logTail: string;
    };

export function Bootstrap() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<BootStatus>({ state: "loading" });

  const connect = useCallback(() => {
    setStatus({ state: "loading" });
    void connectBackend(setStatus);
  }, []);

  useEffect(() => {
    let active = true;
    const update = (next: BootStatus) => {
      if (active) {
        setStatus(next);
      }
    };
    // The backend starts again only with the app, so its exit ends this session.
    const stopWatching = onBackendClosed((error) => void showFailure("stopped", error, update));
    void connectBackend(update);
    return () => {
      active = false;
      stopWatching();
    };
  }, []);

  const showingApp = status.state === "ready";
  useEffect(() => (showingApp ? undefined : letQuitsThrough()), [showingApp]);

  if (showingApp) {
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
        </Stack>
      ) : (
        <Alert color="danger" title={t(`app.loading.${status.title}`)} maw={520}>
          <Stack gap="sm">
            <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
              {status.error instanceof BackendClosedError
                ? t(`app.loading.reasons.${status.error.closure.reason}`, {
                    detail: status.error.closure.detail,
                  })
                : String(status.error)}
            </Text>
            {status.logPath ? (
              <Text size="xs">
                {t("app.loading.logPath")}: <code>{status.logPath}</code>
              </Text>
            ) : null}
            {status.logTail ? <pre className="command-output">{status.logTail}</pre> : null}
            {canRestartApp() ? (
              <Button variant="light" onClick={() => void restartApp()}>
                {t("app.loading.restart")}
              </Button>
            ) : (
              <Button variant="light" onClick={connect}>
                {t("app.loading.retry")}
              </Button>
            )}
          </Stack>
        </Alert>
      )}
    </Center>
  );
}

async function connectBackend(update: (status: BootStatus) => void) {
  try {
    await startBackend();
    update({ state: "ready" });
  } catch (error) {
    await showFailure("failed", error, update);
  }
}

async function showFailure(
  title: "failed" | "stopped",
  error: unknown,
  update: (status: BootStatus) => void,
) {
  // Leave the App at once: its screens cannot reach the backend any more.
  update({ state: "error", title, error, logPath: "", logTail: "" });
  const log = await getBootstrapLog().catch(() => null);
  update({
    state: "error",
    title,
    error,
    logPath: log?.path ?? "",
    logTail: log?.tail ?? "",
  });
}
