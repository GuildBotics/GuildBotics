import { Group, Text } from "@mantine/core";

import { CommandPathActions } from "./CommandPathActions";

export type CommandPathHeaderProps = {
  path: string;
  onOpenExternal?: () => void;
};

/** Path strip above a read-only source view (proposal preview / diff). */
export function CommandPathHeader({ path, onOpenExternal }: CommandPathHeaderProps) {
  return (
    <Group className="command-path-header" gap="xs" justify="space-between" wrap="nowrap">
      <Text className="command-path-header-text" size="sm" title={path} truncate="end">
        {path}
      </Text>
      <CommandPathActions path={path} onOpenExternal={onOpenExternal} />
    </Group>
  );
}
