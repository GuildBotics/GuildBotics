// Moving between the days (or weeks) a time chart shows.
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";

export function startOfLocalDay(date: Date): Date {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate());
}

export function addDays(date: Date, days: number): Date {
  const next = new Date(date);
  next.setDate(next.getDate() + days);
  return next;
}

function toDateInputValue(date: Date): string {
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

/** The previous and next buttons around the shown range, opening a date picker. */
export function DateNav({
  label,
  value,
  onShift,
  onSelect,
}: {
  label: string;
  value: Date;
  onShift: (direction: -1 | 1) => void;
  onSelect: (day: Date) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="activity-date-nav">
      <button
        type="button"
        className="activity-icon-button"
        aria-label={t("activity.previous")}
        onClick={() => onShift(-1)}
      >
        <ChevronLeft size={16} />
      </button>
      <label className="activity-date-label">
        <span>{label}</span>
        <input
          aria-label={t("activity.date")}
          type="date"
          value={toDateInputValue(value)}
          onChange={(event) => {
            const [year, month, day] = event.currentTarget.value.split("-").map(Number);
            if (year && month && day) onSelect(new Date(year, month - 1, day));
          }}
        />
      </label>
      <button
        type="button"
        className="activity-icon-button"
        aria-label={t("activity.next")}
        onClick={() => onShift(1)}
      >
        <ChevronRight size={16} />
      </button>
    </div>
  );
}
