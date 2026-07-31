import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError, getHotkeys, updateHotkeys } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { CommandHotkeyChip } from "./CommandHotkeyChip";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getHotkeys: vi.fn(), updateHotkeys: vi.fn() };
});
const syncHotkeysMock = vi.fn(async () => [] as string[]);
const suspendMock = vi.fn();
const resumeMock = vi.fn();
vi.mock("../hotkeys/hotkeyRuntime", () => ({
  syncHotkeys: (...args: unknown[]) => syncHotkeysMock(...(args as [])),
  suspendHotkeys: () => suspendMock(),
  resumeHotkeys: () => resumeMock(),
}));

const t = i18n.getFixedT("en");

function renderChip(command: string | null = "greet") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider>
      <QueryClientProvider client={queryClient}>
        <CommandHotkeyChip command={command} />
      </QueryClientProvider>
    </MantineProvider>,
  );
}

/** Open the chip's popover and hand back the recording input inside it. */
async function openRecorder(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: t("hotkey.commandLabel") }));
  return await screen.findByRole("textbox", { name: t("hotkey.commandLabel") });
}

beforeEach(() => {
  syncHotkeysMock.mockClear();
  suspendMock.mockClear();
  resumeMock.mockClear();
  vi.mocked(getHotkeys)
    .mockReset()
    .mockResolvedValue({ quick_run: "Control+Alt+G", commands: { greet: "Control+Alt+1" } });
  vi.mocked(updateHotkeys)
    .mockReset()
    .mockImplementation(async (body) => body);
});

describe("CommandHotkeyChip", () => {
  it("shows nothing until a command is selected", () => {
    renderChip(null);

    expect(
      screen.queryByRole("button", { name: t("hotkey.commandLabel") }),
    ).not.toBeInTheDocument();
  });

  it("shows the assigned combination on the chip without opening it", async () => {
    renderChip();

    // jsdom is not macOS, so the chip spells the modifiers out; the macOS
    // symbol notation is covered by the HotkeyInput tests.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: t("hotkey.commandLabel") })).toHaveTextContent(
        "Control+Alt+1",
      ),
    );
    expect(
      screen.queryByRole("textbox", { name: t("hotkey.commandLabel") }),
    ).not.toBeInTheDocument();
  });

  it("reads as unset when the command has no combination", async () => {
    vi.mocked(getHotkeys).mockResolvedValue({ quick_run: "Control+Alt+G", commands: {} });
    renderChip();

    await waitFor(() =>
      expect(screen.getByRole("button", { name: t("hotkey.commandLabel") })).toHaveTextContent(
        t("hotkey.unset"),
      ),
    );
  });

  it("explains the per-command scope inside the popover", async () => {
    const user = userEvent.setup();
    renderChip();

    await openRecorder(user);

    expect(screen.getByText(t("hotkey.commandDescription"))).toBeInTheDocument();
  });

  it("saves a recorded combination on its own and re-registers it", async () => {
    const user = userEvent.setup();
    renderChip();
    const field = await openRecorder(user);

    await user.click(field);
    await user.keyboard("{Control>}{Alt>}2{/Alt}{/Control}");

    await waitFor(() =>
      expect(updateHotkeys).toHaveBeenCalledWith({
        quick_run: "Control+Alt+G",
        commands: { greet: "Control+Alt+2" },
      }),
    );
    await waitFor(() => expect(syncHotkeysMock).toHaveBeenCalled());
  });

  it("releases the registered combinations while recording", async () => {
    const user = userEvent.setup();
    renderChip();
    const field = await openRecorder(user);

    await user.click(field);
    expect(suspendMock).toHaveBeenCalled();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(resumeMock).toHaveBeenCalled());
  });

  it("drops the assignment when the field is cleared", async () => {
    const user = userEvent.setup();
    renderChip();
    const field = await openRecorder(user);

    await user.click(field);
    await user.keyboard("{Backspace}");

    await waitFor(() =>
      expect(updateHotkeys).toHaveBeenCalledWith({
        quick_run: "Control+Alt+G",
        commands: {},
      }),
    );
  });

  it("offers a clear button as soon as the popover opens", async () => {
    const user = userEvent.setup();
    renderChip();

    // Opening must not put the field straight into recording mode: that hides
    // the clear button and leaves no way to unassign with the mouse.
    await openRecorder(user);
    await user.click(await screen.findByRole("button", { name: t("hotkey.clear") }));

    await waitFor(() =>
      expect(updateHotkeys).toHaveBeenCalledWith({
        quick_run: "Control+Alt+G",
        commands: {},
      }),
    );
    await waitFor(() =>
      expect(screen.getByRole("button", { name: t("hotkey.commandLabel") })).toHaveTextContent(
        t("hotkey.unset"),
      ),
    );
  });

  it("explains a rejected combination instead of failing silently", async () => {
    vi.mocked(updateHotkeys).mockRejectedValue(
      new ApiRequestError({
        code: "hotkey_conflict",
        message: "already assigned",
        context: {},
      }),
    );
    const user = userEvent.setup();
    renderChip();
    const field = await openRecorder(user);

    await user.click(field);
    await user.keyboard("{Control>}{Alt>}2{/Alt}{/Control}");

    expect(await screen.findByText(t("hotkey.errors.hotkey_conflict"))).toBeInTheDocument();
  });
});
