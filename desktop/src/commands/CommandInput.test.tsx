import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { uploadCommandInputFile } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { appendCommandInputPath, CommandInput } from "./CommandInput";
import { TestMantineProvider } from "../test/TestMantineProvider";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, uploadCommandInputFile: vi.fn() };
});

const uploadMock = vi.mocked(uploadCommandInputFile);
const t = i18n.getFixedT("en");
const selectedUpload = vi.fn();

function ControlledInput({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <CommandInput
      aria-label={t("commands.message")}
      required={false}
      value={value}
      onChange={setValue}
      onUpload={selectedUpload}
    />
  );
}

function renderInput(initial = "") {
  return render(
    <TestMantineProvider>
      <ControlledInput initial={initial} />
    </TestMantineProvider>,
  );
}

describe("CommandInput", () => {
  // The browser has no path the server machine could read: a dropped or
  // pasted file is handed over as an uploaded copy, and a folder is refused.
  function fileItem(file: File, directory = false) {
    return {
      kind: "file",
      getAsFile: () => file,
      webkitGetAsEntry: () => ({ isDirectory: directory }),
    };
  }

  function dropItems(target: HTMLElement, items: ReturnType<typeof fileItem>[]) {
    const dataTransfer = { types: ["Files"], items };
    fireEvent.dragOver(target, { dataTransfer });
    fireEvent.drop(target, { dataTransfer });
  }

  it("uploads each dropped file and appends the copies' paths", async () => {
    uploadMock
      .mockResolvedValueOnce({
        id: "upload-id",
        path: "/in/a-notes.md",
        guest_path: "/in/a-notes.md",
      })
      .mockResolvedValueOnce({
        id: "upload-id",
        path: "/in/b-shot.png",
        guest_path: "/in/b-shot.png",
      });
    renderInput("describe");
    const input = screen.getByRole("textbox", { name: t("commands.message") });
    const notes = new File(["# notes"], "notes.md", { type: "text/markdown" });
    const shot = new File(["pixels"], "shot.png", { type: "image/png" });

    dropItems(input, [fileItem(notes), fileItem(shot)]);

    await waitFor(() => expect(input).toHaveValue("describe\n/in/a-notes.md\n/in/b-shot.png"));
    expect(selectedUpload).toHaveBeenCalledWith("upload-id", "/in/a-notes.md");
    expect(uploadMock).toHaveBeenCalledWith(notes);
    expect(uploadMock).toHaveBeenCalledWith(shot);
  });

  it("refuses a dropped folder, asking for its server path, and still uploads the files", async () => {
    uploadMock.mockResolvedValue({ id: "upload-id", path: "/in/a.md", guest_path: "/in/a.md" });
    renderInput();
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    dropItems(input, [fileItem(new File([], "project"), true), fileItem(new File(["a"], "a.md"))]);

    expect(await screen.findByRole("alert")).toHaveTextContent(t("commands.inputFolderRefused"));
    await waitFor(() => expect(input).toHaveValue("/in/a.md"));
    expect(uploadMock).toHaveBeenCalledTimes(1);
  });

  it("says the drop hands over a copy while a file is dragged over", () => {
    renderInput();
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    fireEvent.dragOver(input, { dataTransfer: { types: ["Files"], items: [] } });

    expect(screen.getByRole("status")).toHaveTextContent(t("commands.inputFileDropActive"));
  });

  it("uploads a pasted image and appends the returned temporary path", async () => {
    uploadMock.mockResolvedValue({
      path: "/workspace/.guildbotics/data/input.png",
      id: "upload-id",
      guest_path: "/workspace/.guildbotics/data/input.png",
    });
    renderInput("inspect");
    const input = screen.getByRole("textbox", { name: t("commands.message") });
    const image = new File(["pixels"], "clipboard.png", { type: "image/png" });

    fireEvent.paste(input, {
      clipboardData: {
        items: [{ kind: "file", type: "image/png", getAsFile: () => image }],
      },
    });

    await waitFor(() => expect(uploadMock).toHaveBeenCalledWith(image));
    await waitFor(() =>
      expect(input).toHaveValue("inspect\n/workspace/.guildbotics/data/input.png"),
    );
  });

  it("shows an upload failure without replacing the existing input", async () => {
    uploadMock.mockRejectedValue(new Error("disk full"));
    renderInput("keep this");
    const input = screen.getByRole("textbox", { name: t("commands.message") });
    const image = new File(["pixels"], "clipboard.png", { type: "image/png" });

    fireEvent.paste(input, {
      clipboardData: {
        items: [{ kind: "file", type: "image/png", getAsFile: () => image }],
      },
    });

    expect(await screen.findByRole("alert")).toHaveTextContent("disk full");
    expect(input).toHaveValue("keep this");
  });
});

describe("appendCommandInputPath", () => {
  it("preserves path spaces and avoids an extra blank line", () => {
    expect(appendCommandInputPath("existing\n", "/tmp/file name.pdf")).toBe(
      "existing\n/tmp/file name.pdf",
    );
    expect(appendCommandInputPath("existing", "/tmp/a.md")).toBe("existing\n/tmp/a.md");
    expect(appendCommandInputPath("", "/tmp/a.md")).toBe("/tmp/a.md");
  });
});
