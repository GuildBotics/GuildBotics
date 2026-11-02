import { Checkbox, Select, Text, TextInput } from "@mantine/core";
import { Search } from "lucide-react";
import { useMemo, type ReactNode } from "react";
import type { Card } from "../../api/types";
import type { Person } from "../../components/PeoplePicker";
import { useT } from "../../i18n";
import type { CardFilter } from "./ordering";

/** Select values for "any" and "nobody" in the filters (Mantine needs a value). */
const ANY = "*";
const NOBODY = "-";

/**
 * The filters over a workspace's cards (its board, the relations of its
 * tickets): text, assignee, label, and whether closed tickets are shown;
 * `status` follows them.
 */
export function CardFilters({
  cards,
  people,
  filter,
  onFilter,
  includeClosed,
  onIncludeClosed,
  status,
}: {
  cards: Card[];
  people: Person[];
  filter: CardFilter;
  onFilter: (filter: CardFilter) => void;
  includeClosed: boolean;
  onIncludeClosed: (include: boolean) => void;
  status: ReactNode;
}) {
  const t = useT();
  const assigneeNames = useMemo(
    () => [...new Set(cards.flatMap((c) => (c.assignee ? [c.assignee.name] : [])).concat(people.map((p) => p.name)))].sort(),
    [cards, people],
  );
  const labels = useMemo(() => [...new Set(cards.flatMap((c) => c.labels))].sort(), [cards]);
  return (
    <form className="board-toolbar" role="search" aria-label={t("board.filters")} onSubmit={(e) => e.preventDefault()}>
      <TextInput
        className="board-search"
        aria-label={t("board.filterText")}
        placeholder={t("board.filterTextPlaceholder")}
        leftSection={<Search size={16} />}
        value={filter.text}
        onChange={(e) => onFilter({ ...filter, text: e.currentTarget.value })}
      />
      <Select
        aria-label={t("board.filterAssignee")}
        w={190}
        value={filter.assignee || ANY}
        onChange={(v) => onFilter({ ...filter, assignee: !v || v === ANY ? "" : v })}
        allowDeselect={false}
        data={[
          { value: ANY, label: `${t("board.filterAssignee")}: ${t("board.any")}` },
          ...assigneeNames.map((n) => ({ value: n, label: n })),
          { group: " ", items: [{ value: NOBODY, label: t("board.unassigned") }] },
        ]}
      />
      <Select
        aria-label={t("board.filterLabel")}
        w={180}
        value={filter.label || ANY}
        onChange={(v) => onFilter({ ...filter, label: !v || v === ANY ? "" : v })}
        allowDeselect={false}
        data={[{ value: ANY, label: `${t("board.filterLabel")}: ${t("board.any")}` }, ...labels.map((l) => ({ value: l, label: l }))]}
      />
      <Checkbox label={t("board.includeClosed")} checked={includeClosed} onChange={(e) => onIncludeClosed(e.currentTarget.checked)} />
      <Text size="xs" c="dimmed" aria-live="polite" role="status" className="board-status">
        {status}
      </Text>
    </form>
  );
}
