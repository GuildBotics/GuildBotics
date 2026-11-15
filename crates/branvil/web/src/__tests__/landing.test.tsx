import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Landing, LandingNext, Repository } from "../api/types";
import { dictionaries } from "../i18n";
import { fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { BASE, HEAD, landing, pull } from "./fixtures";

const P = "/api/v1/repos/acme/demo/pulls/3";
const REPO: Repository = {
  path: "acme/demo", default_branch: "main", clone_url: "", url: "", protected_branches: ["main"],
  responder: { member: "aiko", chosen: false, automatic: "aiko" }, post_merge_jobs: [], required_checks: ["test"], required_approvals: 1, auto_land: false, ci_network: false, labels: [], revision: 1,
  upstream: { provider: "github", host: "GitHub", repository: "example/demo", url: "https://github.com/example/demo", reader: { member: "agent", chosen: false }, following: { tip: BASE, checked_at: null, followed_at: null, error: null, stopped: null } },
};
const PREVIEW = { host: "GitHub", repository: "example/demo", head: "topic", base: "main", title: "Change", body: "", account: "agent", login: "agent-bot" };
function routes(l: Landing | null, extra: Parameters<typeof fakeApi>[0] = {}) {
  const p = pull({ landing: l, author: "agent", approvals: 1 });
  return {
    "GET /api/v1/session": json(200, SESSION),
    "GET /api/v1/workspaces": json(200, { items: [{ name: "acme", lanes: [] }], next_cursor: null }),
    "GET /api/v1/admin/diagnostics": json(200, {}),
    "GET /api/v1/members": json(200, { items: [], next_cursor: null }),
    "GET /api/v1/repos": json(200, { items: [REPO], next_cursor: null }),
    "GET /api/v1/repos/acme/demo": json(200, REPO),
    "GET /api/v1/repos/acme/demo/pulls": json(200, { items: [p], next_cursor: null }),
    "GET /api/v1/repos/acme/demo/landing": json(200, { items: l ? [l] : [] }),
    "GET /api/v1/repos/acme/demo/branches/main": json(200, { name: "main", sha: BASE }),
    [`GET /api/v1/repos/acme/demo/commits/${BASE}/checks`]: json(200, { sha: BASE, checks: [], checks_expected: false }),
    [`GET ${P}`]: json(200, p),
    [`GET ${P}/checks`]: json(200, { head_sha: HEAD, base_sha: BASE, behind_by: 2, checks: [{ name: "test", status: "completed", conclusion: "success" }], checks_expected: true, base_checks: 1 }),
    [`GET ${P}/activity`]: json(200, { comments: [], reviews: [], threads: [] }),
    [`GET ${P}/threads`]: json(200, { items: [], next_cursor: null }),
    [`GET ${P}/publish`]: json(200, PREVIEW),
    ...extra,
  };
}

describe.each(["en", "ja"] as const)("independent landing (%s)", lang => {
  const words = dictionaries[lang];
  it.each(["publishing", "owner_publish", "host_checks", "owner_merge_host", "following", "owner_fix_following", "host_closed", "owner_fix_auto", "auto_merge", "owner_merge"] as Exclude<LandingNext, "conditions">[])("shows %s without a queue or checkpoint", async next => {
    fakeApi(routes(landing({ next })));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", { name: words["landing.journey"].replace("{base}", "main") });
    expect(journey.querySelector('[data-stop="queue"]')).toBeNull();
    expect(journey.querySelector('[data-stop="gate"]')).toBeNull();
    expect(within(journey).getByText(words[`landing.next.${next}`].replaceAll("{host}", "GitHub").replaceAll("{base}", "main"))).toBeInTheDocument();
  });
  it("renders the server's condition messages", async () => {
    fakeApi(routes(landing({ next: "conditions", blockers: [{ code: "conflict", message: "Merge main into topic and resolve README.md", files: ["README.md"], jobs: [] }] })));
    renderApp("/acme/demo/-/pulls/3", lang);
    expect(await screen.findByText("Merge main into topic and resolve README.md")).toBeInTheDocument();
  });
  it("offers direct publication when sending stopped", async () => {
    const l = landing({ next: "owner_publish" });
    const p = pull({ landing: l, approvals: 1, upstream: { provider: "github", repository: "example/demo", account: "agent", number: 4, url: "https://host.test/pull/4", state: "open", pushed: HEAD, published_at: "now", stopped: "host refused", error: null, auto: false } });
    fakeApi(routes(l, { [`GET ${P}`]: json(200,p) }));
    renderApp("/acme/demo/-/pulls/3",lang);
    const button = await screen.findByRole("button",{ name: words["publish.again"].replaceAll("{host}","GitHub") });
    expect(button).toBeEnabled();
    expect(screen.queryByRole("button",{ name: words["landing.resume"] })).not.toBeInTheDocument();
  });

});
