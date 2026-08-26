import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  fetchWorkspaceSecrets,
  getWorkspaceSecrets,
  sendWorkspaceSecrets,
  type WorkspaceSecrets,
  type WorkspaceSecretState,
} from "../api/client";
import i18n from "../i18n";
import { SecretsCard } from "./SecretsCard";

const t = i18n.getFixedT("en");

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    ...actual,
    getWorkspaceSecrets: vi.fn(),
    fetchWorkspaceSecrets: vi.fn(),
    sendWorkspaceSecrets: vi.fn(),
  };
});

function state(overrides: Partial<WorkspaceSecretState> = {}): WorkspaceSecretState {
  return {
    key: "A_TOKEN",
    status: "ready",
    shared_generation: 1,
    local_generation: 1,
    hub_generation: 1,
    updated_at: "2026-08-20T00:00:00Z",
    can_send: true,
    can_fetch: false,
    ...overrides,
  };
}

function secrets(overrides: Partial<WorkspaceSecrets> = {}): WorkspaceSecrets {
  return {
    enabled: true,
    hub_reachable: true,
    hub_error_code: "",
    secret_store: { available: true, locked: false },
    hub_secret_store: { available: true, locked: false },
    keys: [state()],
    sendable_keys: [],
    fetchable_keys: [],
    missing_count: 0,
    outdated_count: 0,
    pending_count: 0,
    attention_count: 0,
    ...overrides,
  };
}

function renderCard() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MantineProvider>
        <MemoryRouter>
          <SecretsCard />
        </MemoryRouter>
      </MantineProvider>
    </QueryClientProvider>,
  );
  return client;
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(getWorkspaceSecrets).mockResolvedValue(secrets());
});

describe("SecretsCard", () => {
  it("stays out of the way when the workspace has no hub", async () => {
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(secrets({ enabled: false }));

    renderCard();

    await expect(screen.findByText(t("sync.secrets.title"))).rejects.toThrow();
  });

  it("names each key's state without showing anything else about it", async () => {
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({
        keys: [
          state({ key: "A_TOKEN" }),
          state({ key: "B_TOKEN", status: "missing", local_generation: null }),
        ],
        missing_count: 1,
        attention_count: 1,
      }),
    );

    renderCard();

    expect(await screen.findByText("A_TOKEN")).toBeInTheDocument();
    expect(screen.getByText(t("sync.secrets.state.ready"))).toBeInTheDocument();
    expect(screen.getByText(t("sync.secrets.state.missing"))).toBeInTheDocument();
  });

  it("lets the backend decide what a bulk fetch takes, rather than naming keys", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({
        keys: [
          state({ key: "A_TOKEN", status: "missing", local_generation: null, can_fetch: true }),
          state({
            key: "B_TOKEN",
            status: "outdated",
            local_generation: 1,
            hub_generation: 2,
            can_fetch: true,
          }),
          // Changed on two machines: the row offers a fetch, but the backend
          // leaves it out of the bulk action because taking it drops what was
          // typed here.
          state({ key: "C_TOKEN", status: "conflict", can_fetch: true }),
        ],
        fetchable_keys: ["A_TOKEN", "B_TOKEN"],
        missing_count: 1,
        outdated_count: 1,
        attention_count: 3,
      }),
    );
    vi.mocked(fetchWorkspaceSecrets).mockResolvedValue({
      results: [
        { key: "A_TOKEN", status: "fetched", generation: 1 },
        { key: "B_TOKEN", status: "fetched", generation: 2 },
      ],
      secrets: secrets(),
    });
    renderCard();

    await user.click(
      await screen.findByRole("button", { name: t("sync.secrets.fetchAll", { count: 2 }) }),
    );

    // Naming the keys would act on a list as old as the last poll: a key
    // changed on another machine since then would be swept up.
    expect(fetchWorkspaceSecrets).toHaveBeenCalledWith({ keys: [] });
  });

  it("offers nothing to fetch when this machine holds everything", async () => {
    renderCard();

    expect(
      await screen.findByRole("button", { name: t("sync.secrets.fetchAll", { count: 0 }) }),
    ).toBeDisabled();
  });

  it("lets the backend decide what a bulk send hands over", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({
        keys: [
          state({
            key: "A_TOKEN",
            status: "pending_send",
            shared_generation: 0,
            hub_generation: null,
          }),
          state({ key: "B_TOKEN" }),
        ],
        sendable_keys: ["A_TOKEN"],
        pending_count: 1,
        attention_count: 1,
      }),
    );
    vi.mocked(sendWorkspaceSecrets).mockResolvedValue({
      results: [{ key: "A_TOKEN", status: "sent", generation: 1 }],
      secrets: secrets(),
    });
    renderCard();

    await user.click(
      await screen.findByRole("button", { name: t("sync.secrets.sendAll", { count: 1 }) }),
    );

    expect(sendWorkspaceSecrets).toHaveBeenCalledWith({ keys: [] });
  });

  it("names the key when the action is a row rather than a bulk one", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({
        keys: [state({ key: "A_TOKEN", can_send: true, can_fetch: false })],
      }),
    );
    vi.mocked(sendWorkspaceSecrets).mockResolvedValue({
      results: [{ key: "A_TOKEN", status: "sent", generation: 2 }],
      secrets: secrets(),
    });
    renderCard();

    await user.click(await screen.findByRole("button", { name: t("sync.secrets.send") }));

    expect(sendWorkspaceSecrets).toHaveBeenCalledWith({ keys: ["A_TOKEN"] });
  });

  it("marks every other screen's snapshot stale once a transfer moved a value", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({ sendable_keys: ["A_TOKEN"], pending_count: 1, attention_count: 1 }),
    );
    const refreshed = secrets();
    vi.mocked(sendWorkspaceSecrets).mockResolvedValue({
      results: [{ key: "A_TOKEN", status: "sent", generation: 1 }],
      secrets: refreshed,
    });
    const client = renderCard();
    // A snapshot another section read before the transfer, e.g. "is this
    // provider's API key stored?" on the intelligence section.
    client.setQueryData(["project-config"], { provider_api_keys: { openai: false } });

    await user.click(
      await screen.findByRole("button", { name: t("sync.secrets.sendAll", { count: 1 }) }),
    );

    await waitFor(() => expect(client.getQueryState(["project-config"])?.isInvalidated).toBe(true));
    // The secret states themselves were just delivered by the transfer's own
    // response, so they are the one thing not marked stale.
    expect(client.getQueryState(["workspace-secrets"])?.isInvalidated).toBe(false);
    expect(client.getQueryData(["workspace-secrets"])).toEqual(refreshed);
  });

  it("says which keys a transfer could not move, and why", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({ sendable_keys: ["A_TOKEN"], pending_count: 1, attention_count: 1 }),
    );
    vi.mocked(sendWorkspaceSecrets).mockResolvedValue({
      results: [{ key: "A_TOKEN", status: "conflict", generation: null }],
      secrets: secrets(),
    });
    renderCard();

    await user.click(
      await screen.findByRole("button", { name: t("sync.secrets.sendAll", { count: 1 }) }),
    );

    expect(
      await screen.findByText(t("sync.secrets.refused.title", { count: 1 })),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        t("sync.secrets.refused.entry", {
          key: "A_TOKEN",
          reason: t("sync.secrets.result.conflict"),
        }),
      ),
    ).toBeInTheDocument();
  });

  it("says nothing about keys that moved", async () => {
    const user = userEvent.setup();
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({ sendable_keys: ["A_TOKEN"], pending_count: 1, attention_count: 1 }),
    );
    vi.mocked(sendWorkspaceSecrets).mockResolvedValue({
      results: [{ key: "A_TOKEN", status: "sent", generation: 1 }],
      secrets: secrets(),
    });
    renderCard();

    await user.click(
      await screen.findByRole("button", { name: t("sync.secrets.sendAll", { count: 1 }) }),
    );

    await waitFor(() => expect(sendWorkspaceSecrets).toHaveBeenCalled());
    expect(screen.queryByText(t("sync.secrets.refused.title", { count: 1 }))).toBe(null);
  });

  it("says which machine's secret store is locked", async () => {
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({ hub_secret_store: { available: false, locked: true } }),
    );

    renderCard();

    expect(await screen.findByText(t("sync.secrets.alert.hub_locked.title"))).toBeInTheDocument();
  });

  it("still lists what this machine knows when the hub did not answer", async () => {
    vi.mocked(getWorkspaceSecrets).mockResolvedValue(
      secrets({ hub_reachable: false, hub_secret_store: null, hub_error_code: "hub_unreachable" }),
    );

    renderCard();

    expect(
      await screen.findByText(t("sync.secrets.alert.hub_unreachable.title")),
    ).toBeInTheDocument();
    expect(screen.getByText("A_TOKEN")).toBeInTheDocument();
  });
});
