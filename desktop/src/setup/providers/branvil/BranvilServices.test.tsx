import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  providerOperation,
  type ProjectServices,
  type ServiceSelection,
} from "../../../api/client";
import i18n from "../../../i18n";
import "../../../i18n";
import { TestMantineProvider } from "../../../test/TestMantineProvider";
import { createWorkspace, getGitConfig, listWorkspaces } from "./api";
import { branvilCodeHosting, branvilTickets } from "./BranvilServices";

vi.mock("../../../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../api/client")>();
  return { ...actual, providerOperation: vi.fn() };
});
vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    createWorkspace: vi.fn(),
    getGitConfig: vi.fn(),
    listWorkspaces: vi.fn(),
  };
});

const t = i18n.getFixedT("en");
const GIT_CONFIG = '[credential "http://127.0.0.1:8866"]\n\thelper = "!guildbotics git-credential"';

/** The fields of both kinds, as the integrations section holds them. */
function Services({ initial }: { initial: ProjectServices }) {
  const [services, setServices] = useState(initial);
  const field = (kind: "code_hosting_service" | "ticket_service") => {
    const value = services[kind];
    const ui = kind === "code_hosting_service" ? branvilCodeHosting : branvilTickets;
    return value ? (
      <ui.Fields
        value={value}
        onChange={(next: ServiceSelection) => setServices({ ...services, [kind]: next })}
        errors={{}}
        services={services}
      />
    ) : null;
  };
  return (
    <>
      {field("code_hosting_service")}
      {field("ticket_service")}
    </>
  );
}

function renderServices(initial: ProjectServices) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <Services initial={initial} />
      </TestMantineProvider>
    </QueryClientProvider>,
  );
}

beforeEach(async () => {
  await i18n.changeLanguage("en");
  vi.mocked(getGitConfig).mockResolvedValue({ git_config: GIT_CONFIG });
  vi.mocked(listWorkspaces).mockResolvedValue({ workspaces: ["acme", "other"] });
  vi.mocked(createWorkspace).mockResolvedValue({ workspaces: ["acme", "other", "beta"] });
});

describe("Branvil services", () => {
  it("shows the owner's git configuration once, with the first kind", async () => {
    const branvil = { name: "branvil", workspace: "acme" };
    renderServices({ code_hosting_service: branvil, ticket_service: branvil });

    const config = await screen.findAllByLabelText(t("setup.branvil.git.title"));
    expect(config).toHaveLength(1);
    expect(config[0]).toHaveTextContent('helper = "!guildbotics git-credential"');
    expect(screen.getByText(t("setup.branvil.git.hint"))).toBeVisible();
    expect(screen.getByRole("button", { name: t("setup.branvil.git.copy") })).toBeVisible();
  });

  it("asks the backend for the git configuration with the provider's operation", async () => {
    const api = await vi.importActual<typeof import("./api")>("./api");
    vi.mocked(providerOperation).mockResolvedValue({ git_config: GIT_CONFIG });

    await expect(api.getGitConfig()).resolves.toEqual({ git_config: GIT_CONFIG });
    expect(providerOperation).toHaveBeenCalledWith("branvil", "git_config", {});
  });

  it("lists and creates workspaces without connecting first", async () => {
    const user = userEvent.setup();
    renderServices({ ticket_service: { name: "branvil", workspace: "" } });

    expect(screen.getByText(t("setup.branvil.workspaceHint"))).toBeVisible();
    await user.click(screen.getByRole("combobox", { name: t("setup.branvil.workspace") }));
    expect(await screen.findByRole("option", { name: "other" })).toBeInTheDocument();
    await user.keyboard("{Escape}");

    await user.type(screen.getByLabelText(t("setup.branvil.newWorkspace")), " beta ");
    await user.click(screen.getByRole("button", { name: t("setup.branvil.createWorkspace") }));

    await waitFor(() => expect(createWorkspace).toHaveBeenCalledWith("beta"));
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: t("setup.branvil.workspace") })).toHaveValue(
        "beta",
      ),
    );
  });
});
