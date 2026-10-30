import { useEffect, useState } from "react";
import { Alert, Button, Center, Loader, Stack, Text, Title } from "@mantine/core";
import { useTranslation } from "react-i18next";

import {
  BackendClosedError,
  backendEntry,
  getBootstrapLog,
  letQuitsThrough,
  restartApp,
} from "./api/backend";
import { replacePage } from "./session/pageNavigation";

type BootStatus =
  { state: "loading" } | { state: "error"; error: unknown; logPath: string; logTail: string };

/**
 * The bundled page a Desktop window opens first (`bootstrap.html?next=<page>`):
 * it waits for the backend the host starts, then goes to the server's `next`
 * page through an entry link. The host brings the window back here when the
 * backend closes, and the page then shows why, with the startup log.
 */
export function Bootstrap() {
  const { t } = useTranslation();
  const [status, setStatus] = useState<BootStatus>({ state: "loading" });

  // Nothing runs here that a quit could cut off.
  useEffect(letQuitsThrough, []);

  useEffect(() => {
    let active = true;
    const next = new URLSearchParams(window.location.search).get("next") ?? "/";
    backendEntry(next).then(
      (url) => {
        if (active) {
          replacePage(url);
        }
      },
      async (error: unknown) => {
        const update = (logPath = "", logTail = "") => {
          if (active) {
            setStatus({ state: "error", error, logPath, logTail });
          }
        };
        update();
        const log = await getBootstrapLog().catch(() => null);
        update(log?.path, log?.tail);
      },
    );
    return () => {
      active = false;
    };
  }, []);

  return (
    <Center className="app-bootstrap">
      {status.state === "loading" ? (
        <Stack align="center" gap="md" maw={420}>
          <Loader size="lg" />
          <Title order={3}>{t("app.loading.title")}</Title>
        </Stack>
      ) : (
        <Alert color="danger" title={t("app.loading.closed")} maw={520}>
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
            <Button variant="light" onClick={() => void restartApp()}>
              {t("app.loading.restart")}
            </Button>
          </Stack>
        </Alert>
      )}
    </Center>
  );
}
