import { useCallback, useEffect, useState } from "react";
import { Alert, Button, Center, Loader, Stack, Text, Title } from "@mantine/core";
import { HashRouter } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { App } from "./App";
import { startBackend } from "./api/backend";

type BootStatus = { state: "loading" } | { state: "ready" } | { state: "error"; message: string };

export function Bootstrap() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<BootStatus>({ state: "loading" });

  const connect = useCallback(() => {
    setStatus({ state: "loading" });
    startBackend()
      .then(() => setStatus({ state: "ready" }))
      .catch((error: unknown) => setStatus({ state: "error", message: String(error) }));
  }, []);

  useEffect(() => {
    let active = true;
    startBackend()
      .then(() => {
        if (active) {
          setStatus({ state: "ready" });
        }
      })
      .catch((error: unknown) => {
        if (active) {
          setStatus({ state: "error", message: String(error) });
        }
      });
    return () => {
      active = false;
    };
  }, []);

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
