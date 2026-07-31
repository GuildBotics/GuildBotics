import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import type { CommandFileFormat } from "../api/client";
import { CommandEditor } from "./CommandEditor";

function renderEditor(format: CommandFileFormat) {
  return render(
    <MantineProvider>
      <CommandEditor
        value={"---\nname: X\n---\nbody"}
        format={format}
        onChange={vi.fn()}
        onSave={vi.fn()}
      />
    </MantineProvider>,
  );
}

describe("CommandEditor", () => {
  it("renders only the editor surface; the path lives in the command bar", () => {
    renderEditor("markdown");

    expect(screen.getByTestId("command-editor")).toBeInTheDocument();
    expect(
      screen.queryByText("/workspace/.guildbotics/config/commands/x.md"),
    ).not.toBeInTheDocument();
  });

  it("mounts for every supported format without crashing", () => {
    for (const format of ["markdown", "python", "shell", "yaml"] as CommandFileFormat[]) {
      const { unmount } = renderEditor(format);
      expect(screen.getByTestId("command-editor")).toBeInTheDocument();
      unmount();
    }
  });
});
