import { MemberMenu, MemberMenuItem } from "./ui/MemberMenu";
import { Avatar } from "@mantine/core";

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
    <MemberMenu
      label={ariaLabel}
      className={className}
      disabled={!members.length}
      position="bottom-start"
      avatar={
        <Avatar
          src={member ? memberAvatarUrl(member.person_id) : undefined}
          size={28}
          radius="xl"
          alt={member?.name ?? ""}
        >
          {member?.name.slice(0, 1) ?? ""}
        </Avatar>
      }
    >
      {members.map((candidate) => (
        <MemberMenuItem
          key={candidate.person_id}
          avatar={
            <Avatar src={memberAvatarUrl(candidate.person_id)} size={20} radius="xl">
              {candidate.name.slice(0, 1)}
            </Avatar>
          }
          name={candidate.name}
          onChoose={() => onChange(candidate.person_id)}
        />
      ))}
    </MemberMenu>
  );
}
