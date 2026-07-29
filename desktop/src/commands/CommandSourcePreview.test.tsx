import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import "../i18n";
import { CommandSourcePreview } from "./CommandSourcePreview";

describe("CommandSourcePreview", () => {
  it("renders source in a focusable scroll region", () => {
    render(
      <MantineProvider>
        <CommandSourcePreview
          path="/commands/functions/prepare-translation-input.py"
          source={'first\nsecond\nprint("a long line")\n'}
        />
      </MantineProvider>,
    );

    const source = screen.getByRole("region", {
      name: "Source of /commands/functions/prepare-translation-input.py",
    });
    expect(source).toHaveClass("command-source-preview-scroll");
    expect(source).toHaveAttribute("tabindex", "0");
    expect(source).toHaveTextContent('print("a long line")');
  });
});
