import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ComponentProps } from "react";
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
import {
  GitHubAppRegistrationPanel,
  githubAppSaveFailure,
  type GitHubAppSaveOutcome,
} from "./GitHubAppRegistration";
import { TestMantineProvider } from "../test/TestMantineProvider";

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
  person_id: "my-bot",
  start_url: "http://127.0.0.1:8765/github-app/registrations/state-1/start",
  slug: "",
  app_id: null,
  html_url: "",
  github_username: "",
  git_email: "",
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
  installation_page_url: "https://github.com/apps/my-bot/installations/new",
};

const installedRegistration: GitHubAppRegistrationStatus = {
  ...convertedRegistration,
  status: "installed",
  installation_id: 86632391,
};

type PanelProps = Omit<
  ComponentProps<typeof GitHubAppRegistrationPanel>,
  "registration" | "onRegistrationChange"
> & { initialRegistration?: GitHubAppRegistrationStatus | null };

/** Holds the registration the way the member form does. */
function Harness({ initialRegistration = null, ...props }: PanelProps) {
  const [registration, setRegistration] = useState(initialRegistration);
  return (
    <GitHubAppRegistrationPanel
      {...props}
      registration={registration}
      onRegistrationChange={setRegistration}
    />
  );
}

function renderPanel(
  onApplied = vi.fn(),
  defaultOrganization = "",
  extra: Partial<PanelProps> = {},
) {
  render(
    <TestMantineProvider>
      <Harness
        personId="my-bot"
        defaultAppName="my-bot"
        defaultOrganization={defaultOrganization}
        saveOutcome={null}
        onApplied={onApplied}
        pollIntervalMs={20}
        memberKey="edit:my-bot"
        {...extra}
      />
    </TestMantineProvider>,
  );
  return onApplied;
}

function renderSwitchablePanel() {
  const props = {
    defaultOrganization: "acme",
    onApplied: vi.fn(),
    pollIntervalMs: 20,
    saveOutcome: null,
  };
  const { rerender } = render(
    <TestMantineProvider>
      <Harness {...props} personId="my-bot" defaultAppName="my-bot" memberKey="edit:my-bot" />
    </TestMantineProvider>,
  );
  return () =>
    rerender(
      <TestMantineProvider>
        <Harness
          {...props}
          personId="other-bot"
          defaultAppName="other-bot"
          memberKey="edit:other-bot"
        />
      </TestMantineProvider>,
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
        person_id: "my-bot",
        organization: "acme",
      }),
    );
    expect(openExternal).toHaveBeenCalledWith(pendingRegistration.start_url);
    expect(screen.getByText(t("setup.members.githubAppRegistration.pending"))).toBeInTheDocument();
  });

  it("applies the bot identity and follows the registration to its installation", async () => {
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
      }),
    );
    expect(
      screen.getByText(t("setup.members.githubAppRegistration.converted", { slug: "my-bot" })),
    ).toBeInTheDocument();

    vi.mocked(getGitHubAppRegistration).mockResolvedValue(installedRegistration);
    expect(
      await screen.findByText(t("setup.members.githubAppRegistration.installed")),
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
        person_id: "my-bot",
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
        person_id: "my-bot",
        organization: "acme-2",
      }),
    );
  });

  it("drops the previous member's edited fields", async () => {
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
  });

  it("shows no failure of a start the form has moved on from", async () => {
    let fail: (error: Error) => void = () => {};
    vi.mocked(startGitHubAppRegistration).mockReturnValue(
      new Promise((_, reject) => {
        fail = reject;
      }),
    );
    const user = userEvent.setup();
    const switchMember = renderSwitchablePanel();
    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );

    switchMember();
    fail(new Error("GitHub App name must be 1-34 characters."));

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
      ).not.toHaveAttribute("data-loading"),
    );
    expect(screen.queryByText("GitHub App name must be 1-34 characters.")).not.toBeInTheDocument();
  });

  it("cannot register before the member has an ID", () => {
    renderPanel(vi.fn(), "", { personId: " " });

    expect(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    ).toBeDisabled();
  });

  it.each<[GitHubAppSaveOutcome | null, GitHubAppRegistrationStatus | null, string | null]>([
    [null, installedRegistration, "setup.members.githubAppRegistration.installed"],
    ["failed", installedRegistration, "setup.members.githubAppRegistration.saveFailed"],
    ["saved", null, "setup.members.githubAppRegistration.saved"],
    ["expired", null, "setup.members.githubAppRegistration.errors.expired"],
  ])("shows the %s save outcome of a registration", (saveOutcome, registration, key) => {
    renderPanel(vi.fn(), "", { saveOutcome, initialRegistration: registration });

    expect(screen.getByText(t(key!))).toBeInTheDocument();
  });

  it("does not show the last save outcome beside a later registration's failure", async () => {
    const user = userEvent.setup();
    renderPanel(vi.fn(), "", { saveOutcome: "saved" });
    vi.mocked(getGitHubAppRegistration).mockRejectedValue(
      new ApiRequestError({ code: "github_app_registration_not_found", message: "", context: {} }),
    );

    await user.click(
      screen.getByRole("button", { name: t("setup.members.githubAppRegistration.register") }),
    );

    expect(
      await screen.findByText(t("setup.members.githubAppRegistration.errors.expired")),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(t("setup.members.githubAppRegistration.saved")),
    ).not.toBeInTheDocument();
  });

  it("hides the last save outcome once another registration starts", () => {
    renderPanel(vi.fn(), "", { saveOutcome: "saved", initialRegistration: pendingRegistration });

    expect(
      screen.queryByText(t("setup.members.githubAppRegistration.saved")),
    ).not.toBeInTheDocument();
    expect(screen.getByText(t("setup.members.githubAppRegistration.pending"))).toBeInTheDocument();
  });

  it("reads an unknown registration as expired and any other save failure as failed", () => {
    const apiError = (code: string) => new ApiRequestError({ code, message: code, context: {} });

    expect(githubAppSaveFailure(apiError("github_app_registration_not_found"))).toBe("expired");
    expect(githubAppSaveFailure(apiError("github_app_registration_member_mismatch"))).toBe(
      "failed",
    );
    expect(githubAppSaveFailure(new Error("keychain unavailable"))).toBe("failed");
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
