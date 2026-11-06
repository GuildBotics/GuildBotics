import { describe, expect, it } from "vitest";

import i18n from "../../i18n";
import "../../i18n";
import { branvilAccount, type BranvilForm } from "./branvil/BranvilAccount";
import { branvilCodeHosting, branvilTickets } from "./branvil/BranvilServices";
import { isSlackChannel, slackAccount, type SlackForm } from "./slack/SlackAccount";
import type { MemberContext } from "./types";

const t = i18n.getFixedT("en");

function context(overrides: Partial<MemberContext> = {}): MemberContext {
  return {
    human: false,
    personId: "my-bot",
    savedPersonId: "",
    memberKey: "new",
    chatChannels: [],
    chatChannelsField: null,
    services: {},
    ...overrides,
  };
}

function slack(overrides: Partial<SlackForm> = {}): SlackForm {
  return { ...slackAccount.initial(), ...overrides };
}

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
  it("sends the agent saved, chosen or to be created, and a human's saved name", () => {
    expect(branvilAccount.request(branvil(), false)).toEqual({ username: "", reissue: false });
    expect(branvilAccount.request(branvil({ choice: "existing", chosen: "aiko" }), false)).toEqual({
      username: "aiko",
      reissue: false,
    });
    expect(
      branvilAccount.request(
        branvil({ stored: "aiko", choice: "existing", chosen: "other", reissue: true }),
        false,
      ),
    ).toEqual({ username: "aiko", reissue: true });
    expect(branvilAccount.request(branvil({ stored: "otota" }), true)).toEqual({
      username: "otota",
    });
  });

  it("edits a stored account with its token kept", () => {
    expect(
      branvilAccount.fromView({ username: "aiko", has_token: true }, branvil(), false),
    ).toEqual(branvil({ stored: "aiko", storedToken: true }));
    expect(branvilAccount.fromView(undefined, branvil(), false)).toEqual(branvil());
  });

  it("asks an agent to be chosen only when choosing one", () => {
    expect(branvilAccount.errors(branvil(), context(), t)).toEqual({});
    expect(branvilAccount.errors(branvil({ choice: "existing" }), context(), t)).toEqual({
      chosen: t("setup.branvil.member.chooseRequired"),
    });
    expect(
      branvilAccount.errors(branvil({ choice: "existing" }), context({ human: true }), t),
    ).toEqual({});
  });
});

describe("branvil services", () => {
  it("start from the workspace the other kind names", () => {
    expect(branvilTickets.initial({})).toEqual({ workspace: "" });
    expect(
      branvilCodeHosting.initial({ ticket_service: { name: "branvil", workspace: "acme" } }),
    ).toEqual({ workspace: "acme" });
  });

  it("refuse what names no workspace", () => {
    const value = { name: "branvil", workspace: "acme/team" };

    expect(branvilTickets.errors(value, t)).toEqual({
      workspace: t("setup.branvil.workspaceInvalid"),
    });
    expect(branvilCodeHosting.errors({ ...value, workspace: " " }, t)).toEqual({
      workspace: t("setup.branvil.workspaceRequired"),
    });
    expect(branvilTickets.errors({ name: "branvil", workspace: "acme" }, t)).toEqual({});
  });
});
