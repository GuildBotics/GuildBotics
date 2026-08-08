import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import "../i18n";
import { buildCommandSourceDiff, CommandSourceDiff } from "./CommandSourceDiff";

describe("buildCommandSourceDiff", () => {
  it("identifies unchanged, removed, and added lines", () => {
    const lines = buildCommandSourceDiff("first\nold\nlast\n", "first\nnew\nlast\n");

    expect(lines).toEqual([
      { kind: "context", oldLine: 1, newLine: 1, text: "first" },
      { kind: "deletion", oldLine: 2, text: "old" },
      { kind: "addition", newLine: 2, text: "new" },
      { kind: "context", oldLine: 3, newLine: 3, text: "last" },
    ]);
  });

  it("handles an empty source", () => {
    expect(buildCommandSourceDiff("", "created\n")).toEqual([
      { kind: "addition", newLine: 1, text: "created" },
    ]);
    expect(buildCommandSourceDiff("deleted\n", "")).toEqual([
      { kind: "deletion", oldLine: 1, text: "deleted" },
    ]);
  });

  it("keeps repeated context while finding an insertion", () => {
    expect(buildCommandSourceDiff("same\nsame\nlast", "same\ninserted\nsame\nlast")).toEqual([
      { kind: "context", oldLine: 1, newLine: 1, text: "same" },
      { kind: "addition", newLine: 2, text: "inserted" },
      { kind: "context", oldLine: 2, newLine: 3, text: "same" },
      { kind: "context", oldLine: 3, newLine: 4, text: "last" },
    ]);
  });
});

describe("CommandSourceDiff", () => {
  it("renders a labelled, scrollable update diff", () => {
    const { container } = render(
      <MantineProvider env="test">
        <CommandSourceDiff before="old\n" after="new\n" path="/commands/translate.md" />
      </MantineProvider>,
    );

    const diff = screen.getByRole("table", { name: "Changes to /commands/translate.md" });
    expect(diff).toHaveClass("command-source-diff-scroll");
    expect(diff).toHaveAttribute("tabindex", "0");
    expect(container.querySelector('[data-diff-kind="deletion"]')).toHaveTextContent("−old");
    expect(container.querySelector('[data-diff-kind="addition"]')).toHaveTextContent("+new");
  });
});
