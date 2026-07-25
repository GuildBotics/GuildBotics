import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { getHotkeys, updateHotkeys } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { ShortcutsSection } from "./ShortcutsSection";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, getHotkeys: vi.fn(), updateHotkeys: vi.fn() };
});
const syncHotkeysMock = vi.fn(async () => [] as string[]);
vi.mock("../hotkeys/hotkeyRuntime", () => ({
  syncHotkeys: () => syncHotkeysMock(),
  suspendHotkeys: vi.fn(),
  resumeHotkeys: vi.fn(),
}));

const t = i18n.getFixedT("en");

function renderSection() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MantineProvider>
      <QueryClientProvider client={queryClient}>
        <ShortcutsSection />
      </QueryClientProvider>
    </MantineProvider>,
  );
  return screen.getByRole("textbox", { name: t("hotkey.label") });
}

beforeEach(() => {
  syncHotkeysMock.mockReset().mockResolvedValue([]);
  vi.mocked(getHotkeys).mockReset().mockResolvedValue({ quick_run: "", commands: {} });
  vi.mocked(updateHotkeys)
    .mockReset()
    .mockImplementation(async (body) => body);
});

describe("ShortcutsSection", () => {
  it("records the quick run hotkey and registers it", async () => {
    const user = userEvent.setup();
    const field = renderSection();

    await user.click(field);
    await user.keyboard("{Control>}{Alt>}g{/Alt}{/Control}");

    await waitFor(() =>
      expect(updateHotkeys).toHaveBeenCalledWith({ quick_run: "Control+Alt+G", commands: {} }),
    );
    await waitFor(() => expect(syncHotkeysMock).toHaveBeenCalled());
  });

  it("names the combinations the OS refused so they are not silently dead", async () => {
    syncHotkeysMock.mockResolvedValue(["Control+Alt+G"]);
    const user = userEvent.setup();
    const field = renderSection();

    await user.click(field);
    await user.keyboard("{Control>}{Alt>}g{/Alt}{/Control}");

    expect(await screen.findByText(t("hotkey.rejectedTitle"))).toBeInTheDocument();
    expect(
      screen.getByText(t("hotkey.rejectedBody", { accelerators: "Control+Alt+G" })),
    ).toBeInTheDocument();
  });

  it("keeps existing command assignments when the quick run key changes", async () => {
    vi.mocked(getHotkeys).mockResolvedValue({
      quick_run: "",
      commands: { greet: "Control+Alt+1" },
    });
    const user = userEvent.setup();
    const field = renderSection();
    await waitFor(() => expect(getHotkeys).toHaveBeenCalled());

    await user.click(field);
    await user.keyboard("{Control>}{Alt>}g{/Alt}{/Control}");

    await waitFor(() =>
      expect(updateHotkeys).toHaveBeenCalledWith({
        quick_run: "Control+Alt+G",
        commands: { greet: "Control+Alt+1" },
      }),
    );
  });
});
