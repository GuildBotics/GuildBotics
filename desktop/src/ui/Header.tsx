import { Box, Group, Text, Title } from "@mantine/core";
import type { ReactNode } from "react";

/** Header for a page or settings panel, with its controls kept at the right edge. */
export function Header({
  back,
  title,
  meta,
  description,
  action,
  badge,
  order = 2,
  grow = false,
}: {
  back?: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  description?: ReactNode;
  action?: ReactNode;
  badge?: ReactNode;
  order?: 2 | 3 | 4;
  grow?: boolean;
}) {
  const page = order === 2;
  const heading =
    page && typeof title !== "string" ? (
      title
    ) : (
      <Title order={order} fz={order === 4 ? 16 : undefined}>
        {title}
      </Title>
    );
  return (
    <Group
      justify="space-between"
      align="flex-start"
      wrap="nowrap"
      mb={page || grow ? undefined : "md"}
    >
      <Box miw={page || grow ? 0 : undefined} flex={grow ? 1 : undefined}>
        {back && <div className="page-crumb">{back}</div>}
        {grow ? (
          <Group gap="xs">
            {heading}
            {badge}
          </Group>
        ) : (
          heading
        )}
        {description && (
          <Text size="sm" c="dimmed">
            {description}
          </Text>
        )}
        {meta && <div className="page-meta">{meta}</div>}
      </Box>
      {action &&
        (page || grow ? (
          <div style={{ marginTop: back ? 26 : undefined, flexShrink: 0 }}>{action}</div>
        ) : (
          action
        ))}
    </Group>
  );
}
