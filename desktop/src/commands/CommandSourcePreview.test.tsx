import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import "../i18n";
import { CommandSourcePreview } from "./CommandSourcePreview";
import { TestMantineProvider } from "../test/TestMantineProvider";

describe("CommandSourcePreview", () => {
  it("renders source in a focusable scroll region", () => {
    render(
      <TestMantineProvider>
        <CommandSourcePreview
          path="/commands/functions/prepare-translation-input.py"
          source={'first\nsecond\nprint("a long line")\n'}
        />
      </TestMantineProvider>,
    );

    const source = screen.getByRole("region", {
      name: "Source of /commands/functions/prepare-translation-input.py",
    });
    expect(source).toHaveClass("command-source-preview-scroll");
    expect(source).toHaveAttribute("tabindex", "0");
    expect(source).toHaveTextContent('print("a long line")');
  });
});
