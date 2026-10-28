import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getProjectConfig, providerOperation, type ProjectServices } from "../../../api/client";
import i18n from "../../../i18n";
import { openExternal } from "../../../openExternal";
import { TestMantineProvider } from "../../../test/TestMantineProvider";
import { BranvilNavItem } from "./BranvilNavItem";

vi.mock("../../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api/client")>();
  return { ...actual, getProjectConfig: vi.fn(), providerOperation: vi.fn() };
});
vi.mock("../../../openExternal", () => ({ openExternal: vi.fn(async () => {}) }));

const queryClients: QueryClient[] = [];
const t = (key: string) => i18n.t(`setup.branvil.nav.${key}`);

function savedServices(services: ProjectServices) {
  vi.mocked(getProjectConfig).mockResolvedValue({
    config_dir: "/workspace/.guildbotics/config",
    revisions: {},
    language: "en",
    description: "Demo",
    llm_api_type: "openai",
    cli_agent: "codex",
    services,
    provider_api_keys: {},
  });
}

function renderEntry() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  queryClients.push(client);
  render(
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <BranvilNavItem />
      </TestMantineProvider>
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(async () => {
  vi.clearAllMocks();
  await i18n.changeLanguage("en");
  savedServices({ code_hosting_service: { name: "branvil" } });
  vi.mocked(providerOperation).mockResolvedValue({ url: "https://branvil.example/entry" });
});

afterEach(async () => {
  queryClients.splice(0).forEach((client) => client.clear());
  await i18n.changeLanguage("en");
});

describe("Branvil sidebar entry", () => {
  it.each([
    ["branvil", "github", true],
    ["github", "branvil", true],
    ["branvil", "branvil", true],
    ["github", "github", false],
    [undefined, undefined, false],
  ])("uses the saved code host %s and ticket service %s", async (code, tickets, visible) => {
    savedServices({
      ...(code ? { code_hosting_service: { name: code } } : {}),
      ...(tickets ? { ticket_service: { name: tickets } } : {}),
    });
    const client = renderEntry();
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(Boolean(screen.queryByRole("button", { name: t("label") }))).toBe(visible);
    expect(providerOperation).not.toHaveBeenCalled();
  });

  it("updates in place when saving invalidates the shared project config", async () => {
    savedServices({});
    const client = renderEntry();
    await waitFor(() => expect(client.isFetching()).toBe(0));
    expect(screen.queryByRole("button")).not.toBeInTheDocument();

    savedServices({ ticket_service: { name: "branvil" } });
    await act(() => client.invalidateQueries({ queryKey: ["project-config"] }));
    expect(await screen.findByRole("button", { name: t("label") })).toBeVisible();

    savedServices({ ticket_service: { name: "github" } });
    await act(() => client.invalidateQueries({ queryKey: ["project-config"] }));
    await waitFor(() => expect(screen.queryByRole("button")).not.toBeInTheDocument());
  });

  it.each(["en", "ja"])("labels the external entry in %s", async (language) => {
    await i18n.changeLanguage(language);
    renderEntry();
    const button = await screen.findByRole("button", { name: t("label") });
    expect(button).toHaveAttribute("title", t("openInBrowser"));
    expect(i18n.exists("setup.branvil.nav.label", { lng: language })).toBe(true);
    expect(i18n.exists("setup.branvil.nav.openInBrowser", { lng: language })).toBe(true);
    expect(button.querySelector("svg.lucide-external-link")).toBeInTheDocument();
  });

  it("requests a fresh entry link for each click and opens it externally", async () => {
    renderEntry();
    const button = await screen.findByRole("button", { name: t("label") });
    for (const url of ["https://branvil.example/first", "https://branvil.example/second"]) {
      vi.mocked(providerOperation).mockResolvedValueOnce({ url });
      await userEvent.click(button);
      await waitFor(() => expect(openExternal).toHaveBeenLastCalledWith(url));
      await waitFor(() => expect(button).toBeEnabled());
    }
    expect(providerOperation).toHaveBeenCalledTimes(2);
    expect(providerOperation).toHaveBeenCalledWith("branvil", "entry", {});
  });

  it.each(["Not connected to Branvil.", "Branvil is not reachable."])(
    "shows the entry refusal: %s, then allows retry",
    async (reason) => {
      vi.mocked(providerOperation).mockRejectedValueOnce(new Error(reason));
      renderEntry();
      const button = await screen.findByRole("button", { name: t("label") });
      await userEvent.click(button);
      expect(await screen.findByRole("alert")).toHaveTextContent(reason);
      expect(openExternal).not.toHaveBeenCalled();
      await userEvent.click(button);
      await waitFor(() => expect(openExternal).toHaveBeenCalled());
      expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    },
  );

  it("disables the item while requesting entry and shows browser failures", async () => {
    let refuse!: (error: Error) => void;
    vi.mocked(providerOperation).mockReturnValueOnce(
      new Promise((_resolve, reject) => {
        refuse = reject;
      }),
    );
    renderEntry();
    const button = await screen.findByRole("button", { name: t("label") });
    await userEvent.click(button);
    expect(button).toBeDisabled();
    await userEvent.click(button);
    expect(providerOperation).toHaveBeenCalledTimes(1);
    refuse(new Error("Entry refused"));
    expect(await screen.findByRole("alert")).toHaveTextContent("Entry refused");
    expect(button).toBeEnabled();

    vi.mocked(openExternal).mockRejectedValueOnce(new Error("Browser could not open"));
    await userEvent.click(button);
    await waitFor(() =>
      expect(screen.getByRole("alert")).toHaveTextContent("Browser could not open"),
    );
  });
});
