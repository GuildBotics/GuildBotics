import React from "react";
import ReactDOM from "react-dom/client";
import { MantineProvider, createTheme, MantineTheme } from "@mantine/core";
import { Notifications } from "@mantine/notifications";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { Bootstrap } from "./Bootstrap";
import { stopBackend } from "./api/backend";
import "./i18n";
import "@mantine/core/styles.css";
import "@mantine/notifications/styles.css";
import "./styles.css";

const queryClient = new QueryClient();
const theme = createTheme({
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
        if (props.color === "warning") {
          return {
            root: {
              backgroundColor: "var(--color-warning-bg)",
              borderColor: "var(--color-warning-border)",
              borderWidth: "1px",
              borderStyle: "solid",
              color: "var(--color-warning-text)",
            },
            title: {
              color: "var(--color-warning-text)",
            },
          };
        }
        if (props.color === "danger") {
          return {
            root: {
              backgroundColor: "var(--color-danger-bg)",
              borderColor: "var(--color-danger-border)",
              borderWidth: "1px",
              borderStyle: "solid",
              color: "var(--color-danger-text)",
            },
            title: {
              color: "var(--color-danger-text)",
            },
          };
        }
        if (props.color === "success") {
          return {
            root: {
              backgroundColor: "var(--color-success-bg)",
              borderColor: "var(--color-success-border)",
              borderWidth: "1px",
              borderStyle: "solid",
              color: "var(--color-success-text)",
            },
            title: {
              color: "var(--color-success-text)",
            },
          };
        }
        if (props.color === "info") {
          return {
            root: {
              backgroundColor: "var(--color-info-bg)",
              borderColor: "var(--color-info-border)",
              borderWidth: "1px",
              borderStyle: "solid",
              color: "var(--color-info-text)",
            },
            title: {
              color: "var(--color-info-text)",
            },
          };
        }
        return {};
      },
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="auto">
      <Notifications position="top-right" />
      <QueryClientProvider client={queryClient}>
        <Bootstrap />
      </QueryClientProvider>
    </MantineProvider>
  </React.StrictMode>,
);

window.addEventListener("beforeunload", () => {
  void stopBackend();
});
