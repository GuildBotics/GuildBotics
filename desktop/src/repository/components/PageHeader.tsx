import { Group, Title } from "@mantine/core";
import type { ReactNode } from "react";

/**
 * A page's title row, as GuildBotics' pages begin: a link back to where the
 * page was reached from, the title, a line of facts, and controls on the right.
 */
export function PageHeader({
  back,
  title,
  meta,
  right,
}: {
  back?: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  right?: ReactNode;
}) {
  return (
    <Group justify="space-between" align="flex-start" wrap="nowrap">
      <div style={{ minWidth: 0 }}>
        {back && <div className="page-crumb">{back}</div>}
        {typeof title === "string" ? <Title order={2}>{title}</Title> : title}
        {meta && <div className="page-meta">{meta}</div>}
      </div>
      {right && <div style={{ marginTop: back ? 26 : 0, flex: "0 0 auto" }}>{right}</div>}
    </Group>
  );
}
