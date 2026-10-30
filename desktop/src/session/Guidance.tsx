import {
  Alert,
  Button,
  Card,
  Center,
  Code,
  Group,
  Loader,
  Modal,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { useEffect, type ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { letQuitsThrough } from "../api/backend";

/**
 * Rendered wherever the App, whose guard answers the host's quits, is not: the
 * Desktop window would otherwise wait for an answer nobody gives.
 */
export function QuitsThrough() {
  useEffect(letQuitsThrough, []);
  return null;
}

export function Connecting() {
  return (
    <Center className="app-bootstrap">
      <Loader size="lg" />
    </Center>
  );
}

/** A page of its own, in place of the App. */
export function GuideCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Center className="app-bootstrap">
      <Card withBorder padding="lg" maw={560}>
        <Stack gap="md">
          <Title order={3}>{title}</Title>
          {children}
        </Stack>
      </Card>
    </Center>
  );
}

/** Without a session every page says how to get one from the server machine. */
export function NoSessionGuide({
  lead,
  notice,
  error,
  onRetry,
}: {
  lead?: string;
  notice?: string;
  error?: unknown;
  onRetry?: () => void;
}) {
  const { t } = useTranslation();
  return (
    <GuideCard title={t("session.guide.title")}>
      {notice ? <Alert color="warning">{notice}</Alert> : null}
      {lead ? <Text size="sm">{lead}</Text> : null}
      <Text size="sm">{t("session.guide.run")}</Text>
      <Code block>guildbotics open</Code>
      <Text size="sm">{t("session.guide.remote")}</Text>
      <Code block>ssh &lt;server&gt; guildbotics open --print</Code>
      {error === undefined ? null : <FailureAlert error={error} />}
      {onRetry ? (
        <Group>
          <Button variant="light" onClick={onRetry}>
            {t("session.retry")}
          </Button>
        </Group>
      ) : null}
    </GuideCard>
  );
}

export function FailureAlert({ error }: { error: unknown }) {
  return <Alert color="danger">{error instanceof Error ? error.message : String(error)}</Alert>;
}

/** The server stopped answering: what is on the screen stays until it answers again. */
export function UnreachableNotice({
  retrying,
  onRetry,
}: {
  retrying: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  return (
    <Modal
      opened
      onClose={() => undefined}
      withCloseButton={false}
      closeOnClickOutside={false}
      closeOnEscape={false}
      centered
      title={t("session.unreachable.title")}
    >
      <Stack gap="md">
        <Text size="sm">{t("session.unreachable.body")}</Text>
        <Group justify="flex-end">
          <Button loading={retrying} onClick={onRetry}>
            {t("session.retry")}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}
