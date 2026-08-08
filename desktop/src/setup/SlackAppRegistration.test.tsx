import { MantineProvider } from "@mantine/core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ApiRequestError,
  startSlackAppRegistration,
  type SlackAppRegistrationStatus,
} from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { openExternal } from "../openExternal";
import { SlackAppRegistrationPanel } from "./SlackAppRegistration";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return {
    ...actual,
    startSlackAppRegistration: vi.fn(),
  };
});
vi.mock("../openExternal", () => ({ openExternal: vi.fn(async () => {}) }));

const t = i18n.getFixedT("en");

const registration: SlackAppRegistrationStatus = {
  app_name: "alice",
  registration_url: "https://api.slack.com/apps?new_app=1&manifest_json=%7B%7D",
  app_directory_url: "https://api.slack.com/apps",
};

function renderPanel(defaultAppName = "alice", memberKey = "edit:alice") {
  const { rerender } = render(
    <MantineProvider env="test">
      <SlackAppRegistrationPanel defaultAppName={defaultAppName} memberKey={memberKey} />
    </MantineProvider>,
  );
  return (nextAppName: string, nextMemberKey: string) =>
    rerender(
      <MantineProvider env="test">
        <SlackAppRegistrationPanel defaultAppName={nextAppName} memberKey={nextMemberKey} />
      </MantineProvider>,
    );
}

function registerButton() {
  return screen.getByRole("button", { name: t("setup.members.slackAppRegistration.register") });
}

beforeEach(() => {
  vi.mocked(startSlackAppRegistration).mockReset().mockResolvedValue(registration);
  vi.mocked(openExternal).mockClear();
});

describe("SlackAppRegistrationPanel", () => {
  it("prefills the member ID and registers with it", async () => {
    const user = userEvent.setup();
    renderPanel();

    expect(
      screen.getByRole("textbox", { name: t("setup.members.slackAppRegistration.appName") }),
    ).toHaveValue("alice");

    await user.click(registerButton());

    await waitFor(() =>
      expect(startSlackAppRegistration).toHaveBeenCalledWith({ app_name: "alice" }),
    );
    expect(openExternal).toHaveBeenCalledWith(registration.registration_url);
  });

  it("uses the edited app name instead of the prefilled member ID", async () => {
    const user = userEvent.setup();
    renderPanel();

    const field = screen.getByRole("textbox", {
      name: t("setup.members.slackAppRegistration.appName"),
    });
    await user.clear(field);
    await user.type(field, "  Alice Bot  ");
    await user.click(registerButton());

    await waitFor(() =>
      expect(startSlackAppRegistration).toHaveBeenCalledWith({ app_name: "Alice Bot" }),
    );
  });

  it("keeps an emptied app name empty instead of falling back to the member ID", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.clear(
      screen.getByRole("textbox", { name: t("setup.members.slackAppRegistration.appName") }),
    );

    expect(registerButton()).toBeDisabled();
    expect(startSlackAppRegistration).not.toHaveBeenCalled();
  });

  it("shows the remaining manual steps only after the app was started", async () => {
    const user = userEvent.setup();
    renderPanel();

    expect(
      screen.queryByText(t("setup.members.slackAppRegistration.stepsTitle")),
    ).not.toBeInTheDocument();

    await user.click(registerButton());

    expect(
      await screen.findByText(t("setup.members.slackAppRegistration.stepsTitle")),
    ).toBeInTheDocument();
    for (const step of ["create", "reinstall", "copyBot", "copyAppToken", "paste"]) {
      expect(
        screen.getByText(t(`setup.members.slackAppRegistration.steps.${step}`)),
      ).toBeInTheDocument();
    }
    expect(
      screen.getByText(t("setup.members.slackAppRegistration.signedOutHint")),
    ).toBeInTheDocument();
  });

  it("reopens the creation page and the app list from the backend URLs", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(registerButton());
    await screen.findByText(t("setup.members.slackAppRegistration.stepsTitle"));
    vi.mocked(openExternal).mockClear();

    await user.click(
      screen.getByRole("button", { name: t("setup.members.slackAppRegistration.reopen") }),
    );
    expect(openExternal).toHaveBeenCalledWith(registration.registration_url);

    await user.click(
      screen.getByRole("button", { name: t("setup.members.slackAppRegistration.openAppList") }),
    );
    expect(openExternal).toHaveBeenCalledWith(registration.app_directory_url);
  });

  it("translates the invalid app name error code", async () => {
    vi.mocked(startSlackAppRegistration).mockRejectedValue(
      new ApiRequestError({
        code: "invalid_slack_app_name",
        message: "Slack App name must be 1-35 characters.",
        context: {},
      }),
    );
    const user = userEvent.setup();
    renderPanel();

    await user.click(registerButton());

    expect(
      await screen.findByText(t("setup.members.slackAppRegistration.errors.invalidAppName")),
    ).toBeInTheDocument();
    expect(openExternal).not.toHaveBeenCalled();
    expect(
      screen.queryByText(t("setup.members.slackAppRegistration.stepsTitle")),
    ).not.toBeInTheDocument();
  });

  it("shows an unexpected backend error message as-is", async () => {
    vi.mocked(startSlackAppRegistration).mockRejectedValue(new Error("backend is down"));
    const user = userEvent.setup();
    renderPanel();

    await user.click(registerButton());

    expect(await screen.findByText("backend is down")).toBeInTheDocument();
  });

  it("drops the previous member's edited name and started registration", async () => {
    const user = userEvent.setup();
    const switchMember = renderPanel();

    const field = () =>
      screen.getByRole("textbox", { name: t("setup.members.slackAppRegistration.appName") });
    await user.clear(field());
    await user.type(field(), "custom-app");
    await user.click(registerButton());
    await screen.findByText(t("setup.members.slackAppRegistration.stepsTitle"));

    switchMember("bob", "edit:bob");

    expect(field()).toHaveValue("bob");
    // The step guide's links point at the previous member's manifest.
    expect(
      screen.queryByText(t("setup.members.slackAppRegistration.stepsTitle")),
    ).not.toBeInTheDocument();
  });

  it("blocks a hand-edited name that exceeds the length limit", async () => {
    const user = userEvent.setup();
    renderPanel();

    const field = screen.getByRole("textbox", {
      name: t("setup.members.slackAppRegistration.appName"),
    });
    await user.clear(field);
    await user.type(field, "x".repeat(36));

    expect(
      screen.getByText(t("setup.members.slackAppRegistration.errors.invalidAppName")),
    ).toBeInTheDocument();
    expect(registerButton()).toBeDisabled();
    expect(startSlackAppRegistration).not.toHaveBeenCalled();
  });

  it("disables registration when there is no name to use", () => {
    renderPanel("");

    expect(registerButton()).toBeDisabled();
  });
});
