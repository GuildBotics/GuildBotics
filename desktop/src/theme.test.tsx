import { Alert, Badge, Button } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import { TestMantineProvider } from "./test/TestMantineProvider";
import { theme } from "./theme";

function renderThemed(ui: ReactNode) {
  return render(<TestMantineProvider theme={theme}>{ui}</TestMantineProvider>);
}

function rootStyle(text: string, selector: string) {
  const root = screen.getByText(text).closest<HTMLElement>(selector);
  if (!root) {
    throw new Error(`no ${selector} around ${text}`);
  }
  return root.style;
}

describe("app theme", () => {
  it("keeps badge text as written and gives each badge colour its tokens", () => {
    renderThemed(
      <>
        <Badge color="neutral" variant="light">
          codex
        </Badge>
        <Badge color="warning" variant="light">
          pending
        </Badge>
        <Badge color="danger" variant="outline">
          memory
        </Badge>
      </>,
    );

    const neutral = rootStyle("codex", ".mantine-Badge-root");
    expect(neutral.textTransform).toBe("none");
    expect(neutral.getPropertyValue("--badge-radius")).toBe("6px");
    expect(neutral.getPropertyValue("--badge-bg")).toBe("var(--color-bg-hover)");
    expect(neutral.getPropertyValue("--badge-color")).toBe("var(--color-text-subtle)");
    expect(neutral.getPropertyValue("--badge-bd")).toBe("1px solid var(--color-border-subtle)");

    const warning = rootStyle("pending", ".mantine-Badge-root");
    expect(warning.getPropertyValue("--badge-bg")).toBe("var(--color-warning-bg)");
    expect(warning.getPropertyValue("--badge-color")).toBe("var(--color-warning-text)");
    expect(warning.getPropertyValue("--badge-bd")).toBe("1px solid var(--color-warning-border)");

    const outline = rootStyle("memory", ".mantine-Badge-root");
    expect(outline.getPropertyValue("--badge-bg")).toBe("transparent");
    expect(outline.getPropertyValue("--badge-color")).toBe("var(--color-text-muted)");
    expect(outline.getPropertyValue("--badge-bd")).toBe("1px solid var(--color-border)");
  });

  it("outlines a light danger button and fills a filled one with the danger tokens", () => {
    renderThemed(
      <>
        <Button color="danger" variant="light">
          Delete
        </Button>
        <Button color="danger">Force quit</Button>
      </>,
    );

    const light = rootStyle("Delete", ".mantine-Button-root");
    expect(light.getPropertyValue("--button-bg")).toBe("transparent");
    expect(light.getPropertyValue("--button-color")).toBe("var(--color-danger)");
    expect(light.getPropertyValue("--button-bd")).toBe("1px solid var(--color-danger-border)");
    expect(light.getPropertyValue("--button-hover")).toBe("var(--color-danger-bg)");

    const filled = rootStyle("Force quit", ".mantine-Button-root");
    expect(filled.getPropertyValue("--button-bg")).toBe("var(--color-danger)");
    expect(filled.getPropertyValue("--button-hover")).toBe("var(--literal-red-700)");
  });

  it("draws an alert's left line and icon in its tone, and leaves other alerts neutral", () => {
    renderThemed(
      <>
        <Alert color="warning" title="Environment" icon={<span>!</span>}>
          Not ready
        </Alert>
        <Alert color="yellow" title="Live" icon={<span>?</span>}>
          Watching
        </Alert>
      </>,
    );

    const warning = rootStyle("Not ready", ".mantine-Alert-root");
    expect(warning.borderLeftWidth).toBe("4px");
    expect(warning.borderLeftColor).toBe("var(--color-warning)");
    expect(warning.getPropertyValue("--alert-bg")).toBe("var(--color-warning-bg)");
    expect(warning.getPropertyValue("--alert-color")).toBe("var(--color-warning-text)");
    expect(rootStyle("!", ".mantine-Alert-icon").color).toBe("var(--color-warning)");

    const other = rootStyle("Watching", ".mantine-Alert-root");
    expect(other.borderLeftColor).toBe("var(--color-border)");
    expect(other.getPropertyValue("--alert-bg")).toBe("var(--color-bg-panel-subtle)");
    expect(rootStyle("?", ".mantine-Alert-icon").color).toBe("");
  });
});
