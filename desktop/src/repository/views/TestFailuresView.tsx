import { Button, Card, Group, Stack, Table, Text, TextInput } from "@mantine/core";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { Header } from "../../ui/Header";
import { jobs } from "../api/endpoints";
import { ErrorMessage, Loading, Sha, Time } from "../components/common";
import { useT } from "../i18n";
import { useRunnerLabel } from "../query/hooks";
import { keys } from "../query/keys";
import { Link, ServiceLink } from "../router/Router";
import { href } from "../router/routes";

export function TestFailuresView({ repo }: { repo: string }) {
  const t = useT();
  const runnerLabel = useRunnerLabel();
  const [input, setInput] = useState("");
  const [name, setName] = useState("");
  const history = useQuery({
    queryKey: keys.testFailures(repo, name),
    queryFn: () => jobs.testFailures(repo, name),
    enabled: name !== "",
  });
  return (
    <Stack gap="lg">
      <Header title={t("ci.testFailures")} meta={<Link to={href.repo(repo)}>{repo}</Link>} />
      <Card p="lg">
        <Stack gap="md">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              if (!input.trim()) return;
              if (input === name) void history.refetch();
              else setName(input);
            }}
          >
            <Group align="end" gap="sm">
              <TextInput
                label={t("ci.testName")}
                value={input}
                onChange={(event) => setInput(event.currentTarget.value)}
                style={{ flex: "1 1 260px", maxWidth: 640 }}
              />
              <Button type="submit" disabled={!input.trim()}>
                {t("ci.searchFailures")}
              </Button>
            </Group>
          </form>
          {name && history.isPending && <Loading />}
          {history.isError && <ErrorMessage error={history.error} />}
          {history.data?.length === 0 && <Text c="dimmed">{t("ci.noTestFailures")}</Text>}
          {history.data && history.data.length > 0 && (
            <Table.ScrollContainer minWidth={520}>
              <Table>
                <caption>{name}</caption>
                <Table.Thead>
                  <Table.Tr>
                    <Table.Th>{t("ci.failedAttempt")}</Table.Th>
                    <Table.Th>{t("ci.runner")}</Table.Th>
                    <Table.Th>{t("ci.commit")}</Table.Th>
                    <Table.Th>{t("ci.finished")}</Table.Th>
                  </Table.Tr>
                </Table.Thead>
                <Table.Tbody>
                  {history.data.map((attempt) => (
                    <Table.Tr key={attempt.attempt_id}>
                      <Table.Td>
                        <ServiceLink url={attempt.url}>{attempt.job}</ServiceLink>
                      </Table.Td>
                      <Table.Td>{runnerLabel(attempt.runner, true)}</Table.Td>
                      <Table.Td>
                        <Sha repo={repo} value={attempt.sha} />
                      </Table.Td>
                      <Table.Td>
                        <Time value={attempt.finished_at} />
                      </Table.Td>
                    </Table.Tr>
                  ))}
                </Table.Tbody>
              </Table>
            </Table.ScrollContainer>
          )}
        </Stack>
      </Card>
    </Stack>
  );
}
