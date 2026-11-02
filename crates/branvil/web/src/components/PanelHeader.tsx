import { Group, Text, Title } from "@mantine/core";
import type { ReactNode } from "react";

/** A settings card's header, as GuildBotics' PanelHeader: title and description, an action on the right. */
export function PanelHeader({ title, description, action, order = 3 }: { title: ReactNode; description?: ReactNode; action?: ReactNode; order?: 3 | 4 }) {
  return (
    <Group justify="space-between" align="flex-start" wrap="nowrap" mb="md">
      <div>
        <Title order={order} fz={order === 4 ? 16 : undefined}>
          {title}
        </Title>
        {description && (
          <Text size="sm" c="dimmed">
            {description}
          </Text>
        )}
      </div>
      {action}
    </Group>
  );
}
