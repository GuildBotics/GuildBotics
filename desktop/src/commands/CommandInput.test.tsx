import { MantineProvider } from "@mantine/core";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  checkCommandInputPaths,
  copyCommandInputFile,
  uploadCommandInputFile,
  type CommandInputPathStatus,
} from "../api/client";
import { openMainWindow } from "../hotkeys/hotkeyRuntime";
import i18n from "../i18n";
import "../i18n";
import { appendCommandInputPaths, CommandInput, grantSettingsRoute } from "./CommandInput";

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
  return {
    ...actual,
    checkCommandInputPaths: vi.fn(),
    copyCommandInputFile: vi.fn(),
    uploadCommandInputFile: vi.fn(),
  };
});

vi.mock("../hotkeys/hotkeyRuntime", () => ({ openMainWindow: vi.fn(async () => {}) }));

const uploadMock = vi.mocked(uploadCommandInputFile);
const checkMock = vi.mocked(checkCommandInputPaths);
const copyMock = vi.mocked(copyCommandInputFile);
const openMainWindowMock = vi.mocked(openMainWindow);
const t = i18n.getFixedT("en");

function ControlledInput({ initial = "", cwd }: { initial?: string; cwd?: string }) {
  const [value, setValue] = useState(initial);
  return (
    <CommandInput
      aria-label={t("commands.message")}
      required={false}
      value={value}
      onChange={setValue}
      cwd={cwd}
    />
  );
}

function renderInput(initial = "", cwd?: string) {
  return render(
    <MantineProvider env="test">
      <ControlledInput initial={initial} cwd={cwd} />
    </MantineProvider>,
  );
}

/** Drop `paths` on the input once the Tauri listener is attached. */
async function drop(input: HTMLElement, paths: string[]) {
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
    tauriDrag.handler?.({ payload: { type: "drop", paths, position: { x: 20, y: 20 } } });
  });
}

function described(
  entries: [string, CommandInputPathStatus["kind"], boolean, CommandInputPathStatus["grant"]?][],
): CommandInputPathStatus[] {
  return entries.map(([path, kind, reachable, grant = null]) => ({
    path,
    kind,
    reachable,
    guest_path: path,
    grant,
  }));
}

describe("CommandInput", () => {
  beforeEach(() => {
    uploadMock.mockReset();
    checkMock.mockReset();
    copyMock.mockReset();
    openMainWindowMock.mockClear();
    Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: 1 });
    Object.defineProperty(navigator, "platform", { configurable: true, value: "" });
    tauriDrag.handler = null;
    tauriDrag.unlisten.mockReset();
    Object.defineProperty(window, "__TAURI_INTERNALS__", {
      configurable: true,
      value: {},
    });
  });

  it("appends dropped paths the environment reaches, asking about the working directory", async () => {
    checkMock.mockResolvedValue({
      paths: described([
        ["/tmp/scan one.pdf", "file", true],
        ["/tmp/page.png", "file", true],
      ]),
    });
    renderInput("OCR these files:", "/work/clone");
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    await drop(input, ["/tmp/scan one.pdf", "/tmp/page.png"]);

    await waitFor(() =>
      expect(input).toHaveValue("OCR these files:\n/tmp/scan one.pdf\n/tmp/page.png"),
    );
    expect(checkMock).toHaveBeenCalledWith({
      paths: ["/tmp/scan one.pdf", "/tmp/page.png"],
      cwd: "/work/clone",
    });
    expect(screen.queryByText(t("commands.inputPathUnreachable"))).not.toBeInTheDocument();
  });

  it("holds an unreachable dropped file until the user hands over a copy", async () => {
    checkMock.mockResolvedValue({
      paths: described([
        ["/Users/me/Desktop/shot.png", "file", false, { scope: "document", path: "Desktop" }],
        ["/Users/me/gone.txt", "missing", false],
      ]),
    });
    copyMock.mockResolvedValue({
      path: "/Users/me/Documents/GuildBotics/tmp/s1/ab12-shot.png",
      guest_path: "/Users/me/Documents/GuildBotics/tmp/s1/ab12-shot.png",
    });
    renderInput("describe");
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    await drop(input, ["/Users/me/Desktop/shot.png", "/Users/me/gone.txt"]);

    // A missing path is not a file to hand over; the unreachable one waits.
    await waitFor(() => expect(input).toHaveValue("describe\n/Users/me/gone.txt"));
    const held = await screen.findByRole("group", { name: t("commands.inputPathUnreachable") });
    expect(held).toHaveTextContent("/Users/me/Desktop/shot.png");
    expect(held).toHaveTextContent(t("commands.inputPathHint"));

    fireEvent.click(screen.getByRole("button", { name: t("commands.inputPathCopy") }));

    await waitFor(() => expect(copyMock).toHaveBeenCalledWith("/Users/me/Desktop/shot.png"));
    await waitFor(() =>
      expect(input).toHaveValue(
        "describe\n/Users/me/gone.txt\n/Users/me/Documents/GuildBotics/tmp/s1/ab12-shot.png",
      ),
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("keeps the original and opens its folder's grant when the user allows it; a folder is never copied", async () => {
    checkMock.mockResolvedValue({
      paths: described([
        [
          "/Users/me/Desktop/photos",
          "directory",
          false,
          { scope: "document", path: "Desktop/photos" },
        ],
      ]),
    });
    renderInput();
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    await drop(input, ["/Users/me/Desktop/photos"]);

    await screen.findByRole("group", { name: t("commands.inputPathUnreachable") });
    expect(screen.queryByRole("button", { name: t("commands.inputPathCopy") })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: t("commands.inputPathGrant") }));

    // The path stays: once the grant is saved the command is ready to run.
    expect(input).toHaveValue("/Users/me/Desktop/photos");
    expect(copyMock).not.toHaveBeenCalled();
    expect(openMainWindowMock).toHaveBeenCalledWith(
      "/setup?section=intelligence&advanced=intelligence&focus=grants-documents&grant=Desktop%2Fphotos",
    );
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("drops a held file when the user dismisses it, inserting nothing", async () => {
    checkMock.mockResolvedValue({
      paths: described([
        ["/Users/me/Desktop/a.png", "file", false, { scope: "document", path: "Desktop" }],
        ["/Users/me/Desktop/b.png", "file", false, { scope: "document", path: "Desktop" }],
      ]),
    });
    renderInput("look");
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    await drop(input, ["/Users/me/Desktop/a.png", "/Users/me/Desktop/b.png"]);

    const held = await screen.findByRole("group", { name: t("commands.inputPathUnreachable") });
    fireEvent.click(
      within(held).getAllByRole("button", { name: t("commands.inputPathDismiss") })[0],
    );

    expect(input).toHaveValue("look");
    expect(held).not.toHaveTextContent("/Users/me/Desktop/a.png");
    expect(held).toHaveTextContent("/Users/me/Desktop/b.png");
    expect(copyMock).not.toHaveBeenCalled();
    expect(openMainWindowMock).not.toHaveBeenCalled();
  });

  it("offers no grant for a file whose directory cannot be granted", async () => {
    checkMock.mockResolvedValue({
      paths: described([["/Users/me/notes.md", "file", false, null]]),
    });
    renderInput();

    await drop(screen.getByRole("textbox", { name: t("commands.message") }), [
      "/Users/me/notes.md",
    ]);

    await screen.findByRole("group", { name: t("commands.inputPathUnreachable") });
    expect(screen.getByRole("button", { name: t("commands.inputPathCopy") })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("commands.inputPathGrant") })).toBeNull();
  });

  it("reads the drop position as CSS pixels on macOS and as device pixels elsewhere", async () => {
    checkMock.mockImplementation(async ({ paths }) => ({
      paths: paths.map((path) => ({
        path,
        kind: "file" as const,
        reachable: true,
        guest_path: path,
        grant: null,
      })),
    }));
    Object.defineProperty(window, "devicePixelRatio", { configurable: true, value: 2 });
    Object.defineProperty(navigator, "platform", { configurable: true, value: "MacIntel" });
    renderInput();
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    // The field spans 10..210 x 10..110 in CSS pixels; wry reports (20, 20)
    // in those same units on macOS, which is inside.
    await drop(input, ["/tmp/mac.md"]);
    await waitFor(() => expect(input).toHaveValue("/tmp/mac.md"));

    // Elsewhere the same numbers are device pixels: (20, 20) is (10, 10) in
    // CSS pixels, on the field's edge, while (40, 40) lands inside it.
    Object.defineProperty(navigator, "platform", { configurable: true, value: "Win32" });
    act(() => {
      tauriDrag.handler?.({
        payload: { type: "drop", paths: ["/tmp/edge.md"], position: { x: 500, y: 500 } },
      });
    });
    act(() => {
      tauriDrag.handler?.({
        payload: { type: "drop", paths: ["/tmp/win.md"], position: { x: 40, y: 40 } },
      });
    });
    await waitFor(() => expect(input).toHaveValue("/tmp/mac.md\n/tmp/win.md"));
  });

  it("inserts dropped paths untouched and reports it when the check itself fails", async () => {
    checkMock.mockRejectedValue(new Error("backend down"));
    renderInput();
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    await drop(input, ["/tmp/a.md"]);

    await waitFor(() => expect(input).toHaveValue("/tmp/a.md"));
    expect(await screen.findByRole("alert")).toHaveTextContent("backend down");
  });

  it("uploads a pasted image and appends the returned temporary path", async () => {
    uploadMock.mockResolvedValue({
      path: "/workspace/.guildbotics/data/input.png",
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

describe("grantSettingsRoute", () => {
  it("points at the documents card for a home directory and the device card elsewhere", () => {
    expect(grantSettingsRoute({ scope: "document", path: "Desktop" })).toBe(
      "/setup?section=intelligence&advanced=intelligence&focus=grants-documents&grant=Desktop",
    );
    expect(grantSettingsRoute({ scope: "device", path: "/Volumes/data" })).toBe(
      "/setup?section=intelligence&advanced=intelligence&focus=grants-device&grant=%2FVolumes%2Fdata",
    );
  });
});

describe("appendCommandInputPaths", () => {
  it("preserves path spaces and avoids an extra blank line", () => {
    expect(appendCommandInputPaths("existing\n", ["/tmp/file name.pdf"])).toBe(
      "existing\n/tmp/file name.pdf",
    );
  });

  it("puts the environment's own spelling of a handed-over path in the field", async () => {
    checkMock.mockResolvedValue({
      paths: [
        {
          path: "C:\\Users\\me\\Desktop\\shot.png",
          kind: "file",
          reachable: false,
          guest_path: "/c/Users/me/Desktop/shot.png",
          grant: { scope: "document", path: "Desktop" },
        },
      ],
    });
    copyMock.mockResolvedValue({
      path: "C:\\Users\\me\\Documents\\GuildBotics\\tmp\\s1\\ab12-shot.png",
      guest_path: "/c/Users/me/Documents/GuildBotics/tmp/s1/ab12-shot.png",
    });
    renderInput("describe");
    const input = screen.getByRole("textbox", { name: t("commands.message") });

    await drop(input, ["C:\\Users\\me\\Desktop\\shot.png"]);
    await screen.findByText("C:\\Users\\me\\Desktop\\shot.png");
    fireEvent.click(screen.getByRole("button", { name: t("commands.inputPathCopy") }));

    await waitFor(() =>
      expect(input).toHaveValue("describe\n/c/Users/me/Documents/GuildBotics/tmp/s1/ab12-shot.png"),
    );
  });
});
