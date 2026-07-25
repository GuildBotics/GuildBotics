import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiRequestError, getHotkeys, updateHotkeys } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { CommandHotkeyField } from "./CommandHotkeyField";

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

function renderField(command: string | null = "greet") {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <MantineProvider>
      <QueryClientProvider client={queryClient}>
        <CommandHotkeyField command={command} />
      </QueryClientProvider>
    </MantineProvider>,
  );
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

describe("CommandHotkeyField", () => {
  it("shows nothing until a command is selected", () => {
    renderField(null);

    expect(
      screen.queryByRole("textbox", { name: t("hotkey.commandLabel") }),
    ).not.toBeInTheDocument();
  });

  it("shows the combination already assigned to the command", async () => {
    renderField();

    // jsdom is not macOS, so the field spells the modifiers out; the macOS
    // symbol notation is covered by the HotkeyInput tests.
    const field = await screen.findByRole("textbox", { name: t("hotkey.commandLabel") });
    await waitFor(() => expect(field).toHaveValue("Control+Alt+1"));
  });

  it("saves a recorded combination on its own and re-registers it", async () => {
    const user = userEvent.setup();
    renderField();
    const field = await screen.findByRole("textbox", { name: t("hotkey.commandLabel") });

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
    renderField();

    await user.click(await screen.findByRole("textbox", { name: t("hotkey.commandLabel") }));
    expect(suspendMock).toHaveBeenCalled();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(resumeMock).toHaveBeenCalled());
  });

  it("drops the assignment when the field is cleared", async () => {
    const user = userEvent.setup();
    renderField();

    await user.click(await screen.findByRole("textbox", { name: t("hotkey.commandLabel") }));
    await user.keyboard("{Backspace}");

    await waitFor(() =>
      expect(updateHotkeys).toHaveBeenCalledWith({
        quick_run: "Control+Alt+G",
        commands: {},
      }),
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
    renderField();

    await user.click(await screen.findByRole("textbox", { name: t("hotkey.commandLabel") }));
    await user.keyboard("{Control>}{Alt>}2{/Alt}{/Control}");

    expect(await screen.findByText(t("hotkey.errors.hotkey_conflict"))).toBeInTheDocument();
  });
});
