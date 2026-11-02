import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { leave } from "../router/Router";
import { parseRequest } from "../views/AuthorizeView";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";

vi.mock("../router/Router", async (original) => ({
  ...(await original<typeof import("../router/Router")>()),
  leave: vi.fn(),
}));

afterEach(() => {
  vi.mocked(leave).mockClear();
});

const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
const REDIRECT = "http://127.0.0.1:53682/callback";
const PARAMS = {
  client: "Desk App",
  redirect_uri: REDIRECT,
  state: "s&1",
  code_challenge: CHALLENGE,
  code_challenge_method: "S256",
};
const authorizePath = (params: Record<string, string> = PARAMS) =>
  `/-/authorize?${new URLSearchParams(params).toString()}`;
const noSession = apiError(401, "unauthorized", "No valid token or session.");

describe("the consent page of a local app", () => {
  it("accepts only a loopback app's S256 request", () => {
    expect(parseRequest(new URLSearchParams(PARAMS).toString())).toEqual({
      client: "Desk App",
      redirect_uri: REDIRECT,
      state: "s&1",
      code_challenge: CHALLENGE,
    });
    const invalid = (change: Record<string, string>) => {
      const result = parseRequest(new URLSearchParams({ ...PARAMS, ...change }).toString());
      return "invalid" in result ? result.invalid : [];
    };
    for (const redirect of [
      "https://127.0.0.1/cb",
      "http://attacker.example/cb",
      "http://localhost.attacker.example/cb",
      "http://me@127.0.0.1/cb",
      "http://127.0.0.1/cb#x",
      "/cb",
    ]) {
      expect(invalid({ redirect_uri: redirect }), redirect).toEqual(["redirect_uri"]);
    }
    expect(invalid({ redirect_uri: "http://localhost/cb" })).toEqual([]);
    expect(invalid({ redirect_uri: "http://[::1]:8080/cb" })).toEqual([]);
    expect(invalid({ client: "a/b" })).toEqual(["client"]);
    expect(invalid({ client: " " })).toEqual(["client"]);
    expect(invalid({ state: "" })).toEqual(["state"]);
    expect(invalid({ code_challenge: "plain" })).toEqual(["code_challenge"]);
    expect(invalid({ code_challenge_method: "plain" })).toEqual(["code_challenge_method"]);
  });

  it("refuses an invalid request without offering to allow or redirecting", async () => {
    const fake = fakeApi({ "GET /api/v1/session": json(200, SESSION) });
    renderApp(authorizePath({ ...PARAMS, redirect_uri: "http://attacker.example/cb" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("redirect_uri");
    expect(screen.queryByRole("button", { name: "Allow" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deny" })).not.toBeInTheDocument();
    expect(fake.calls("POST", "/api/v1/apps/authorizations")).toHaveLength(0);
    expect(leave).not.toHaveBeenCalled();
  });

  it("allows the app with the session's CSRF value and goes to the redirect", async () => {
    const redirect = `${REDIRECT}?code=c-1&state=s%261`;
    const fake = fakeApi({
      "GET /api/v1/session": json(200, SESSION),
      "POST /api/v1/apps/authorizations": json(201, { redirect }),
    });
    const user = userEvent.setup();
    renderApp(authorizePath());
    expect(await screen.findByRole("heading", { name: "Allow Desk App to manage Branvil?" })).toBeInTheDocument();
    expect(screen.getByText(/Settings → Members → owner → Tokens/)).toHaveTextContent("app:Desk App");
    expect(screen.getByText(/you return to http:\/\/127\.0\.0\.1:53682/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Allow" }));
    await waitFor(() => expect(leave).toHaveBeenCalledWith(redirect));
    const [sent] = fake.calls("POST", "/api/v1/apps/authorizations");
    expect(sent?.body).toEqual({ client: "Desk App", redirect_uri: REDIRECT, state: "s&1", code_challenge: CHALLENGE });
    expect(sent?.headers["X-Branvil-CSRF"]).toBe("csrf-123");
    // A session in hand: nothing is remembered for after entering.
    expect(window.localStorage.getItem("branvil.return")).toBeNull();
  });

  it("denies by going to the redirect with access_denied and the state, without asking the service", async () => {
    const fake = fakeApi({ "GET /api/v1/session": json(200, SESSION) });
    const user = userEvent.setup();
    renderApp(authorizePath());
    await user.click(await screen.findByRole("button", { name: "Deny" }));
    expect(leave).toHaveBeenCalledWith(`${REDIRECT}?error=access_denied&state=s%261`);
    expect(fake.calls("POST", "/api/v1/apps/authorizations")).toHaveLength(0);
  });

  it("shows a refusal and stays when the service refuses", async () => {
    fakeApi({
      "GET /api/v1/session": json(200, SESSION),
      "POST /api/v1/apps/authorizations": apiError(400, "invalid", "state must be 1-512 bytes."),
    });
    const user = userEvent.setup();
    renderApp(authorizePath());
    await user.click(await screen.findByRole("button", { name: "Allow" }));
    expect(await screen.findByText("state must be 1-512 bytes.")).toBeInTheDocument();
    expect(leave).not.toHaveBeenCalled();
  });

  it("without a session says how to enter and returns here after entering", async () => {
    const fake = fakeApi({
      "GET /api/v1/session": noSession,
      "POST /api/v1/session": json(200, SESSION),
    });
    const first = renderApp(authorizePath());
    expect(await screen.findByText(/To allow Desk App, open Branvil first/)).toBeInTheDocument();
    expect(screen.getByText("branvil open")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Allow" })).not.toBeInTheDocument();
    expect(fake.calls("POST", "/api/v1/apps/authorizations")).toHaveLength(0);

    // `branvil open`'s link, opened in the same browser, comes back here once.
    first.unmount();
    renderApp("/-/enter#code-1");
    expect(await screen.findByRole("heading", { name: "Allow Desk App to manage Branvil?" })).toBeInTheDocument();
    expect(window.location.pathname + window.location.search).toBe(authorizePath());
    expect(window.localStorage.getItem("branvil.return")).toBeNull();
  });

  it("forgets the page after 10 minutes, so entering opens the board", async () => {
    fakeApi({
      "GET /api/v1/session": noSession,
      "POST /api/v1/session": json(200, SESSION),
      "GET /api/v1/workspaces": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/repos": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/admin/diagnostics": json(200, {}),
    });
    const first = renderApp(authorizePath());
    await screen.findByText(/To allow Desk App, open Branvil first/);
    expect(window.localStorage.getItem("branvil.return")).not.toBeNull();
    first.unmount();
    const now = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(now + 10 * 60_000 + 1);
    renderApp("/-/enter#code-2");
    expect(await screen.findByRole("heading", { name: "No workspace yet" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
    vi.mocked(Date.now).mockRestore();
  });

  it("returns only to the consent page", async () => {
    window.localStorage.setItem("branvil.return", JSON.stringify({ path: "//attacker.example/", expires: Date.now() + 60_000 }));
    fakeApi({
      "POST /api/v1/session": json(200, SESSION),
      "GET /api/v1/workspaces": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/repos": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/admin/diagnostics": json(200, {}),
    });
    renderApp("/-/enter#code-3");
    expect(await screen.findByRole("heading", { name: "No workspace yet" })).toBeInTheDocument();
    expect(window.location.pathname).toBe("/");
  });
});
