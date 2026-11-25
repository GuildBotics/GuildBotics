import { Text } from "@mantine/core";
import { Loading } from "../components/common";
import { useT } from "../i18n";
import { useSession } from "../query/hooks";
import { CiSettings } from "./settings/CiSettings";
import { MembersSettings } from "./settings/MembersSettings";
import { NotificationsSettings } from "./settings/NotificationsSettings";
import { WorkspaceSettings } from "./settings/WorkspaceSettings";

export type RepositorySection = "repositories" | "members" | "ci" | "notifications";

export function useRepositorySettings(
  section: string | null,
  workspace: string | null,
  enabled: boolean,
) {
  const session = useSession(enabled);
  const owner = session.data?.me.kind === "owner";
  const current: RepositorySection =
    section === "ci" || section === "notifications"
      ? owner
        ? section
        : "members"
      : section === "members"
        ? "members"
        : "repositories";
  return {
    current,
    owner,
    pending: session.isPending && (section === "ci" || section === "notifications"),
    workspace,
  };
}

export function RepositorySettingsPanel({
  settings,
  repo,
}: {
  settings: ReturnType<typeof useRepositorySettings>;
  repo?: string;
}) {
  const t = useT();
  if (settings.pending) return <Loading />;

  switch (settings.current) {
    case "ci":
      return <CiSettings />;
    case "notifications":
      return <NotificationsSettings />;
    case "members":
      return <MembersSettings />;
    case "repositories":
      return settings.workspace ? (
        <WorkspaceSettings
          key={repo ?? settings.workspace}
          workspace={settings.workspace}
          open={repo}
        />
      ) : (
        <Text size="sm" c="dimmed">
          {t("workspace.noneHelp")}
        </Text>
      );
  }
}
