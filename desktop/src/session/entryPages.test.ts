import { afterEach, describe, expect, it, vi } from "vitest";

import {
  deniedRedirect,
  parseAppRequest,
  rememberReturn,
  samePagePath,
  takeReturn,
} from "./entryPages";

const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
const REDIRECT = "http://127.0.0.1:53682/callback";
const PARAMS = {
  client: "GuildBotics CLI laptop",
  redirect_uri: REDIRECT,
  state: "s&1",
  code_challenge: CHALLENGE,
  code_challenge_method: "S256",
};

function invalid(change: Record<string, string>): string[] {
  const result = parseAppRequest(new URLSearchParams({ ...PARAMS, ...change }).toString());
  return "invalid" in result ? result.invalid : [];
}

afterEach(() => {
  localStorage.clear();
  window.history.replaceState(null, "", "/");
  vi.restoreAllMocks();
});

describe("parseAppRequest", () => {
  it("accepts a loopback app's S256 request", () => {
    expect(parseAppRequest(new URLSearchParams(PARAMS).toString())).toEqual({
      client: "GuildBotics CLI laptop",
      redirect_uri: REDIRECT,
      state: "s&1",
      code_challenge: CHALLENGE,
    });
    expect(invalid({ redirect_uri: "http://localhost/cb" })).toEqual([]);
    expect(invalid({ redirect_uri: "http://[::1]:8080/cb" })).toEqual([]);
    expect(invalid({ state: "s".repeat(512) })).toEqual([]);
  });

  it.each([
    "https://127.0.0.1/cb",
    "http://attacker.example/cb",
    "http://localhost.attacker.example/cb",
    "http://me@127.0.0.1/cb",
    "http://me:pw@127.0.0.1/cb",
    "http://127.0.0.1/cb#x",
    "/cb",
  ])("refuses the redirect %s", (redirect) => {
    expect(invalid({ redirect_uri: redirect })).toEqual(["redirect_uri"]);
  });

  it("refuses a client name, state, challenge or method the server refuses", () => {
    expect(invalid({ client: "a/b" })).toEqual(["client"]);
    expect(invalid({ client: " " })).toEqual(["client"]);
    expect(invalid({ client: "c".repeat(65) })).toEqual(["client"]);
    expect(invalid({ state: "" })).toEqual(["state"]);
    // 512 bytes, counted in UTF-8: 171 three-byte characters are 513.
    expect(invalid({ state: "あ".repeat(171) })).toEqual(["state"]);
    expect(invalid({ code_challenge: "plain" })).toEqual(["code_challenge"]);
    expect(invalid({ code_challenge: `${CHALLENGE}=` })).toEqual(["code_challenge"]);
    expect(invalid({ code_challenge_method: "plain" })).toEqual(["code_challenge_method"]);
    expect(parseAppRequest("")).toEqual({
      invalid: ["client", "redirect_uri", "state", "code_challenge", "code_challenge_method"],
    });
  });

  it("denies with access_denied and the state added to the app's address", () => {
    const request = parseAppRequest(
      new URLSearchParams({ ...PARAMS, redirect_uri: `${REDIRECT}?keep=1` }).toString(),
    );
    expect("invalid" in request).toBe(false);
    expect(deniedRedirect(request as Exclude<typeof request, { invalid: string[] }>)).toBe(
      `${REDIRECT}?keep=1&error=access_denied&state=s%261`,
    );
  });
});

describe("samePagePath", () => {
  it("keeps only a page of this server", () => {
    expect(samePagePath("/quick.html")).toBe("/quick.html");
    expect(samePagePath("/-/authorize?client=a%20b")).toBe("/-/authorize?client=a%20b");
    expect(samePagePath(null)).toBeNull();
    expect(samePagePath("")).toBeNull();
    expect(samePagePath("//attacker.example/")).toBeNull();
    expect(samePagePath("/\\attacker.example/")).toBeNull();
    expect(samePagePath("https://attacker.example/")).toBeNull();
  });

  it("refuses what the server refuses, and what a browser drops from an address", () => {
    // `next` arrives decoded from the query; a browser strips tabs, newlines
    // and other control characters, which would leave `//attacker.example`.
    for (const control of ["\t", "\n", "\r", "\x00", "\x1f", "\x7f"]) {
      expect(samePagePath(`/${control}/attacker.example`)).toBeNull();
    }
    expect(samePagePath(new URLSearchParams("next=%09/attacker.example").get("next"))).toBeNull();
    expect(samePagePath("/ /attacker.example")).toBeNull();
    expect(samePagePath("/#/service")).toBeNull();
    expect(samePagePath("quick.html")).toBeNull();
  });
});

describe("the remembered consent page", () => {
  const page = `/-/authorize?${new URLSearchParams(PARAMS)}`;

  it("is returned to once", () => {
    window.history.replaceState(null, "", page);
    rememberReturn();

    expect(takeReturn()).toBe(page);
    expect(takeReturn()).toBeNull();
  });

  it("is forgotten after 10 minutes", () => {
    window.history.replaceState(null, "", page);
    rememberReturn();
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now + 10 * 60_000 + 1);

    expect(takeReturn()).toBeNull();
  });

  it("is only ever the consent page", () => {
    window.history.replaceState(null, "", "/elsewhere");
    rememberReturn();

    expect(takeReturn()).toBeNull();
  });
});
