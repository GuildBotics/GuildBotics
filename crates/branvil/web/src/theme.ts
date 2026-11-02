// The Mantine theme: the same as GuildBotics Desktop's (desktop/src/main.tsx),
// so Branvil looks like a part of it. Colors, cards, and alerts come from the
// tokens in styles.css.

import { createTheme, type MantineTheme } from "@mantine/core";

type Tone = "warning" | "danger" | "success" | "info";
const TONES: Tone[] = ["warning", "danger", "success", "info"];

export const theme = createTheme({
  primaryColor: "teal",
  defaultRadius: "md",
  colors: {
    neutral: ["#f8fafc", "#f1f5f9", "#e2e8f0", "#cbd5e1", "#94a3b8", "#64748b", "#475569", "#334155", "#1e293b", "#0f172a"],
    success: ["#ecfdf5", "#d1fae5", "#a7f3d0", "#6ee7b7", "#34d399", "#10b981", "#059669", "#047857", "#065f46", "#022c22"],
    warning: ["#fffbeb", "#fef3c7", "#fde68a", "#fcd34d", "#fbbf24", "#f59e0b", "#d97706", "#b45309", "#78350f", "#451a03"],
    danger: ["#fef2f2", "#fee2e2", "#fecaca", "#fca5a5", "#f87171", "#ef4444", "#dc2626", "#b91c1c", "#991b1b", "#7f1d1d"],
    info: ["#f5f3ff", "#ede9fe", "#ddd6fe", "#c4b5fd", "#a78bfa", "#8b5cf6", "#7c3aed", "#6d28d9", "#5b21b6", "#4c1d95"],
  },
  components: {
    // GuildBotics' styles.css means badges to keep their case (logins and
    // branch names are shown as they are); its overrides select on a
    // `data-color` attribute Mantine 9 no longer sets, so it is said here.
    Badge: { styles: { root: { textTransform: "none" } } },
    Select: { defaultProps: { comboboxProps: { hideDetached: false } } },
    Card: {
      styles: {
        root: {
          backgroundColor: "var(--color-bg-panel-subtle)",
          borderColor: "var(--color-border-subtle)",
          borderWidth: "1px",
          borderStyle: "solid",
          boxShadow: "var(--shadow-premium)",
          borderRadius: "10px",
          transition: "box-shadow 0.2s ease, border-color 0.2s ease",
        },
      },
    },
    Alert: {
      styles: (_theme: MantineTheme, props: { color?: string }) => {
        const tone = TONES.find((c) => c === props.color);
        return {
          root: {
            backgroundColor: tone ? `var(--color-${tone}-bg)` : "var(--color-bg-panel-subtle)",
            borderColor: tone ? `var(--color-${tone}-border)` : "var(--color-border-subtle)",
            borderWidth: "1px",
            borderStyle: "solid",
            borderLeft: "4px solid var(--alert-indicator-color, var(--color-border))",
            borderRadius: "10px",
            color: tone ? `var(--color-${tone}-text)` : "var(--color-text-main)",
            padding: "12px 16px",
            boxShadow: "var(--shadow-premium)",
          },
          title: { color: tone ? `var(--color-${tone}-text)` : "var(--color-text-main)", fontWeight: 700 },
          message: { color: tone ? `var(--color-${tone}-text)` : "var(--color-text-subtle)" },
        };
      },
    },
  },
});
