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
    <MantineProvider env="test">
      <GitHubAppRegistrationPanel
        defaultAppName="my-bot"
        defaultOrganization={defaultOrganization}
        onApplied={onApplied}
        pollIntervalMs={20}
        memberKey="edit:my-bot"
      />
    </MantineProvider>,
  );
  return onApplied;
}

function renderSwitchablePanel() {
  const props = {
    defaultOrganization: "acme",
    onApplied: vi.fn(),
    pollIntervalMs: 20,
  };
  const { rerender } = render(
    <MantineProvider env="test">
      <GitHubAppRegistrationPanel {...props} defaultAppName="my-bot" memberKey="edit:my-bot" />
    </MantineProvider>,
  );
  return () =>
    rerender(
      <MantineProvider env="test">
        <GitHubAppRegistrationPanel
          {...props}
          defaultAppName="other-bot"
          memberKey="edit:other-bot"
        />
      </MantineProvider>,
    );
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

    // GitHub App names are globally unique, so the suggested name qualifies
    // the member ID with the organization.
    await waitFor(() =>
      expect(startGitHubAppRegistration).toHaveBeenCalledWith({
        app_name: "my-bot-acme",
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
    expect(
      screen.getByRole("textbox", { name: t("setup.members.githubAppRegistration.appName") }),
    ).toHaveValue("my-bot-acme");

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

  it("stops following the organization once the app name was edited", async () => {
    const user = userEvent.setup();
    renderPanel(vi.fn(), "acme");

    const appNameField = screen.getByRole("textbox", {
      name: t("setup.members.githubAppRegistration.appName"),
    });
    await user.clear(appNameField);
    await user.type(appNameField, "custom-name");
    await user.type(
      screen.getByRole("textbox", {
        name: t("setup.members.githubAppRegistration.organization"),
      }),
      "-2",
    );

    expect(appNameField).toHaveValue("custom-name");

    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );

    await waitFor(() =>
      expect(startGitHubAppRegistration).toHaveBeenCalledWith({
        app_name: "custom-name",
        organization: "acme-2",
      }),
    );
  });

  it("drops the previous member's edited fields and started registration", async () => {
    const user = userEvent.setup();
    const switchMember = renderSwitchablePanel();

    const appNameField = () =>
      screen.getByRole("textbox", { name: t("setup.members.githubAppRegistration.appName") });
    await user.clear(appNameField());
    await user.type(appNameField(), "custom-app");
    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );
    await screen.findByText(t("setup.members.githubAppRegistration.pending"));

    switchMember();

    expect(appNameField()).toHaveValue("other-bot-acme");
    // The pending state polls and reopens the previous member's registration.
    expect(
      screen.queryByText(t("setup.members.githubAppRegistration.pending")),
    ).not.toBeInTheDocument();
  });

  it("caps the suggested name at the length the backend accepts", async () => {
    renderPanel(vi.fn(), "very-long-organization-name");

    const appNameField = screen.getByRole("textbox", {
      name: t("setup.members.githubAppRegistration.appName"),
    });
    // "my-bot-very-long-organization-name" is 34 characters exactly.
    expect(appNameField).toHaveValue("my-bot-very-long-organization-name");

    renderPanel(vi.fn(), "very-long-organization-name-that-overflows");
    const [, secondField] = screen.getAllByRole("textbox", {
      name: t("setup.members.githubAppRegistration.appName"),
    });
    expect((secondField as HTMLInputElement).value.length).toBeLessThanOrEqual(34);
    expect((secondField as HTMLInputElement).value).not.toMatch(/-$/);
  });

  it("blocks a hand-edited name that exceeds the length limit", async () => {
    const user = userEvent.setup();
    renderPanel();

    const appNameField = screen.getByRole("textbox", {
      name: t("setup.members.githubAppRegistration.appName"),
    });
    await user.clear(appNameField);
    await user.type(appNameField, "x".repeat(35));

    expect(
      screen.getByText(t("setup.members.githubAppRegistration.errors.invalidAppName")),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    ).toBeDisabled();
    expect(startGitHubAppRegistration).not.toHaveBeenCalled();
  });

  it("keeps an emptied app name empty instead of falling back to the member ID", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.clear(
      screen.getByRole("textbox", { name: t("setup.members.githubAppRegistration.appName") }),
    );

    expect(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    ).toBeDisabled();
    expect(startGitHubAppRegistration).not.toHaveBeenCalled();
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
