import { Button, Card, Stack, Text, Title } from "@mantine/core";
import { ErrorMessage } from "../components/common";
import { useT } from "../i18n";

/** Without a session every page shows how to get one. */
export function GuidanceView({ error, onRetry }: { error?: unknown; onRetry?: () => void }) {
  const t = useT();
  return (
    <main className="guide">
      <Card p="lg">
        <Stack gap="md">
          <Title order={2}>Branvil</Title>
          <Text size="sm">{t("guide.run")}</Text>
          <pre>
            <code>guildbotics open --print</code>
          </pre>
          <Text size="sm" c="dimmed">
            {t("guide.why")}
          </Text>
          {error !== undefined && (
            <>
              <ErrorMessage error={error} />
              <div>
                <Button variant="default" onClick={onRetry}>
                  {t("common.retry")}
                </Button>
              </div>
            </>
          )}
        </Stack>
      </Card>
    </main>
  );
}
