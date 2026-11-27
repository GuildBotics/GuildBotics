import { Checkbox, MultiSelect, Select, Text, TextInput } from "@mantine/core";
import { Search } from "lucide-react";
import { useMemo, type ReactNode } from "react";
import { LANES, type Card } from "../../api/types";
import type { Person } from "../../components/PeoplePicker";
import { useT } from "../../i18n";
import type { CardFilter } from "./ordering";

/** Select values for "any" and "nobody" in the filters (Mantine needs a value). */
const ANY = "*";
const NOBODY = "-";

/**
 * The filters over a workspace's cards (its board, the relations of its
 * tickets): text, assignees, lane, label, repository (when the workspace has
 * more than one), interactive tickets only, and whether closed tickets are
 * shown; `status` follows them.
 */
export function CardFilters({
  cards,
  people,
  repositories,
  filter,
  onFilter,
  includeClosed,
  onIncludeClosed,
  status,
}: {
  cards: Card[];
  people: Person[];
  /** The workspace's repositories. */
  repositories: string[];
  filter: CardFilter;
  onFilter: (filter: CardFilter) => void;
  includeClosed: boolean;
  onIncludeClosed: (include: boolean) => void;
  status: ReactNode;
}) {
  const t = useT();
  const assigneeNames = useMemo(
    () =>
      [
        ...new Set(
          cards
            .flatMap((c) => (c.assignee ? [c.assignee.name] : []))
            .concat(people.map((p) => p.name)),
        ),
      ].sort(),
    [cards, people],
  );
  const labels = useMemo(() => [...new Set(cards.flatMap((c) => c.labels))].sort(), [cards]);
  return (
    <form
      className="board-toolbar"
      role="search"
      aria-label={t("board.filters")}
      onSubmit={(e) => e.preventDefault()}
    >
      <TextInput
        className="board-search"
        aria-label={t("board.filterText")}
        placeholder={t("board.filterTextPlaceholder")}
        leftSection={<Search size={16} />}
        value={filter.text}
        onChange={(e) => onFilter({ ...filter, text: e.currentTarget.value })}
      />
      <MultiSelect
        aria-label={t("board.filterAssignee")}
        w={220}
        placeholder={
          filter.assignees.length > 0
            ? undefined
            : `${t("board.filterAssignee")}: ${t("board.any")}`
        }
        value={filter.assignees}
        onChange={(assignees) => onFilter({ ...filter, assignees })}
        clearable
        data={[
          ...assigneeNames,
          { group: " ", items: [{ value: NOBODY, label: t("board.unassigned") }] },
        ]}
      />
      <Select
        aria-label={t("board.filterStatus")}
        w={180}
        value={filter.lane || ANY}
        onChange={(v) => onFilter({ ...filter, lane: LANES.find((lane) => lane === v) ?? "" })}
        allowDeselect={false}
        data={[
          { value: ANY, label: `${t("board.filterStatus")}: ${t("board.any")}` },
          ...LANES.map((lane) => ({ value: lane, label: t(`lane.${lane}`) })),
        ]}
      />
      <Select
        aria-label={t("board.filterLabel")}
        w={180}
        value={filter.label || ANY}
        onChange={(v) => onFilter({ ...filter, label: !v || v === ANY ? "" : v })}
        allowDeselect={false}
        data={[
          { value: ANY, label: `${t("board.filterLabel")}: ${t("board.any")}` },
          ...labels.map((l) => ({ value: l, label: l })),
        ]}
      />
      {repositories.length > 1 && (
        <Select
          aria-label={t("board.repository")}
          w={200}
          value={filter.repository || ANY}
          onChange={(v) => onFilter({ ...filter, repository: !v || v === ANY ? "" : v })}
          allowDeselect={false}
          data={[
            { value: ANY, label: `${t("board.repository")}: ${t("board.any")}` },
            ...repositories.map((r) => ({ value: r, label: r })),
          ]}
        />
      )}
      <Checkbox
        label={t("board.filterInteractive")}
        checked={filter.interactive}
        onChange={(e) => onFilter({ ...filter, interactive: e.currentTarget.checked })}
      />
      <Checkbox
        label={t("board.includeClosed")}
        checked={includeClosed}
        disabled={filter.lane === "done"}
        onChange={(e) => onIncludeClosed(e.currentTarget.checked)}
      />
      <Text size="xs" c="dimmed" aria-live="polite" role="status" className="board-status">
        {status}
      </Text>
    </form>
  );
}
