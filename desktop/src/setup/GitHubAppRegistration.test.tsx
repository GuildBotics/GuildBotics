import { MantineProvider } from "@mantine/core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiRequestError,
  getGitHubAppRegistration,
  startGitHubAppRegistration,
  type GitHubAppRegistrationStatus,
} from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { openExternal } from "../openExternal";
import { GitHubAppRegistrationPanel } from "./GitHubAppRegistration";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    startGitHubAppRegistration: vi.fn(),
    getGitHubAppRegistration: vi.fn(),
  };
});
vi.mock("../openExternal", () => ({ openExternal: vi.fn(async () => {}) }));

const t = i18n.getFixedT("en");

const pendingRegistration: GitHubAppRegistrationStatus = {
  state: "state-1",
  status: "pending",
  app_name: "my-bot",
  start_url: "http://127.0.0.1:8765/github-app/registrations/state-1/start",
  slug: "",
  app_id: null,
  html_url: "",
  github_username: "",
  git_email: "",
  private_key_path: "",
  installation_id: null,
  installation_page_url: "",
  installation_check_error: "",
};

const convertedRegistration: GitHubAppRegistrationStatus = {
  ...pendingRegistration,
  start_url: "",
  status: "converted",
  slug: "my-bot",
  app_id: 1978826,
  html_url: "https://github.com/apps/my-bot",
  github_username: "my-bot[bot]",
  git_email: "233270845+my-bot[bot]@users.noreply.github.com",
  private_key_path: "/data/github-apps/my-bot.private-key.pem",
  installation_page_url: "https://github.com/apps/my-bot/installations/new",
};

const installedRegistration: GitHubAppRegistrationStatus = {
  ...convertedRegistration,
  status: "installed",
  installation_id: 86632391,
};

function renderPanel(onApplied = vi.fn(), defaultOrganization = "") {
  render(
    <MantineProvider>
      <GitHubAppRegistrationPanel
        defaultAppName="my-bot"
        defaultOrganization={defaultOrganization}
        onApplied={onApplied}
        pollIntervalMs={20}
      />
    </MantineProvider>,
  );
  return onApplied;
}

beforeEach(() => {
  vi.mocked(startGitHubAppRegistration).mockReset().mockResolvedValue(pendingRegistration);
  vi.mocked(getGitHubAppRegistration).mockReset().mockResolvedValue(pendingRegistration);
  vi.mocked(openExternal).mockClear();
});

describe("GitHubAppRegistrationPanel", () => {
  it("starts a registration and opens the browser at the start URL", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.type(
      screen.getByRole("textbox", {
        name: t("setup.members.githubAppRegistration.organization"),
      }),
      "acme",
    );
    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );

    await waitFor(() =>
      expect(startGitHubAppRegistration).toHaveBeenCalledWith({
        app_name: "my-bot",
        organization: "acme",
      }),
    );
    expect(openExternal).toHaveBeenCalledWith(pendingRegistration.start_url);
    expect(screen.getByText(t("setup.members.githubAppRegistration.pending"))).toBeInTheDocument();
  });

  it("applies converted credentials and then the detected installation ID", async () => {
    const user = userEvent.setup();
    const onApplied = renderPanel();

    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );
    await waitFor(() => expect(startGitHubAppRegistration).toHaveBeenCalled());

    vi.mocked(getGitHubAppRegistration).mockResolvedValue(convertedRegistration);
    await waitFor(() =>
      expect(onApplied).toHaveBeenCalledWith({
        githubUsername: "my-bot[bot]",
        gitEmail: "233270845+my-bot[bot]@users.noreply.github.com",
        appId: "1978826",
        privateKeyPath: "/data/github-apps/my-bot.private-key.pem",
      }),
    );
    expect(
      screen.getByText(t("setup.members.githubAppRegistration.converted", { slug: "my-bot" })),
    ).toBeInTheDocument();

    vi.mocked(getGitHubAppRegistration).mockResolvedValue(installedRegistration);
    await waitFor(() =>
      expect(onApplied).toHaveBeenCalledWith(
        expect.objectContaining({ installationId: "86632391" }),
      ),
    );
    expect(
      screen.getByText(t("setup.members.githubAppRegistration.installed")),
    ).toBeInTheDocument();
  });

  it("prefills the organization from the project and respects clearing it", async () => {
    const user = userEvent.setup();
    renderPanel(vi.fn(), "acme");

    const organizationField = screen.getByRole("textbox", {
      name: t("setup.members.githubAppRegistration.organization"),
    });
    expect(organizationField).toHaveValue("acme");

    // Clearing the field must mean "personal account", not fall back to the
    // project default.
    await user.clear(organizationField);
    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );

    await waitFor(() =>
      expect(startGitHubAppRegistration).toHaveBeenCalledWith({
        app_name: "my-bot",
        organization: "",
      }),
    );
  });

  it("shows the backend error when starting fails", async () => {
    vi.mocked(startGitHubAppRegistration).mockRejectedValue(
      new Error("GitHub App name must be 1-34 characters."),
    );
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );

    expect(await screen.findByText("GitHub App name must be 1-34 characters.")).toBeInTheDocument();
    expect(openExternal).not.toHaveBeenCalled();
  });

  it("translates known registration error codes", async () => {
    vi.mocked(startGitHubAppRegistration).mockRejectedValue(
      new ApiRequestError({
        code: "invalid_github_app_name",
        message: "GitHub App name must be 1-34 characters.",
        context: {},
      }),
    );
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );

    expect(
      await screen.findByText(t("setup.members.githubAppRegistration.errors.invalidAppName")),
    ).toBeInTheDocument();
  });

  it("shows the expiry message when the registration disappears while polling", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );
    await waitFor(() => expect(startGitHubAppRegistration).toHaveBeenCalled());

    vi.mocked(getGitHubAppRegistration).mockRejectedValue(
      new ApiRequestError({
        code: "github_app_registration_not_found",
        message: "GitHub App registration was not found or has expired.",
        context: {},
      }),
    );

    expect(
      await screen.findByText(t("setup.members.githubAppRegistration.errors.expired")),
    ).toBeInTheDocument();
  });

  it("surfaces installation check failures reported by the backend", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );
    await waitFor(() => expect(startGitHubAppRegistration).toHaveBeenCalled());

    vi.mocked(getGitHubAppRegistration).mockResolvedValue({
      ...convertedRegistration,
      installation_check_error: "boom",
    });

    expect(
      await screen.findByText(
        t("setup.members.githubAppRegistration.installCheckError", { message: "boom" }),
      ),
    ).toBeInTheDocument();
  });
});
