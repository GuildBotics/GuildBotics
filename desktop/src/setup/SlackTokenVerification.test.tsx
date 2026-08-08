import { MantineProvider } from "@mantine/core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { verifySlackTokens, type SlackTokenVerifyResponse } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { SlackTokenVerificationPanel } from "./SlackTokenVerification";

vi.mock("../api/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/client")>();
  return { ...actual, verifySlackTokens: vi.fn() };
});

const t = i18n.getFixedT("en");

const bothOk: SlackTokenVerifyResponse = {
  bot_ok: true,
  bot_user_id: "U0BOT",
  bot_display_name: "alice-bot",
  workspace: "GuildBotics HQ",
  bot_error: "",
  bot_source: "input",
  scopes_ok: true,
  scope_error: "",
  scope_needed: "",
  app_token_ok: true,
  app_token_error: "",
  app_token_source: "input",
  channels: [],
};

function botLine(overrides: Partial<SlackTokenVerifyResponse> = {}): string {
  const verification = { ...bothOk, ...overrides };
  const label = t(
    verification.bot_source === "stored"
      ? "setup.members.slackTokenVerify.labels.botStored"
      : "setup.members.slackTokenVerify.labels.bot",
  );
  const detail = verification.bot_ok
    ? t(
        verification.workspace
          ? "setup.members.slackTokenVerify.ok.bot"
          : "setup.members.slackTokenVerify.ok.botNoWorkspace",
        {
          displayName: verification.bot_display_name,
          userId: verification.bot_user_id,
          workspace: verification.workspace,
        },
      )
    : t(`setup.members.slackTokenVerify.errors.${verification.bot_error}`);
  return `${label}: ${detail}`;
}

function appTokenLine(overrides: Partial<SlackTokenVerifyResponse> = {}): string {
  const verification = { ...bothOk, ...overrides };
  const label = t(
    verification.app_token_source === "stored"
      ? "setup.members.slackTokenVerify.labels.appTokenStored"
      : "setup.members.slackTokenVerify.labels.appToken",
  );
  const detail = verification.app_token_ok
    ? t("setup.members.slackTokenVerify.ok.appToken")
    : t(`setup.members.slackTokenVerify.errors.${verification.app_token_error}`);
  return `${label}: ${detail}`;
}

function renderPanel(botToken = "xoxb-1", appToken = "xapp-1", personId = "alice") {
  const { rerender } = render(
    <MantineProvider env="test">
      <SlackTokenVerificationPanel botToken={botToken} appToken={appToken} personId={personId} />
    </MantineProvider>,
  );
  return (nextBotToken: string, nextAppToken: string) =>
    rerender(
      <MantineProvider env="test">
        <SlackTokenVerificationPanel
          botToken={nextBotToken}
          appToken={nextAppToken}
          personId={personId}
        />
      </MantineProvider>,
    );
}

function verifyButton() {
  return screen.getByRole("button", { name: t("setup.members.slackTokenVerify.button") });
}

beforeEach(() => {
  vi.mocked(verifySlackTokens).mockReset().mockResolvedValue(bothOk);
});

describe("SlackTokenVerificationPanel", () => {
  it("sends both tokens and reports the bot identity with its workspace", async () => {
    const user = userEvent.setup();
    renderPanel();

    await user.click(verifyButton());

    expect(verifySlackTokens).toHaveBeenCalledWith({
      bot_token: "xoxb-1",
      app_token: "xapp-1",
      person_id: "alice",
      channels: [],
    });
    expect(await screen.findByText(botLine())).toBeInTheDocument();
    expect(screen.getByText(appTokenLine())).toBeInTheDocument();
  });

  it("omits the workspace from the message when Slack did not return one", async () => {
    vi.mocked(verifySlackTokens).mockResolvedValue({ ...bothOk, workspace: "" });
    const user = userEvent.setup();
    renderPanel();

    await user.click(verifyButton());

    expect(await screen.findByText(botLine({ workspace: "" }))).toBeInTheDocument();
  });

  it("explains a swapped bot/app token pair", async () => {
    vi.mocked(verifySlackTokens).mockResolvedValue({
      ...bothOk,
      bot_ok: false,
      bot_error: "wrong_token_type",
      app_token_ok: false,
      app_token_error: "wrong_token_type",
    });
    const user = userEvent.setup();
    renderPanel("xapp-1", "xoxb-1");

    await user.click(verifyButton());

    const failed = { bot_ok: false, bot_error: "wrong_token_type" } as const;
    expect(await screen.findByText(botLine(failed))).toBeInTheDocument();
    expect(
      screen.getByText(appTokenLine({ app_token_ok: false, app_token_error: "wrong_token_type" })),
    ).toBeInTheDocument();
  });

  it("shows one token as valid while the other failed", async () => {
    vi.mocked(verifySlackTokens).mockResolvedValue({
      ...bothOk,
      app_token_ok: false,
      app_token_error: "invalid_auth",
    });
    const user = userEvent.setup();
    renderPanel();

    await user.click(verifyButton());

    expect(await screen.findByText(botLine())).toBeInTheDocument();
    expect(
      screen.getByText(appTokenLine({ app_token_ok: false, app_token_error: "invalid_auth" })),
    ).toBeInTheDocument();
  });

  it("falls back to the raw code for an unknown Slack error", async () => {
    vi.mocked(verifySlackTokens).mockResolvedValue({
      ...bothOk,
      bot_ok: false,
      bot_error: "ratelimited",
    });
    const user = userEvent.setup();
    renderPanel();

    await user.click(verifyButton());

    const label = t("setup.members.slackTokenVerify.labels.bot");
    const detail = t("setup.members.slackTokenVerify.errors.unknown", { code: "ratelimited" });
    expect(await screen.findByText(`${label}: ${detail}`)).toBeInTheDocument();
  });

  it("drops the verdict once the tokens it describes are changed", async () => {
    const user = userEvent.setup();
    const setTokens = renderPanel();

    await user.click(verifyButton());
    await screen.findByText(appTokenLine());

    setTokens("xoxb-2", "xapp-1");

    expect(screen.queryByText(appTokenLine())).not.toBeInTheDocument();
  });

  it("ignores a response that arrives after the tokens were changed", async () => {
    let resolveVerify: (value: SlackTokenVerifyResponse) => void = () => {};
    vi.mocked(verifySlackTokens).mockReturnValue(
      new Promise<SlackTokenVerifyResponse>((resolve) => {
        resolveVerify = resolve;
      }),
    );
    const user = userEvent.setup();
    const setTokens = renderPanel();

    await user.click(verifyButton());
    setTokens("xoxb-2", "xapp-1");
    resolveVerify(bothOk);

    await waitFor(() => expect(verifyButton()).not.toHaveAttribute("data-loading"));
    expect(screen.queryByText(appTokenLine())).not.toBeInTheDocument();
  });

  it("shows a request failure instead of a per-token result", async () => {
    vi.mocked(verifySlackTokens).mockRejectedValue(new Error("backend is down"));
    const user = userEvent.setup();
    renderPanel();

    await user.click(verifyButton());

    expect(await screen.findByText("backend is down")).toBeInTheDocument();
    expect(screen.queryByText(appTokenLine())).not.toBeInTheDocument();
  });

  it("reports a token that authenticates but carries no scopes", async () => {
    vi.mocked(verifySlackTokens).mockResolvedValue({
      ...bothOk,
      scopes_ok: false,
      scope_error: "missing_scope",
      scope_needed: "channels:read",
    });
    const user = userEvent.setup();
    renderPanel();

    await user.click(verifyButton());

    // The token passes auth.test, so the scope line is what tells the truth.
    expect(await screen.findByText(botLine())).toBeInTheDocument();
    const label = t("setup.members.slackTokenVerify.labels.scopes");
    const detail = t("setup.members.slackTokenVerify.scopeMissing", {
      needed: "channels:read",
    });
    expect(screen.getByText(`${label}: ${detail}`)).toBeInTheDocument();
  });

  it("omits the scope line when the token itself was rejected", async () => {
    vi.mocked(verifySlackTokens).mockResolvedValue({
      ...bothOk,
      bot_ok: false,
      bot_error: "invalid_auth",
      scopes_ok: false,
    });
    const user = userEvent.setup();
    renderPanel();

    await user.click(verifyButton());

    await screen.findByText(botLine({ bot_ok: false, bot_error: "invalid_auth" }));
    expect(
      screen.queryByText(new RegExp(`^${t("setup.members.slackTokenVerify.labels.scopes")}:`)),
    ).not.toBeInTheDocument();
  });

  it("hides the scope line when the backend returned no scope verdict", async () => {
    // A backend older than the scope probe omits these fields entirely; an
    // absent verdict must not be rendered as a failure with an empty reason.
    const { scopes_ok, scope_error, scope_needed, ...withoutScopeFields } = bothOk;
    void scopes_ok;
    void scope_error;
    void scope_needed;
    vi.mocked(verifySlackTokens).mockResolvedValue(withoutScopeFields as SlackTokenVerifyResponse);
    const user = userEvent.setup();
    renderPanel();

    await user.click(verifyButton());

    await screen.findByText(botLine());
    expect(
      screen.queryByText(new RegExp(`^${t("setup.members.slackTokenVerify.labels.scopes")}:`)),
    ).not.toBeInTheDocument();
  });

  it("reports each channel the bot cannot read, naming the invite to run", async () => {
    vi.mocked(verifySlackTokens).mockResolvedValue({
      ...bothOk,
      channels: [
        { channel: "general", ok: true, error: "" },
        { channel: "random", ok: false, error: "not_in_channel" },
        { channel: "typo", ok: false, error: "not_found" },
      ],
    });
    const user = userEvent.setup();
    renderPanel("xoxb-1", "xapp-1", "alice");

    await user.click(verifyButton());

    expect(
      await screen.findByText(`#general: ${t("setup.members.slackTokenVerify.ok.channel")}`),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        `#random: ${t("setup.members.slackTokenVerify.channelNotJoined", {
          botName: "alice-bot",
        })}`,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(`#typo: ${t("setup.members.slackTokenVerify.channelNotFound")}`),
    ).toBeInTheDocument();
  });

  it("sends the channels currently in the form", async () => {
    const user = userEvent.setup();
    render(
      <MantineProvider env="test">
        <SlackTokenVerificationPanel
          botToken="xoxb-1"
          appToken="xapp-1"
          personId="alice"
          channels={["general", "random"]}
        />
      </MantineProvider>,
    );

    await user.click(verifyButton());

    expect(verifySlackTokens).toHaveBeenCalledWith({
      bot_token: "xoxb-1",
      app_token: "xapp-1",
      person_id: "alice",
      channels: ["general", "random"],
    });
  });

  it("names the saved token when an empty field was checked against it", async () => {
    vi.mocked(verifySlackTokens).mockResolvedValue({
      ...bothOk,
      bot_source: "stored",
      app_token_source: "stored",
    });
    const user = userEvent.setup();
    renderPanel("", "");

    await user.click(verifyButton());

    // An empty field keeps the saved token, so "OK" must say which one it is.
    expect(await screen.findByText(botLine({ bot_source: "stored" }))).toBeInTheDocument();
    expect(screen.getByText(appTokenLine({ app_token_source: "stored" }))).toBeInTheDocument();
  });
});
