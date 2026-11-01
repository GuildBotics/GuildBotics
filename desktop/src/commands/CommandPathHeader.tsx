import { Group, Text } from "@mantine/core";

import { CommandPathActions } from "./CommandPathActions";

/** Path strip above a read-only source view (proposal preview / diff). */
export function CommandPathHeader({ path }: { path: string }) {
  return (
    <Group className="command-path-header" gap="xs" justify="space-between" wrap="nowrap">
      <Text className="command-path-header-text" size="sm" title={path} truncate="end">
        {path}
      </Text>
      <CommandPathActions path={path} />
    </Group>
  );
}
