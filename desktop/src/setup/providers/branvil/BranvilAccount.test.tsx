import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { providerOperation, type ProjectServices } from "../../../api/client";
import i18n from "../../../i18n";
import "../../../i18n";
import { openExternal } from "../../../openExternal";
import { TestMantineProvider } from "../../../test/TestMantineProvider";
import type { MemberContext } from "../types";
import { BRANVIL_BASE_URL, type Connection } from "./api";
import { branvilAccount, type BranvilForm } from "./BranvilAccount";
import { branvilCodeHosting, branvilTickets } from "./BranvilServices";

vi.mock("../../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api/client")>();
  return {
    ...actual,
    connected: () => ({ base: "http://127.0.0.1:8765" }),
    providerOperation: vi.fn(),
  };
});
vi.mock("../../../openExternal", () => ({ openExternal: vi.fn(async () => {}) }));

const t = i18n.getFixedT("en");
const SERVICES: ProjectServices = {
  code_hosting_service: { name: "branvil", base_url: BRANVIL_BASE_URL, workspace: "acme" },
  ticket_service: { name: "branvil", base_url: BRANVIL_BASE_URL, workspace: "acme" },
};

/** A Branvil the operations answer for: connected unless told otherwise, its
 * consent answered by the second poll. */
function branvil({ connected = true } = {}) {
  let polls = 0;
  const workspaces = ["acme", "other"];
  const connection = (owner: string): Connection => ({
    status: owner ? "connected" : "disconnected",
    owner,
    message: owner ? "" : "Not connected.",
  });
  vi.mocked(providerOperation).mockImplementation(async (_provider, operation, body) => {
    const request = body as Record<string, string>;
    switch (operation) {
      case "connection":
        if (request.state) {
          polls += 1;
          if (polls < 2) return { status: "pending", owner: "", message: "" };
          connected = true;
        }
        return connection(connected ? "otota" : "");
      case "connect":
        return { state: "consent-1", url: `${BRANVIL_BASE_URL}/-/authorize?state=consent-1` };
      case "entry":
        return { url: `${BRANVIL_BASE_URL}/-/enter#code` };
      case "workspaces":
        return { workspaces };
      case "workspaces/create":
        workspaces.push(request.name);
        return { workspaces };
      case "agents":
        return { agents: ["aiko", "beta"] };
    }
    throw new Error(`unexpected ${operation}`);
  });
}

const memberContext = (human: boolean): MemberContext => ({
  human,
  personId: "my-bot",
  savedPersonId: "",
  memberKey: "my-bot",
  gitEmail: "",
  setGitEmail: () => undefined,
  secretEnvKeys: {},
  chatChannels: [],
  chatChannelsField: null,
  services: SERVICES,
});

function renderWithProviders(node: React.ReactNode) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TestMantineProvider>{node}</TestMantineProvider>
    </QueryClientProvider>,
  );
}

function Account({ start, human }: { start: BranvilForm; human: boolean }) {
  const [form, setForm] = useState(start);
  return (
    <>
      <branvilAccount.Panel
        form={form}
        onChange={(update) => setForm(update)}
        context={memberContext(human)}
        errors={{}}
      />
      <output>{JSON.stringify(branvilAccount.request(form, human))}</output>
    </>
  );
}

const sent = () => JSON.parse(screen.getByRole("status").textContent ?? "");

beforeEach(() => {
  vi.mocked(providerOperation).mockReset();
  vi.mocked(openExternal).mockClear();
});

describe("BranvilAccount panel", () => {
  it("creates a new agent named after the member, or takes one chosen", async () => {
    branvil();
    renderWithProviders(<Account start={branvilAccount.initial()} human={false} />);

    expect(screen.getByLabelText(t("setup.branvil.member.new", { name: "my-bot" }))).toBeChecked();
    expect(sent()).toEqual({ username: "", reissue: false });

    await userEvent.click(screen.getByLabelText(t("setup.branvil.member.existing")));
    await userEvent.click(screen.getByRole("combobox", { name: t("setup.branvil.member.choose") }));
    await userEvent.click(await screen.findByRole("option", { name: "beta" }));

    expect(sent()).toEqual({ username: "beta", reissue: false });
    // No token is ever typed: GuildBotics issues it.
    expect(screen.queryByPlaceholderText("bvt_…")).not.toBeInTheDocument();
  });

  it("shows a linked agent's token and reissues it on request", async () => {
    branvil();
    const start = branvilAccount.fromView(
      { username: "aiko", has_token: true },
      branvilAccount.initial(),
      false,
    );
    renderWithProviders(<Account start={start} human={false} />);

    expect(screen.getByText(t("setup.branvil.member.stored", { name: "aiko" }))).toBeVisible();
    expect(screen.getByText(t("setup.branvil.member.tokenSet"))).toBeVisible();
    await userEvent.click(screen.getByLabelText(t("setup.branvil.member.reissue")));

    expect(sent()).toEqual({ username: "aiko", reissue: true });
  });

  it("names a human after Branvil's owner", async () => {
    branvil();
    renderWithProviders(<Account start={branvilAccount.initial()} human />);

    expect(screen.getByText(t("setup.branvil.member.ownerPending"))).toBeVisible();
    expect(sent()).toEqual({ username: "" });
    expect(
      await screen.findByText(t("setup.branvil.connection.connected", { owner: "otota" })),
    ).toBeVisible();
  });
});

describe("Branvil connection", () => {
  it("connects on the owner's consent in the browser, then opens Branvil", async () => {
    branvil({ connected: false });
    renderWithProviders(<Account start={branvilAccount.initial()} human />);

    await userEvent.click(
      await screen.findByRole("button", { name: t("setup.branvil.connection.connect") }),
    );

    expect(providerOperation).toHaveBeenCalledWith("branvil", "connect", {
      base_url: BRANVIL_BASE_URL,
      callback_base_url: "http://127.0.0.1:8765",
    });
    expect(openExternal).toHaveBeenCalledWith(`${BRANVIL_BASE_URL}/-/authorize?state=consent-1`);
    expect(screen.getByText(t("setup.branvil.connection.pending"))).toBeVisible();
    expect(
      await screen.findByText(
        t("setup.branvil.connection.connected", { owner: "otota" }),
        {},
        { timeout: 5000 },
      ),
    ).toBeVisible();
    expect(providerOperation).toHaveBeenCalledWith("branvil", "connection", {
      base_url: BRANVIL_BASE_URL,
      state: "consent-1",
    });

    await userEvent.click(screen.getByRole("button", { name: t("setup.branvil.connection.open") }));

    await waitFor(() =>
      expect(openExternal).toHaveBeenLastCalledWith(`${BRANVIL_BASE_URL}/-/enter#code`),
    );
  });

  it("says why it is not connected", async () => {
    branvil({ connected: false });
    renderWithProviders(<Account start={branvilAccount.initial()} human />);

    expect(await screen.findByText("Not connected.")).toBeVisible();
  });
});

function Services() {
  const [services, setServices] = useState<ProjectServices>(SERVICES);
  return (
    <>
      {(
        [
          ["code_hosting_service", branvilCodeHosting],
          ["ticket_service", branvilTickets],
        ] as const
      ).map(([kind, ui]) => (
        <ui.Fields
          key={kind}
          value={services[kind]!}
          onChange={(value) => setServices((current) => ({ ...current, [kind]: value }))}
          errors={ui.errors(services[kind]!, t)}
          services={services}
        />
      ))}
      <output>{JSON.stringify(services)}</output>
    </>
  );
}

describe("Branvil service fields", () => {
  it("name the Branvil once, and each kind's workspace chosen or created there", async () => {
    branvil();
    renderWithProviders(<Services />);

    // The tickets follow the code host's Branvil.
    expect(screen.getAllByLabelText(t("setup.branvil.baseUrl"))).toHaveLength(1);
    expect(
      await screen.findAllByText(t("setup.branvil.connection.connected", { owner: "otota" })),
    ).toHaveLength(1);
    const [, tickets] = screen.getAllByLabelText(t("setup.branvil.newWorkspace"));
    await userEvent.type(tickets, "beta");
    const [, create] = screen.getAllByRole("button", { name: t("setup.branvil.createWorkspace") });
    await userEvent.click(create);

    await waitFor(() => expect(sent().ticket_service.workspace).toBe("beta"));
    expect(providerOperation).toHaveBeenCalledWith("branvil", "workspaces/create", {
      base_url: BRANVIL_BASE_URL,
      name: "beta",
    });
    expect(sent().code_hosting_service.workspace).toBe("acme");
  });

  it("refuse what names no Branvil or workspace", () => {
    const value = { name: "branvil", base_url: "http://hub/x", workspace: "" };

    expect(branvilTickets.errors(value, t)).toEqual({
      base_url: t("setup.branvil.baseUrlInvalid"),
      workspace: t("setup.branvil.workspaceRequired"),
    });
  });
});
