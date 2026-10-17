import { Alert, Badge, Button, Card, createTheme } from "@mantine/core";

import { comboboxThemeComponents } from "./comboboxTheme";

/**
 * Colours with a semantic token set in `styles.css` (`--color-<tone>`,
 * `-bg`, `-border`, `-text`).
 *
 * Mantine does not put the `color` prop on the element as an attribute, so a
 * stylesheet cannot select by colour. Every colour-dependent look is decided
 * here, through the CSS variables and styles Mantine applies to the element
 * itself.
 */
const TONES = ["success", "warning", "danger", "info"] as const;

const toneOf = (color: string | undefined) => TONES.find((tone) => tone === color);

const badgeColors = (background: string, text: string, border: string) => ({
  "--badge-bg": background,
  "--badge-color": text,
  "--badge-bd": `1px solid ${border}`,
});

const toneBadge = (tone: (typeof TONES)[number]) =>
  badgeColors(
    `var(--color-${tone}-bg)`,
    `var(--color-${tone}-text)`,
    `var(--color-${tone}-border)`,
  );

// Neutral and info badges label things rather than report a state, so they stay quiet.
const BADGE_COLORS: Record<string, ReturnType<typeof badgeColors>> = {
  neutral: badgeColors(
    "var(--color-bg-hover)",
    "var(--color-text-subtle)",
    "var(--color-border-subtle)",
  ),
  info: badgeColors(
    "var(--color-bg-hover-subtle)",
    "var(--color-text-subtle)",
    "var(--color-border-subtle)",
  ),
  success: toneBadge("success"),
  warning: toneBadge("warning"),
  danger: toneBadge("danger"),
};

const OUTLINE_BADGE = badgeColors("transparent", "var(--color-text-muted)", "var(--color-border)");

// A danger button outlines its action instead of filling it, unless it is the filled one.
const DANGER_BUTTONS: Record<string, Record<string, string>> = {
  light: {
    "--button-bg": "transparent",
    "--button-color": "var(--color-danger)",
    "--button-bd": "1px solid var(--color-danger-border)",
    "--button-hover": "var(--color-danger-bg)",
    "--button-hover-color": "var(--color-danger)",
  },
  filled: {
    "--button-bg": "var(--color-danger)",
    "--button-color": "var(--mantine-color-white)",
    "--button-hover": "var(--literal-red-700)",
    "--button-hover-color": "var(--mantine-color-white)",
  },
};

export const theme = createTheme({
  primaryColor: "teal",
  defaultRadius: "md",
  colors: {
    neutral: [
      "#f8fafc",
      "#f1f5f9",
      "#e2e8f0",
      "#cbd5e1",
      "#94a3b8",
      "#64748b",
      "#475569",
      "#334155",
      "#1e293b",
      "#0f172a",
    ],
    success: [
      "#ecfdf5",
      "#d1fae5",
      "#a7f3d0",
      "#6ee7b7",
      "#34d399",
      "#10b981",
      "#059669",
      "#047857",
      "#065f46",
      "#022c22",
    ],
    warning: [
      "#fffbeb",
      "#fef3c7",
      "#fde68a",
      "#fcd34d",
      "#fbbf24",
      "#f59e0b",
      "#d97706",
      "#b45309",
      "#78350f",
      "#451a03",
    ],
    danger: [
      "#fef2f2",
      "#fee2e2",
      "#fecaca",
      "#fca5a5",
      "#f87171",
      "#ef4444",
      "#dc2626",
      "#b91c1c",
      "#991b1b",
      "#7f1d1d",
    ],
    info: [
      "#f5f3ff",
      "#ede9fe",
      "#ddd6fe",
      "#c4b5fd",
      "#a78bfa",
      "#8b5cf6",
      "#7c3aed",
      "#6d28d9",
      "#5b21b6",
      "#4c1d95",
    ],
  },
  components: {
    ...comboboxThemeComponents,
    Card: Card.extend({
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
    }),
    Alert: Alert.extend({
      vars: (_theme, { color }) => {
        const tone = toneOf(color);
        return {
          root: {
            "--alert-bg": tone ? `var(--color-${tone}-bg)` : "var(--color-bg-panel-subtle)",
            "--alert-bd": `1px solid ${tone ? `var(--color-${tone}-border)` : "var(--color-border-subtle)"}`,
            "--alert-color": tone ? `var(--color-${tone}-text)` : "var(--color-text-main)",
            "--alert-radius": "10px",
          },
        };
      },
      styles: (_theme, { color }) => {
        const tone = toneOf(color);
        return {
          root: {
            borderLeftWidth: "4px",
            borderLeftColor: tone ? `var(--color-${tone})` : "var(--color-border)",
            padding: "12px 16px",
            boxShadow: "var(--shadow-premium)",
          },
          message: { color: tone ? `var(--color-${tone}-text)` : "var(--color-text-subtle)" },
          icon: tone ? { color: `var(--color-${tone})` } : {},
        };
      },
    }),
    Badge: Badge.extend({
      vars: (_theme, { color, variant }) => ({
        root: {
          "--badge-radius": "6px",
          ...(variant === "outline" ? OUTLINE_BADGE : BADGE_COLORS[color ?? ""]),
        },
      }),
      styles: { root: { textTransform: "none" } },
    }),
    Button: Button.extend({
      vars: (_theme, { color, variant }) => ({
        root: color === "danger" ? DANGER_BUTTONS[variant ?? "filled"] : {},
      }),
    }),
  },
});
