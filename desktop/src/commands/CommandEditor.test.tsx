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
        path="/workspace/.guildbotics/config/commands/x.md"
        onChange={vi.fn()}
        onSave={vi.fn()}
      />
    </MantineProvider>,
  );
}

describe("CommandEditor", () => {
  it("renders the display path and editor surface", () => {
    renderEditor("markdown");

    expect(screen.getByText("/workspace/.guildbotics/config/commands/x.md")).toBeInTheDocument();
    expect(screen.getByTestId("command-editor")).toBeInTheDocument();
  });

  it("mounts for every supported format without crashing", () => {
    for (const format of ["markdown", "python", "shell", "yaml"] as CommandFileFormat[]) {
      const { unmount } = renderEditor(format);
      expect(screen.getByTestId("command-editor")).toBeInTheDocument();
      unmount();
    }
  });
});
