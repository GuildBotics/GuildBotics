import { describe, expect, it } from "vitest";

import i18n from "../../i18n";
import "../../i18n";
import { branvilAccount, type BranvilForm } from "./branvil/BranvilAccount";
import { BRANVIL_BASE_URL, branvilCodeHosting, branvilTickets } from "./branvil/BranvilServices";
import type { AppRegistrationStatus } from "./github/api";
import { githubAccount, type GitHubForm } from "./github/GitHubAccount";
import { isSlackChannel, slackAccount, type SlackForm } from "./slack/SlackAccount";
import type { MemberContext } from "./types";

const t = i18n.getFixedT("en");

function context(overrides: Partial<MemberContext> = {}): MemberContext {
  return {
    human: false,
    personId: "my-bot",
    savedPersonId: "",
    memberKey: "new",
    gitEmail: "my-bot@example.com",
    setGitEmail: () => undefined,
    secretEnvKeys: {},
    chatChannels: [],
    chatChannelsField: null,
    services: {},
    ...overrides,
  };
}

function github(overrides: Partial<GitHubForm> = {}): GitHubForm {
  return { ...githubAccount.initial(), ...overrides };
}

function slack(overrides: Partial<SlackForm> = {}): SlackForm {
  return { ...slackAccount.initial(), ...overrides };
}

function registration(status: AppRegistrationStatus["status"]): AppRegistrationStatus {
  return {
    state: "registration-1",
    status,
    app_name: "my-bot",
    person_id: "my-bot",
    start_url: "",
    slug: "my-bot",
    app_id: 1978826,
    html_url: "https://github.com/apps/my-bot",
    username: "my-bot[bot]",
    git_email: "233270845+my-bot[bot]@users.noreply.github.com",
    installation_id: status === "installed" ? 86632391 : null,
    installation_page_url: "",
    installation_check_error: "",
  };
}

const APP_URL = "https://github.com/organizations/acme/settings/apps/my-app";

describe("githubAccount.request", () => {
  it("sends only the username for a human, whatever the form's account type", () => {
    expect(
      githubAccount.request(
        github({ accountType: "machine_user", username: " alice ", accessToken: "ghp_x" }),
        true,
      ),
    ).toEqual({ account_type: "human", username: "alice" });
  });

  it("sends a machine user's access token, and leaves a blank one out to keep the stored", () => {
    expect(
      githubAccount.request(
        github({ accountType: "machine_user", username: "bot", accessToken: "ghp_abcdefgh" }),
        false,
      ),
    ).toEqual({ account_type: "machine_user", username: "bot", access_token: "ghp_abcdefgh" });
    expect(
      githubAccount.request(github({ accountType: "machine_user", username: "bot" }), false),
    ).toEqual({ account_type: "machine_user", username: "bot", access_token: undefined });
  });

  it("sends an empty account type for a member without a GitHub account", () => {
    expect(githubAccount.request(github(), false)).toEqual({ account_type: "", username: "" });
  });

  it("sends a GitHub App's IDs and key path", () => {
    expect(
      githubAccount.request(
        github({
          accountType: "github_apps",
          username: "my-app[bot]",
          installationId: "12",
          appId: "34",
          // A picked or pasted path keeps no whitespace around it.
          privateKeyPath: " /keys/app.pem ",
        }),
        false,
      ),
    ).toEqual({
      account_type: "github_apps",
      username: "my-app[bot]",
      installation_id: 12,
      app_id: 34,
      private_key_path: "/keys/app.pem",
    });
  });

  it("names an installed registration instead of the app's IDs and key", () => {
    expect(
      githubAccount.request(
        github({
          accountType: "github_apps",
          username: "my-bot[bot]",
          installationId: "12",
          registration: registration("installed"),
        }),
        false,
      ),
    ).toEqual({
      account_type: "github_apps",
      username: "my-bot[bot]",
      app_registration_id: "registration-1",
    });
  });

  it("sends the typed IDs while a registration is not installed yet", () => {
    const account = githubAccount.request(
      github({
        accountType: "github_apps",
        username: "my-bot[bot]",
        registration: registration("converted"),
      }),
      false,
    );
    expect(account).not.toHaveProperty("app_registration_id");
    expect(account).toMatchObject({ installation_id: undefined, app_id: undefined });
  });
});

describe("githubAccount.fromView", () => {
  it("reads the stored account and which secrets are stored", () => {
    const form = githubAccount.fromView(
      {
        account_type: "machine_user",
        username: "bot",
        installation_id: null,
        app_id: null,
        has_private_key: false,
        has_access_token: true,
      },
      githubAccount.initial(),
      false,
    );
    expect(form).toMatchObject({
      accountType: "machine_user",
      username: "bot",
      accessToken: "",
      setupMode: "create",
      stored: { privateKey: false, accessToken: true },
    });
  });

  it("names an already-registered app once its app ID is stored", () => {
    const form = githubAccount.fromView(
      {
        account_type: "github_apps",
        username: "my-app[bot]",
        installation_id: 12,
        app_id: 34,
        has_private_key: true,
        has_access_token: false,
      },
      githubAccount.initial(),
      false,
    );
    expect(form).toMatchObject({
      accountType: "github_apps",
      installationId: "12",
      appId: "34",
      setupMode: "existing",
      stored: { privateKey: true, accessToken: false },
    });
  });

  it("forces a human member's account to the human's own user", () => {
    expect(
      githubAccount.fromView(
        { account_type: "machine_user", username: "alice" },
        githubAccount.initial(),
        true,
      ).accountType,
    ).toBe("human");
  });

  it("starts from no account when nothing is stored", () => {
    expect(githubAccount.fromView(undefined, githubAccount.initial(), false)).toEqual(
      githubAccount.initial(),
    );
  });

  it("keeps a registration in flight across a reload of the member", () => {
    const inFlight = registration("converted");
    const form = githubAccount.fromView(
      { account_type: "", username: "", app_id: 99 },
      github({ registration: inFlight }),
      false,
    );
    expect(form.registration).toBe(inFlight);
    expect(form.accountType).toBe("github_apps");
    expect(form.setupMode).toBe("create");
  });
});

describe("githubAccount.errors", () => {
  it("asks nothing of a member without a GitHub account", () => {
    expect(githubAccount.errors(github(), context({ gitEmail: "" }), t)).toEqual({});
  });

  it("requires an access token for a machine user", () => {
    const errors = githubAccount.errors(
      github({ accountType: "machine_user", username: "bot" }),
      context(),
      t,
    );
    expect(errors).toEqual({ accessToken: t("setup.validation.githubAccessTokenRequired") });
  });

  it("accepts a stored access token for a machine user", () => {
    expect(
      githubAccount.errors(
        github({
          accountType: "machine_user",
          username: "bot",
          stored: { privateKey: false, accessToken: true },
        }),
        context(),
        t,
      ),
    ).toEqual({});
  });

  it("validates the format of a provided access token", () => {
    expect(
      githubAccount.errors(
        github({ accountType: "machine_user", username: "bot", accessToken: "bad-token" }),
        context(),
        t,
      ).accessToken,
    ).toBe(t("setup.validation.githubAccessTokenInvalid"));
  });

  it("requires and validates the username", () => {
    expect(
      githubAccount.errors(github({ accountType: "machine_user" }), context(), t).username,
    ).toBe(t("setup.validation.memberGithubUsernameRequired"));
    expect(
      githubAccount.errors(github({ accountType: "machine_user", username: "-bot" }), context(), t)
        .username,
    ).toBe(t("setup.validation.memberGithubUsernameInvalid"));
  });

  it("requires a valid commit email from the member", () => {
    const form = github({ accountType: "human", username: "alice" });
    expect(githubAccount.errors(form, context({ gitEmail: " " }), t).gitEmail).toBe(
      t("setup.validation.memberGitEmailRequired"),
    );
    expect(githubAccount.errors(form, context({ gitEmail: "not-an-email" }), t).gitEmail).toBe(
      t("setup.validation.memberGitEmailInvalid"),
    );
  });

  it("holds a human member to the human's own user even when the form names none", () => {
    expect(githubAccount.errors(github(), context({ human: true }), t).username).toBe(
      t("setup.validation.memberGithubUsernameRequired"),
    );
  });

  it("requires installation id, app id, and private key path for a GitHub App", () => {
    const errors = githubAccount.errors(
      github({ accountType: "github_apps", identity: APP_URL, username: "my-app[bot]" }),
      context(),
      t,
    );
    expect(errors).toEqual({
      installationId: t("setup.validation.githubInstallationIdRequired"),
      appId: t("setup.validation.githubAppIdRequired"),
      privateKeyPath: t("setup.validation.githubPrivateKeyPathRequired"),
    });
    const invalid = githubAccount.errors(
      github({
        accountType: "github_apps",
        username: "my-app[bot]",
        installationId: "x",
        appId: "y",
        privateKeyPath: "/keys/app.pem",
      }),
      context(),
      t,
    );
    expect(invalid.installationId).toBe(t("setup.validation.githubInstallationIdInvalid"));
    expect(invalid.appId).toBe(t("setup.validation.githubAppIdInvalid"));
  });

  it("accepts a stored private key for a GitHub App", () => {
    // After registration the key lives in the OS secret store and its path is
    // not kept; editing the member must not ask for a path again.
    expect(
      githubAccount.errors(
        github({
          accountType: "github_apps",
          username: "my-app[bot]",
          installationId: "1",
          appId: "2",
          stored: { privateKey: true, accessToken: false },
        }),
        context(),
        t,
      ),
    ).toEqual({});
  });

  it("asks for the app's URL until the app is resolved, and validates it", () => {
    expect(
      githubAccount.errors(github({ accountType: "github_apps" }), context({ gitEmail: "" }), t)
        .identity,
    ).toBe(t("setup.validation.memberGithubAppsUrlRequired"));
    expect(
      githubAccount.errors(
        github({ accountType: "github_apps", identity: "https://example.com/app" }),
        context(),
        t,
      ).identity,
    ).toBe(t("setup.validation.memberGithubAppsUrlInvalid"));
  });

  it("lets an installed registration supply the app's IDs and key", () => {
    expect(
      githubAccount.errors(
        github({
          accountType: "github_apps",
          username: "my-bot[bot]",
          registration: registration("installed"),
        }),
        context(),
        t,
      ),
    ).toEqual({});
  });

  it("blocks the save while the registered app is not installed, with a message the panel gives", () => {
    expect(
      githubAccount.errors(
        github({
          accountType: "github_apps",
          username: "my-bot[bot]",
          registration: registration("converted"),
        }),
        context(),
        t,
      ),
    ).toEqual({ registration: "" });
  });
});

describe("slackAccount.request", () => {
  it("sends a human member's own Slack user", () => {
    expect(
      slackAccount.request(slack({ userId: " U01234567 ", botToken: "xoxb-1" }), true),
    ).toEqual({ user_id: "U01234567" });
  });

  it("sends a bot's tokens trimmed", () => {
    expect(
      slackAccount.request(
        slack({ userId: "U01234567", botToken: " xoxb-1 ", appToken: " xapp-1 " }),
        false,
      ),
    ).toEqual({ bot_token: "xoxb-1", app_token: "xapp-1" });
  });
});

describe("slackAccount.fromView", () => {
  it("edits a member that carries tokens as an existing app", () => {
    expect(
      slackAccount.fromView(
        { user_id: "", has_bot_token: true, has_app_token: false },
        slackAccount.initial(),
        false,
      ),
    ).toEqual({
      ...slackAccount.initial(),
      setupMode: "existing",
      stored: { botToken: true, appToken: false },
    });
  });

  it("sets up a new app for a member without tokens", () => {
    expect(
      slackAccount.fromView({ user_id: "U01234567" }, slackAccount.initial(), true),
    ).toMatchObject({ userId: "U01234567", setupMode: "create" });
  });
});

describe("slackAccount.errors", () => {
  it("requires a valid Slack User ID for a human member", () => {
    const human = context({ human: true });
    expect(slackAccount.errors(slack(), human, t).userId).toBe(
      t("setup.validation.slackUserIdRequired"),
    );
    expect(slackAccount.errors(slack({ userId: "not-a-slack-id" }), human, t).userId).toBe(
      t("setup.validation.slackUserIdInvalid"),
    );
    expect(slackAccount.errors(slack({ userId: "U01234567" }), human, t)).toEqual({});
  });

  it("asks no tokens of a bot that listens on no channel", () => {
    expect(slackAccount.errors(slack(), context(), t)).toEqual({});
  });

  it("requires both tokens of a bot that listens on channels, unless they are stored", () => {
    const listening = context({ chatChannels: ["general"] });
    expect(slackAccount.errors(slack(), listening, t)).toEqual({
      botToken: t("setup.validation.slackBotTokenRequired"),
      appToken: t("setup.validation.slackAppTokenRequired"),
    });
    expect(
      slackAccount.errors(slack({ stored: { botToken: true, appToken: true } }), listening, t),
    ).toEqual({});
  });

  it("validates the format of provided tokens", () => {
    expect(
      slackAccount.errors(
        slack({ botToken: "not-a-bot-token", appToken: "not-an-app-token" }),
        context(),
        t,
      ),
    ).toEqual({
      botToken: t("setup.validation.slackBotTokenInvalid"),
      appToken: t("setup.validation.slackAppTokenInvalid"),
    });
    expect(
      slackAccount.errors(
        slack({ botToken: "xoxb-valid-token", appToken: "xapp-valid-token" }),
        context({ chatChannels: ["general"] }),
        t,
      ),
    ).toEqual({});
  });
});

describe("isSlackChannel", () => {
  it("accepts channel IDs and localized channel names", () => {
    for (const channel of ["general", "#general", "開発", "かいはつ", "カイハツ", "C0123456789"]) {
      expect(isSlackChannel(channel)).toBe(true);
    }
  });

  it("rejects names Slack does not accept", () => {
    for (const channel of ["Dev", "_dev", "-dev", "#_dev", "#-dev", ""]) {
      expect(isSlackChannel(channel)).toBe(false);
    }
  });
});

function branvil(overrides: Partial<BranvilForm> = {}): BranvilForm {
  return { ...branvilAccount.initial(), ...overrides };
}

describe("branvilAccount", () => {
  it("sends a human's name, and an agent's name and token", () => {
    const form = branvil({ username: " aiko ", token: " bvt_1 " });

    expect(branvilAccount.request(form, true)).toEqual({ username: "aiko" });
    expect(branvilAccount.request(form, false)).toEqual({ username: "aiko", token: "bvt_1" });
  });

  it("edits a stored account with its token kept", () => {
    expect(
      branvilAccount.fromView({ username: "aiko", has_token: true }, branvil(), false),
    ).toEqual({ username: "aiko", token: "", storedToken: true });
    expect(branvilAccount.fromView(undefined, branvil(), false)).toEqual(branvil());
  });

  it("asks an agent for a token unless one is stored, and every member for a name", () => {
    expect(branvilAccount.errors(branvil(), context(), t)).toEqual({
      username: t("setup.branvil.usernameRequired"),
      token: t("setup.branvil.tokenRequired"),
    });
    expect(
      branvilAccount.errors(branvil({ username: "a b" }), context({ human: true }), t),
    ).toEqual({ username: t("setup.branvil.usernameInvalid") });
    expect(
      branvilAccount.errors(branvil({ username: "aiko", storedToken: true }), context(), t),
    ).toEqual({});
  });
});

describe("branvil services", () => {
  it("start from what the other kind says of the same Branvil", () => {
    expect(branvilTickets.initial({})).toEqual({
      base_url: BRANVIL_BASE_URL,
      owner: "",
      board: "",
    });
    expect(
      branvilCodeHosting.initial({
        ticket_service: { name: "branvil", base_url: "http://hub:9000", owner: "acme" },
      }),
    ).toEqual({ base_url: "http://hub:9000", owner: "acme" });
  });

  it("refuse what names no Branvil, scope, or board", () => {
    const value = { name: "branvil", base_url: "http://hub/x", owner: "acme/..", board: "board" };

    expect(branvilTickets.errors(value, t)).toEqual({
      base_url: t("setup.branvil.baseUrlInvalid"),
      owner: t("setup.branvil.ownerInvalid"),
      board: t("setup.branvil.boardInvalid"),
    });
    expect(branvilCodeHosting.errors({ ...value, owner: " " }, t)).toEqual({
      base_url: t("setup.branvil.baseUrlInvalid"),
      owner: t("setup.branvil.ownerRequired"),
    });
    expect(
      branvilTickets.errors({ name: "branvil", owner: "acme/team", board: "acme/board" }, t),
    ).toEqual({});
  });
});
