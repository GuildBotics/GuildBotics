import { Menu, Text, UnstyledButton, type MenuProps } from "@mantine/core";
import type { ReactNode } from "react";

export function MemberMenu({
  label,
  avatar,
  children,
  className,
  disabled,
  title,
  position,
  closeOnItemClick = true,
}: Pick<MenuProps, "position" | "closeOnItemClick"> & {
  label: string;
  avatar: ReactNode;
  children: ReactNode;
  className?: string;
  disabled?: boolean;
  title?: string;
}) {
  return (
    <Menu position={position} closeOnItemClick={closeOnItemClick} withinPortal>
      <Menu.Target>
        <UnstyledButton className={className} aria-label={label} title={title} disabled={disabled}>
          {avatar}
        </UnstyledButton>
      </Menu.Target>
      <Menu.Dropdown>{children}</Menu.Dropdown>
    </Menu>
  );
}

export function MemberMenuItem({
  avatar,
  name,
  checked,
  note,
  onChoose,
}: {
  avatar: ReactNode;
  name: string;
  checked?: boolean;
  note?: string | null;
  onChoose: () => void;
}) {
  return (
    <Menu.Item
      leftSection={avatar}
      rightSection={
        note ? (
          <Text size="xs" c="dimmed" ml="md">
            {note}
          </Text>
        ) : null
      }
      aria-checked={checked}
      onClick={onChoose}
    >
      {name}
    </Menu.Item>
  );
}
