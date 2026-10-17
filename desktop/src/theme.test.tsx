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

function badgeColors(style: CSSStyleDeclaration) {
  return ["--badge-bg", "--badge-color", "--badge-bd"].map((name) => style.getPropertyValue(name));
}

const TONES = ["success", "warning", "danger", "info"];

describe("app theme", () => {
  it.each([
    ["neutral", "var(--color-bg-hover)", "var(--color-text-subtle)", "var(--color-border-subtle)"],
    [
      "info",
      "var(--color-bg-hover-subtle)",
      "var(--color-text-subtle)",
      "var(--color-border-subtle)",
    ],
    ...["success", "warning", "danger"].map((tone) => [
      tone,
      `var(--color-${tone}-bg)`,
      `var(--color-${tone}-text)`,
      `var(--color-${tone}-border)`,
    ]),
  ])("gives a %s badge its tokens and keeps its text as written", (color, bg, text, border) => {
    renderThemed(
      <Badge color={color} variant="light">
        codex
      </Badge>,
    );

    const style = rootStyle("codex", ".mantine-Badge-root");
    expect(style.textTransform).toBe("none");
    expect(style.getPropertyValue("--badge-radius")).toBe("6px");
    expect(badgeColors(style)).toEqual([bg, text, `1px solid ${border}`]);
  });

  it("gives an outline badge its tokens whatever its colour", () => {
    renderThemed(
      <Badge color="danger" variant="outline">
        memory
      </Badge>,
    );

    expect(badgeColors(rootStyle("memory", ".mantine-Badge-root"))).toEqual([
      "transparent",
      "var(--color-text-muted)",
      "1px solid var(--color-border)",
    ]);
  });

  it("leaves Mantine's colours on other badges but keeps their text as written", () => {
    renderThemed(
      <Badge color="gray" variant="light">
        skill
      </Badge>,
    );

    const style = rootStyle("skill", ".mantine-Badge-root");
    expect(style.textTransform).toBe("none");
    expect(style.getPropertyValue("--badge-bg")).toBe("var(--mantine-color-gray-light)");
  });

  it("outlines a light danger button and fills a filled one with the danger tokens", () => {
    renderThemed(
      <>
        <Button color="danger" variant="light">
          Delete
        </Button>
        <Button color="danger">Force quit</Button>
        <Button color="danger" variant="default">
          Discard
        </Button>
      </>,
    );

    const light = rootStyle("Delete", ".mantine-Button-root");
    expect(light.getPropertyValue("--button-bg")).toBe("transparent");
    expect(light.getPropertyValue("--button-color")).toBe("var(--color-danger)");
    expect(light.getPropertyValue("--button-bd")).toBe("1px solid var(--color-danger-border)");
    expect(light.getPropertyValue("--button-hover")).toBe("var(--color-danger-bg)");

    const filled = rootStyle("Force quit", ".mantine-Button-root");
    expect(filled.getPropertyValue("--button-bg")).toBe("var(--color-danger)");
    expect(filled.getPropertyValue("--button-color")).toBe("var(--mantine-color-white)");
    expect(filled.getPropertyValue("--button-hover")).toBe("var(--literal-red-700)");

    const other = rootStyle("Discard", ".mantine-Button-root");
    expect(other.getPropertyValue("--button-bg")).not.toContain("--color-danger");
  });

  it.each(TONES)("draws a %s alert, its left line, and its icon in its tone", (tone) => {
    renderThemed(
      <Alert color={tone} title="Environment" icon={<span>!</span>}>
        Not ready
      </Alert>,
    );

    const style = rootStyle("Not ready", ".mantine-Alert-root");
    expect(style.getPropertyValue("--alert-bg")).toBe(`var(--color-${tone}-bg)`);
    expect(style.getPropertyValue("--alert-bd")).toBe(`1px solid var(--color-${tone}-border)`);
    expect(style.getPropertyValue("--alert-color")).toBe(`var(--color-${tone}-text)`);
    expect(style.borderLeftWidth).toBe("4px");
    expect(style.borderLeftColor).toBe(`var(--color-${tone})`);
    expect(rootStyle("Not ready", ".mantine-Alert-message").color).toBe(
      `var(--color-${tone}-text)`,
    );
    expect(rootStyle("!", ".mantine-Alert-icon").color).toBe(`var(--color-${tone})`);
  });

  it("draws other alerts on the panel colours with a neutral line and icon", () => {
    renderThemed(
      <Alert color="yellow" title="Live" icon={<span>?</span>}>
        Watching
      </Alert>,
    );

    const style = rootStyle("Watching", ".mantine-Alert-root");
    expect(style.getPropertyValue("--alert-bg")).toBe("var(--color-bg-panel-subtle)");
    expect(style.getPropertyValue("--alert-bd")).toBe("1px solid var(--color-border-subtle)");
    expect(style.getPropertyValue("--alert-color")).toBe("var(--color-text-main)");
    expect(style.borderLeftColor).toBe("var(--color-border)");
    expect(rootStyle("Watching", ".mantine-Alert-message").color).toBe("var(--color-text-subtle)");
    expect(rootStyle("?", ".mantine-Alert-icon").color).toBe("");
  });

  it("gives every alert the panel's radius, padding, and shadow", () => {
    renderThemed(<Alert color="danger">Failed</Alert>);

    const style = rootStyle("Failed", ".mantine-Alert-root");
    expect(style.getPropertyValue("--alert-radius")).toBe("10px");
    expect(style.padding).toBe("12px 16px");
    expect(style.boxShadow).toBe("var(--shadow-premium)");
  });
});
