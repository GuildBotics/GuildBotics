import { Button, Code, Group, Modal, ScrollArea, Stack, Text, TextInput } from "@mantine/core";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useQueryClient } from "@tanstack/react-query";
import { loginCliAgent } from "../api/client";

/** Provider prompts, URLs and device codes stay in this mounted dialog only. */
export function BrowserToolLogin({ tool }: { tool: string }) {
  const { t } = useTranslation();
  const query = useQueryClient();
  const [opened, setOpened] = useState(false);
  const [output, setOutput] = useState("");
  const [code, setCode] = useState("");
  const [result, setResult] = useState<boolean | null>(null);
  const connection = useRef<ReturnType<typeof loginCliAgent> | null>(null);
  useEffect(() => () => connection.current?.close(), []);
  function close() {
    connection.current?.close();
    connection.current = null;
    setOutput("");
    setCode("");
    setOpened(false);
  }
  function start() {
    setOutput("");
    setCode("");
    setResult(null);
    setOpened(true);
    connection.current = loginCliAgent(
      tool,
      (chunk) => setOutput((previous) => (previous + chunk).slice(-65536)),
      (saved) => {
        setCode("");
        setResult(saved);
        void query.invalidateQueries({ queryKey: ["agent-environment-status"] });
        void query.invalidateQueries({ queryKey: ["system-alerts"] });
        void query.invalidateQueries({ queryKey: ["cli-agent-usage", tool] });
      },
    );
  }
  return (
    <>
      <Button size="compact-xs" onClick={start}>
        {t("setup.intelligence.environment.login")}
      </Button>
      <Modal
        opened={opened}
        onClose={close}
        title={t("setup.intelligence.environment.login")}
        size="lg"
      >
        <Stack>
          <Text size="sm">{t("setup.intelligence.environment.browserLoginHint")}</Text>
          <ScrollArea.Autosize mah={320}>
            <Code block style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
              {output}
            </Code>
          </ScrollArea.Autosize>
          {result === null ? (
            <form
              onSubmit={(event) => {
                event.preventDefault();
                connection.current?.send(code);
                setCode("");
              }}
            >
              <Group align="end">
                <TextInput
                  autoComplete="off"
                  value={code}
                  onChange={(event) => setCode(event.currentTarget.value)}
                  label={t("setup.intelligence.environment.loginCode")}
                />
                <Button type="submit">{t("setup.intelligence.environment.loginSend")}</Button>
              </Group>
            </form>
          ) : (
            <Text role="status" c={result ? "success" : "danger"}>
              {t(`setup.intelligence.environment.${result ? "loginSaved" : "loginError"}`)}
            </Text>
          )}
        </Stack>
      </Modal>
    </>
  );
}
