import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { StrictMode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { getConfigStatus } from "../api/client";
import i18n from "../i18n";
import "../i18n";
import { TestMantineProvider } from "../test/TestMantineProvider";
import { EntryPage } from "./EntryPage";
import { leavePage, replacePage } from "./pageNavigation";

vi.mock("./pageNavigation", () => ({ leavePage: vi.fn(), replacePage: vi.fn() }));

const t = i18n.getFixedT("en");

type Answer = { status: number; body: unknown } | Error;
type Call = { method: string; url: string; headers: Record<string, string>; body: unknown };

/** The server as `fetch` meets it: "METHOD /api/path" to an answer, recording the calls. */
function serve(routes: Record<string, Answer | (() => Answer)>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: RequestInit) => {
      const method = init.method ?? "GET";
      const body = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
      calls.push({ method, url, headers: init.headers as Record<string, string>, body });
      const route = routes[`${method} ${url}`];
      const answer = typeof route === "function" ? route() : route;
      if (!answer) {
        throw new Error(`unexpected ${method} ${url}`);
      }
      if (answer instanceof Error) {
        throw answer;
      }
      return {
        ok: answer.status < 400,
        status: answer.status,
        json: async () => answer.body,
      } as Response;
    }),
  );
  return calls;
}

const ok = (body: unknown): Answer => ({ status: 200, body });
const refused = (status: number, code: string): Answer => ({
  status,
  body: { code, message: `refused: ${code}`, context: {} },
});
const SESSION = ok({ csrf: "csrf-1" });
const NO_SESSION = refused(401, "unauthorized");
const OFFLINE = new TypeError("Failed to fetch");

/** A screen that reads the server, so a test can watch it fail and come back. */
function Screen() {
  const config = useQuery({ queryKey: ["config"], queryFn: getConfigStatus, retry: false });
  return <div>Screen read {config.dataUpdatedAt ? config.data?.cwd : "nothing"}</div>;
}

function renderPage(path: string) {
  window.history.replaceState(null, "", path);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <StrictMode>
      <TestMantineProvider>
        <QueryClientProvider client={queryClient}>
          <EntryPage>
            <Screen />
          </EntryPage>
        </QueryClientProvider>
      </TestMantineProvider>
    </StrictMode>,
  );
}

const CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM";
const REDIRECT = "http://127.0.0.1:53682/callback";
const REQUEST = {
  client: "GuildBotics CLI laptop",
  redirect_uri: REDIRECT,
  state: "s&1",
  code_challenge: CHALLENGE,
};
const AUTHORIZE = `/-/authorize?${new URLSearchParams({ ...REQUEST, code_challenge_method: "S256" })}`;

beforeEach(() => {
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState(null, "", "/");
});

describe("a page the server serves", () => {
  it("shows the screen once the session is taken up", async () => {
    serve({ "GET /api/session": SESSION, "GET /api/config/status": ok({ cwd: "/ws" }) });

    renderPage("/");

    expect(await screen.findByText("Screen read /ws")).toBeInTheDocument();
  });

  it("says how to enter, on the server machine or from another, without a session", async () => {
    serve({ "GET /api/session": NO_SESSION });

    renderPage("/");

    expect(await screen.findByText(t("session.guide.title"))).toBeInTheDocument();
    expect(screen.getByText("guildbotics open")).toBeInTheDocument();
    expect(screen.getByText("ssh <server> guildbotics open --print")).toBeInTheDocument();
    expect(screen.queryByText(/Screen read/)).not.toBeInTheDocument();
  });

  it("says how to enter again once a request finds the session gone", async () => {
    let config: Answer = ok({ cwd: "/ws" });
    serve({ "GET /api/session": SESSION, "GET /api/config/status": () => config });
    const view = renderPage("/");
    await screen.findByText("Screen read /ws");

    config = NO_SESSION;
    void getConfigStatus().catch(() => undefined);

    expect(await screen.findByText(t("session.guide.title"))).toBeInTheDocument();
    expect(view.queryByText(/Screen read/)).not.toBeInTheDocument();
  });

  it("keeps the screen under a notice while the server is unreachable, and reloads it on retry", async () => {
    let config: Answer = ok({ cwd: "/ws" });
    let session: Answer = SESSION;
    const calls = serve({
      "GET /api/session": () => session,
      "GET /api/config/status": () => config,
    });
    const user = userEvent.setup();
    renderPage("/");
    await screen.findByText("Screen read /ws");

    config = OFFLINE;
    session = OFFLINE;
    void getConfigStatus().catch(() => undefined);

    const notice = await screen.findByRole("dialog", { name: t("session.unreachable.title") });
    expect(screen.getByText("Screen read /ws")).toBeInTheDocument();
    const sessionReads = () => calls.filter((call) => call.url === "/api/session").length;
    const before = sessionReads();
    await user.click(within(notice).getByRole("button", { name: t("session.retry") }));
    await waitFor(() => expect(sessionReads()).toBeGreaterThan(before));
    expect(screen.getByRole("dialog")).toBeInTheDocument();

    config = ok({ cwd: "/ws2" });
    session = SESSION;
    await user.click(within(notice).getByRole("button", { name: t("session.retry") }));

    expect(await screen.findByText("Screen read /ws2")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("shows the notice alone when the session cannot be read at first", async () => {
    serve({ "GET /api/session": OFFLINE });

    renderPage("/");

    expect(
      await screen.findByRole("dialog", { name: t("session.unreachable.title") }),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Screen read/)).not.toBeInTheDocument();
  });
});

describe("the entry page", () => {
  it("enters once with the link's code, forgets the code, and opens next", async () => {
    const calls = serve({ "POST /api/session": SESSION });

    renderPage("/-/enter?next=/quick.html#code-1");

    await waitFor(() => expect(replacePage).toHaveBeenCalledWith("/quick.html"));
    // StrictMode runs the effect twice; the code works a single time.
    expect(calls).toEqual([
      expect.objectContaining({ method: "POST", url: "/api/session", body: { code: "code-1" } }),
    ]);
    expect(window.location.hash).toBe("");
    expect(window.location.pathname + window.location.search).toBe("/-/enter");
  });

  it("opens the App when no page, or a page of another site, is named", async () => {
    serve({ "POST /api/session": SESSION });

    renderPage("/-/enter?next=//attacker.example/#code-1");

    await waitFor(() => expect(replacePage).toHaveBeenCalledWith("/"));
  });

  it("says how to get a new link when the code was used or expired", async () => {
    serve({ "POST /api/session": NO_SESSION });

    renderPage("/-/enter#code-1");

    expect(await screen.findByText(t("session.guide.expired"))).toBeInTheDocument();
    expect(screen.getByText("guildbotics open")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("session.retry") })).not.toBeInTheDocument();
    expect(replacePage).not.toHaveBeenCalled();
  });

  it("tries the link again after a failure that leaves it possibly good", async () => {
    let answer: Answer = OFFLINE;
    const calls = serve({ "POST /api/session": () => answer });
    const user = userEvent.setup();

    renderPage("/-/enter#code-1");

    expect(await screen.findByText("Failed to fetch")).toBeInTheDocument();
    answer = SESSION;
    await user.click(screen.getByRole("button", { name: t("session.retry") }));
    await waitFor(() => expect(replacePage).toHaveBeenCalledWith("/"));
    expect(calls).toHaveLength(2);
  });

  it("without a code only says how to enter", async () => {
    const calls = serve({});

    renderPage("/-/enter");

    expect(await screen.findByText(t("session.guide.title"))).toBeInTheDocument();
    expect(calls).toEqual([]);
  });
});

describe("the consent page", () => {
  it("refuses an invalid request without offering anything", async () => {
    const calls = serve({ "GET /api/session": SESSION });

    renderPage(AUTHORIZE.replace("127.0.0.1", "attacker.example"));

    expect(await screen.findByText(/redirect_uri/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("session.authorize.allow") })).toBeNull();
    expect(screen.queryByRole("button", { name: t("session.authorize.deny") })).toBeNull();
    expect(calls).toEqual([]);
  });

  it("allows the app with the session's CSRF value and goes to its address", async () => {
    const redirect = `${REDIRECT}?code=c-1&state=s%261`;
    const calls = serve({
      "GET /api/session": SESSION,
      "POST /api/apps/authorizations": { status: 201, body: { redirect } },
    });
    const user = userEvent.setup();

    renderPage(AUTHORIZE);

    const title = t("session.authorize.title", { client: REQUEST.client });
    expect(await screen.findByRole("heading", { name: title })).toBeInTheDocument();
    expect(
      screen.getByText(t("session.authorize.returns", { address: "http://127.0.0.1:53682" })),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("session.authorize.allow") }));

    await waitFor(() => expect(leavePage).toHaveBeenCalledWith(redirect));
    const sent = calls.find((call) => call.url === "/api/apps/authorizations");
    expect(sent?.body).toEqual(REQUEST);
    expect(sent?.headers["X-GuildBotics-CSRF"]).toBe("csrf-1");
    expect(localStorage.length).toBe(0);
  });

  it("denies by going to the app's address with access_denied, without asking the server", async () => {
    const calls = serve({ "GET /api/session": SESSION });
    const user = userEvent.setup();

    renderPage(AUTHORIZE);
    await user.click(await screen.findByRole("button", { name: t("session.authorize.deny") }));

    expect(leavePage).toHaveBeenCalledWith(`${REDIRECT}?error=access_denied&state=s%261`);
    expect(calls.map((call) => call.url)).toEqual(["/api/session"]);
  });

  it("stays, showing the refusal, when the server refuses", async () => {
    serve({
      "GET /api/session": SESSION,
      "POST /api/apps/authorizations": refused(400, "invalid"),
    });
    const user = userEvent.setup();

    renderPage(AUTHORIZE);
    await user.click(await screen.findByRole("button", { name: t("session.authorize.allow") }));

    expect(await screen.findByText("refused: invalid")).toBeInTheDocument();
    expect(leavePage).not.toHaveBeenCalled();
  });

  it("without a session says how to enter, and entering returns here", async () => {
    serve({ "GET /api/session": NO_SESSION, "POST /api/session": SESSION });

    const first = renderPage(AUTHORIZE);

    expect(
      await screen.findByText(t("session.authorize.enterFirst", { client: REQUEST.client })),
    ).toBeInTheDocument();
    expect(screen.getByText("guildbotics open")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("session.authorize.allow") })).toBeNull();

    first.unmount();
    renderPage("/-/enter#code-1");

    await waitFor(() => expect(replacePage).toHaveBeenCalledWith(AUTHORIZE));
  });
});
