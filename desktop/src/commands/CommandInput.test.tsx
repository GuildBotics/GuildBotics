import { MantineProvider } from "@mantine/core";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { uploadCommandInputFile } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { appendCommandInputPaths, CommandInput } from "./CommandInput";

const tauriDrag = vi.hoisted(() => ({
  handler: null as ((event: unknown) => void) | null,
  unlisten: vi.fn(),
}));

vi.mock("@tauri-apps/api/webview", () => ({
  getCurrentWebview: () => ({
    onDragDropEvent: vi.fn(async (handler: (event: unknown) => void) => {
      tauriDrag.handler = handler;
      return tauriDrag.unlisten;
    }),
  }),
}));

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, uploadCommandInputFile: vi.fn() };
});

const uploadMock = vi.mocked(uploadCommandInputFile);
const t = i18n.getFixedT("en");

function ControlledInput({ initial = "" }: { initial?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <CommandInput
      aria-label={t("commands.message")}
      required={false}
      value={value}
      onChange={setValue}
    />
  );
}

function renderInput(initial = "") {
  return render(
    <MantineProvider>
      <ControlledInput initial={initial} />
    </MantineProvider>,
  );
}

describe("CommandInput", () => {
  beforeEach(() => {
    uploadMock.mockReset();
    tauriDrag.handler = null;
    tauriDrag.unlisten.mockReset();
    Object.defineProperty(window, "__TAURI_INTERNALS__", {
      configurable: true,
      value: {},
    });
  });

  it("appends dropped native file paths when the pointer is over the input", async () => {
    renderInput("OCR these files:");
    const input = screen.getByRole("textbox", { name: t("commands.message") });
    vi.spyOn(input, "getBoundingClientRect").mockReturnValue({
      bottom: 110,
      height: 100,
      left: 10,
      right: 210,
      top: 10,
      width: 200,
      x: 10,
      y: 10,
      toJSON: () => ({}),
    });
    await waitFor(() => expect(tauriDrag.handler).not.toBeNull());

    act(() => {
      tauriDrag.handler?.({
        payload: {
          type: "drop",
          paths: ["/tmp/scan one.pdf", "/tmp/page.png"],
          position: { x: 20, y: 20 },
        },
      });
    });

    expect(input).toHaveValue("OCR these files:\n/tmp/scan one.pdf\n/tmp/page.png");
  });

  it("uploads a pasted image and appends the returned temporary path", async () => {
    uploadMock.mockResolvedValue({ path: "/workspace/.guildbotics/data/input.png" });
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

describe("appendCommandInputPaths", () => {
  it("preserves path spaces and avoids an extra blank line", () => {
    expect(appendCommandInputPaths("existing\n", ["/tmp/file name.pdf"])).toBe(
      "existing\n/tmp/file name.pdf",
    );
  });
});
