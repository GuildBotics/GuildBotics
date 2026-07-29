import { Avatar, Menu, UnstyledButton } from "@mantine/core";

import { memberAvatarUrl, type TeamSummary } from "./api/client";

type SelectableMember = Pick<TeamSummary["members"][number], "person_id" | "name">;

type MemberSelectorProps = {
  ariaLabel: string;
  className?: string;
  member: SelectableMember | null;
  members: SelectableMember[];
  onChange: (personId: string) => void;
};

/** Compact avatar menu for choosing the member that performs an action. */
export function MemberSelector({
  ariaLabel,
  className,
  member,
  members,
  onChange,
}: MemberSelectorProps) {
  return (
    <Menu position="bottom-start" withinPortal>
      <Menu.Target>
        <UnstyledButton className={className} aria-label={ariaLabel} disabled={!member}>
          <Avatar
            src={member ? memberAvatarUrl(member.person_id) : undefined}
            size={28}
            radius="xl"
            alt={member?.name ?? ""}
          >
            {member?.name.slice(0, 1) ?? ""}
          </Avatar>
        </UnstyledButton>
      </Menu.Target>
      <Menu.Dropdown>
        {members.map((candidate) => (
          <Menu.Item
            key={candidate.person_id}
            leftSection={
              <Avatar src={memberAvatarUrl(candidate.person_id)} size={20} radius="xl">
                {candidate.name.slice(0, 1)}
              </Avatar>
            }
            onClick={() => onChange(candidate.person_id)}
          >
            {candidate.name}
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}
