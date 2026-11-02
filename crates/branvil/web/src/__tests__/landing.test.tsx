import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Landing, LandingNext, PullRequest, Repository } from "../api/types";
import { dictionaries } from "../i18n";
import { applyChange } from "../query/events";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { BASE, HEAD, landing, pull } from "./fixtures";

const P = "/api/v1/repos/acme/demo/pulls/3";
const REPO: Repository = {
  path: "acme/demo", default_branch: "main", clone_url: "", url: "", protected_branches: ["main"],
  required_checks: ["test"], required_approvals: 1, auto_land: false, ci_network: false, labels: [], revision: 1,
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

const STOP: Record<LandingNext, string> = {
  gate: "gate", publishing: "host", owner_publish: "host", host_checks: "host", host_closed: "host", owner_fix_auto: "host",
  owner_merge_host: "merge", auto_merge: "merge", owner_merge: "merge", following: "base", owner_fix_following: "base",
};
const stops = (journey: HTMLElement, state?: string) => [...journey.querySelectorAll<HTMLElement>(state ? `[data-stop][data-state="${state}"]` : "[data-stop]")].map((e) => e.dataset.stop);
const badges = (journey: HTMLElement) => [...journey.querySelectorAll(".mantine-Badge-root")].map((e) => e.textContent);
const checksLoaded = async () => within(await screen.findByRole("region", { name: "Checks" })).findByTitle(HEAD);
const NEXT: LandingNext[] = ["gate", "publishing", "owner_publish", "host_checks", "owner_merge_host", "following", "owner_fix_following", "host_closed", "owner_fix_auto", "auto_merge", "owner_merge"];
const entry = (next: LandingNext, extra: Partial<Landing> = {}) => landing({ first: { ...landing().first!, next, next_detail: "Diagnostic detail" }, ...extra });

describe.each(["en", "ja"] as const)("queue path (%s)", (lang) => {
  const words = dictionaries[lang];
  const journeyName = () => words["landing.journey"].replace("{base}", "main");
  const label = (next: LandingNext, base = "main") => words[`landing.next.${next}`].replaceAll("{host}", "GitHub").replaceAll("{base}", base);
  const listBadge = async () => {
    const list = await screen.findByRole("table", { name: words["pulls.title"] });
    await waitFor(() => expect(list.querySelector("[data-state]")).toBeInTheDocument());
    return list.querySelector("[data-state]") as HTMLElement;
  };
  const tone = (badge: HTMLElement, color: string) => expect(badge).toHaveStyle(`--badge-color: var(--mantine-color-${color}-light-color)`);
  it.each(NEXT)("says %s in the list, with the next step in its title", async (next) => {
    fakeApi(routes(entry(next)));
    renderApp("/acme/demo", lang);
    const badge = await listBadge();
    await waitFor(() => expect(badge).toHaveTextContent(words[`landing.badge.${next}`].replaceAll("{host}", "GitHub").replaceAll("{base}", "main")));
    expect(badge).toHaveAttribute("title", label(next));
    expect(badge).toHaveAttribute("data-stop", STOP[next]);
    tone(badge, next.startsWith("owner_") || next === "host_closed" ? "warning" : "neutral");
  });
  it("names a base other than the default branch", async () => {
    const l = entry("owner_merge", { base: "release" });
    fakeApi(routes(l, {
      "GET /api/v1/repos": json(200, { items: [{ ...REPO, upstream: null }], next_cursor: null }),
      "GET /api/v1/repos/acme/demo": json(200, { ...REPO, upstream: null }),
      "GET /api/v1/repos/acme/demo/pulls": json(200, { items: [pull({ base: "release", landing: l, approvals: 1 })], next_cursor: null }),
    }));
    renderApp("/acme/demo", lang);
    expect(await listBadge()).toHaveTextContent(`release · ${words["landing.badge.owner_merge"].replace("{base}", "release")}`);
  });
  it("says a waiting pull request's place and who goes first", async () => {
    fakeApi(routes(landing({ state: "waiting", position: 3, first: { ...landing().first!, number: 1 } })));
    renderApp("/acme/demo", lang);
    const badge = await listBadge();
    expect(badge).toHaveTextContent(words["landing.position"].replace("{position}", "3"));
    expect(badge).toHaveAttribute("title", words["landing.waitFor"].replace("{first}", "1"));
  });
  it("names what keeps a pull request out of the queue", async () => {
    const p = pull({ landing: null, approvals: 0, draft: true });
    fakeApi(routes(null, { "GET /api/v1/repos/acme/demo/pulls": json(200, { items: [p], next_cursor: null }), [`GET ${P}`]: json(200, p) }));
    renderApp("/acme/demo", lang);
    const badge = await listBadge();
    const approvals = words["merge.approvals"].replace("{count}", "0").replace("{required}", "1");
    await waitFor(() => expect(badge).toHaveAttribute("title", `${words["pull.draft"]} / ${approvals}`));
    expect(badge).toHaveTextContent(words["landing.blockedBy"].replace("{what}", `${words["pull.draft"]} +1`));
    tone(badge, "warning");
  });
  it("is plainly not queued when nothing visible is missing", async () => {
    fakeApi(routes(null));
    renderApp("/acme/demo", lang);
    const badge = await listBadge();
    await waitFor(() => expect(badge).toHaveTextContent(words["landing.notInQueue"]));
    expect(badge).toHaveAttribute("title", words["landing.headReady"]);
    tone(badge, "neutral");
  });
  it.each([["host_failed", "GitHub"], ["gate_failed", words["landing.checkpoint"]], ["approvals", words["landing.stop.conditions"]]] as const)("says where it left the queue (%s)", async (code, stop) => {
    fakeApi(routes(landing({ state: "left", first: null, position: null, reason: { code, message: "Service English", jobs: [], files: [] } })));
    renderApp("/acme/demo", lang);
    const badge = await listBadge();
    expect(badge).toHaveTextContent(words["landing.leftAt"].replace("{stop}", stop));
    expect(badge).toHaveAttribute("title", words[`landing.reason.${code}`]);
    tone(badge, "danger");
  });
  it("reads no checks for an open row past the budget, and names only what it knows", async () => {
    const rows = Array.from({ length: 31 }, (_, i) => pull({ number: 31 - i, label: `!${31 - i}`, url: `${window.location.origin}/acme/demo/-/pulls/${31 - i}`, landing: null, approvals: 1 }));
    const fake = fakeApi(routes(null, {
      "GET /api/v1/repos/acme/demo/pulls": json(200, { items: rows, next_cursor: null }),
      ...Object.fromEntries(rows.map((p) => [`GET /api/v1/repos/acme/demo/pulls/${p.number}/checks`, json(200, { head_sha: HEAD, checks: [{ name: "test", status: "completed", conclusion: "failure" }] })])),
    }));
    renderApp("/acme/demo", lang);
    const list = await screen.findByRole("table", { name: words["pulls.title"] });
    await userEvent.setup().click(await screen.findByRole("button", { name: words["pulls.loadMore"] }));
    const last = await within(list).findByRole("row", { name: /acme\/demo !1 Fix login$/ });
    await waitFor(() => expect(fake.calls("GET", "/api/v1/repos/acme/demo/pulls/2/checks")).toHaveLength(1));
    expect(last.querySelector("[data-state]")).toHaveTextContent(words["landing.notInQueue"]);
    expect(fake.calls("GET", "/api/v1/repos/acme/demo/pulls/1/checks")).toHaveLength(0);
  });
  it("shows a closed pull request's state instead of its way", async () => {
    const p = pull({ state: "closed", landing: landing({ state: "left", first: null, position: null, reason: { code: "closed", message: "", jobs: [], files: [] } }) });
    fakeApi(routes(null, { "GET /api/v1/repos/acme/demo/pulls": json(200, { items: [p], next_cursor: null }) }));
    renderApp("/acme/demo", lang);
    const list = await screen.findByRole("table", { name: words["pulls.title"] });
    await within(list).findByRole("row", { name: /Fix login/ });
    expect(list.querySelector("[data-state]")).not.toBeInTheDocument();
  });
  it.each(NEXT)("places this pull request at the stop %s names and links only host actions", async (next) => {
    fakeApi(routes(entry(next)));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(label(next))).toBeInTheDocument();
    expect(within(journey).getByText("Diagnostic detail")).toBeInTheDocument();
    const stop = journey.querySelector('[aria-current="step"]') as HTMLElement;
    expect(stop).toHaveAttribute("data-stop", STOP[next]);
    expect(stop).toHaveAttribute("data-state", next.startsWith("owner_") || next === "host_closed" ? "turn" : "now");
    expect(stops(journey)).toEqual(["conditions", "queue", "gate", "host", "merge", "base"]);
    expect(stops(journey, "done")).toEqual(stops(journey).slice(0, stops(journey).indexOf(STOP[next])));
    const links = within(journey).queryAllByRole("link");
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["owner_merge_host", "host_closed"].includes(next) ? ["https://example.test/pulls/3"] : []);
  });
  it.each(["auto_merge", "owner_merge", "owner_fix_auto"] as const)("merges here without an upstream (%s)", async (next) => {
    fakeApi(routes(entry(next), { "GET /api/v1/repos/acme/demo": json(200, { ...REPO, upstream: null }) }));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    await waitFor(() => expect(journey.querySelector('[aria-current="step"]')).toHaveAttribute("data-stop", "merge"));
    expect(stops(journey)).toEqual(["conditions", "queue", "gate", "merge", "base"]);
  });
  it("names only what the head still needs to join the queue", async () => {
    fakeApi(routes(null, {
      "GET /api/v1/repos/acme/demo":json(200,{...REPO,required_checks:["ok","fail","run","absent"]}),
      [`GET ${P}`]: json(200, pull({ landing: null, approvals: 0, draft: true })),
      [`GET ${P}/checks`]:json(200,{head_sha:HEAD,checks:[{name:"ok",status:"completed",conclusion:"success"},{name:"fail",status:"completed",conclusion:"failure"},{name:"run",status:"queued"}]}),
    }));
    renderApp("/acme/demo/-/pulls/3",lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words["landing.missing"])).toBeInTheDocument();
    await within(journey).findByText(`fail · ${words["landing.check.failure"]}`);
    expect(badges(journey)).toEqual([words["pull.draft"], `fail · ${words["landing.check.failure"]}`, `run · ${words["landing.check.running"]}`, `absent · ${words["landing.check.missing"]}`, words["merge.approvals"].replace("{count}", "0").replace("{required}", "1")]);
    expect(journey.querySelector('[aria-current="step"]')).toHaveAttribute("data-state", "turn");
    expect(within(journey).getByText(words["landing.notQueued"])).toBeInTheDocument();
  });
  it("waits for CI without handing the turn to a person", async () => {
    fakeApi(routes(null, {[`GET ${P}/checks`]:json(200,{head_sha:HEAD,checks:[{name:"test",status:"queued"}]})}));
    renderApp("/acme/demo/-/pulls/3",lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(`test · ${words["landing.check.running"]}`)).toBeInTheDocument();
    expect(within(journey).getByText(words["landing.missing"])).toBeInTheDocument();
    expect(journey.querySelector('[aria-current="step"]')).toHaveAttribute("data-state", "now");
  });
  it("says what is in place when nothing visible is missing", async () => {
    fakeApi(routes(null));
    renderApp("/acme/demo/-/pulls/3",lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words["landing.notInQueue"])).toBeInTheDocument();
    expect(journey.querySelector('[aria-current="step"]')).toHaveAttribute("data-state", "now");
    expect(badges(journey)).toEqual([]);
    expect(within(journey).getByText(words["landing.headReady"])).toBeInTheDocument();
  });
  it("waits at its place in the queue", async () => {
    const leader = entry("host_checks", {number:1, first:{...entry("host_checks").first!,number:1}});
    fakeApi(routes(landing({state:"waiting", position:4, first:leader.first})));
    renderApp("/acme/demo/-/pulls/3",lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words["landing.waitingAt"].replace("{position}", "4"))).toBeInTheDocument();
    const stop = journey.querySelector('[aria-current="step"]') as HTMLElement;
    expect(stop).toHaveAttribute("data-stop", "queue");
    expect(stop).toHaveTextContent("4");
    expect(within(journey).getByText(words["landing.waitFor"].replace("{first}", "1"))).toBeInTheDocument();
  });
  it("counts the pull requests waiting behind the leader", async () => {
    const leader = entry("owner_merge_host");
    const items = [leader, ...[4,5].map((n) => landing({ number: n, state: "waiting", position: n-2, first: leader.first })), landing({ number: 6, state: "left", position: null, first: null }), landing({ number: 7, base: "release" })];
    fakeApi(routes(leader, { "GET /api/v1/repos/acme/demo/landing": json(200, { items }) }));
    renderApp("/acme/demo/-/pulls/3",lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words["landing.behind"].replace("{count}", "2"))).toBeInTheDocument();
  });
  it("keeps the recorded clock and its label when only the next action changes", async () => {
    vi.useFakeTimers({toFake:["Date","setInterval","clearInterval"]});
    vi.setSystemTime(new Date("2026-10-25T10:01:00Z"));
    let l=entry("owner_merge_host",{first:{...entry("owner_merge_host").first!,state_at:"2026-10-25T10:00:00Z"}});
    fakeApi(routes(l, {[`GET ${P}`]: () => json(200, pull({ landing: l, author: "agent", approvals: 1 }))}));
    const {client} = renderApp("/acme/demo/-/pulls/3",lang);
    const queue=await screen.findByRole("region",{name:journeyName()});
    const recorded = lang === "ja" ? "検問・公開状態の記録から" : "Since the recorded gate/publishing update:";
    expect(await within(queue).findByText(recorded, {exact:false})).toBeInTheDocument();
    expect(within(queue).getByText(lang==="ja"?"1分0秒":"1m 0s")).toBeInTheDocument();
    act(()=>vi.advanceTimersByTime(2000));
    for (const next of NEXT) {
      l = {...l, first: {...l.first!, next}};
      act(() => applyChange({kind:"pull", path:"acme/demo", id:"3"}, client));
      await within(queue).findByText(label(next));
      expect(within(queue).getByText(recorded, {exact:false})).toBeInTheDocument();
      expect(within(queue).getByText(lang==="ja"?"1分2秒":"1m 2s")).toBeInTheDocument();
    }
  });
  it("uses Branvil's icon for service comments",async()=>{
    fakeApi(routes(null,{[`GET ${P}/activity`]:json(200,{comments:[{id:"gate_failure",author:"branvil",body:"Gate failed",created_at:"2026-10-25T10:00:00Z",reactions:[],meta:null,url:""}],reviews:[],threads:[]})}));
    renderApp("/acme/demo/-/pulls/3",lang);
    expect(await screen.findByRole("img",{name:"Branvil"})).toBeInTheDocument();
  });
  it("shows checking for a mismatched head",async()=>{
    fakeApi(routes(null,{[`GET ${P}/checks`]:json(200,{head_sha:BASE,checks:[{name:"test",status:"completed",conclusion:"success"}]})}));
    renderApp("/acme/demo/-/pulls/3",lang);
    const journey=await screen.findByRole("region",{name:journeyName()});
    expect(await within(journey).findByText(`test · ${words["landing.checking"]}`)).toBeInTheDocument();
  });
  it.each(["conflict", "gate_failed", "gating", "passed"] as const)("forgets an old %s gate after the head moves", async (state) => {
    const l = landing({head_sha: BASE, gate: BASE, state: state === "conflict" || state === "gate_failed" ? "left" : state, reason: {code:state, message:"Old failure", jobs:["old-job"], files:["old-file.rs"]}});
    fakeApi(routes(l));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words["landing.notInQueue"])).toBeInTheDocument();
    expect(journey.querySelector('[aria-current="step"]')).toHaveAttribute("data-stop", "conditions");
    expect(within(journey).queryByText(/old-job|old-file|Old failure/)).not.toBeInTheDocument();
    expect(within(journey).queryByText(words["landing.retryGate"])).not.toBeInTheDocument();
  });
  it.each([
    ["conflict", "gate"], ["gate_failed", "gate"], ["already_in_base", "gate"], ["host_failed", "host"], ["publish_stopped", "host"],
    ["head_checks", "conditions"], ["approvals", "conditions"], ["empty_head", "conditions"], ["publish_requirements", "conditions"],
  ] as const)("marks where it left the queue (%s)", async (code, stop) => {
    fakeApi(routes(landing({state:"left",first:null,position:null,reason:{code,message:"Service English",jobs:["build"],files:[]}})));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words["landing.leftWith"].replace("{reason}", words[`landing.reason.${code}`]))).toBeInTheDocument();
    const current = journey.querySelector('[aria-current="step"]') as HTMLElement;
    expect(current).toHaveAttribute("data-stop", stop);
    expect(current).toHaveAttribute("data-state", "failed");
    expect(within(journey).getByText(words["landing.failedJobs"].replace("{jobs}", "build"))).toBeInTheDocument();
  });
  it("still names what is missing after leaving for it", async () => {
    fakeApi(routes(null, {[`GET ${P}`]: json(200, pull({ approvals: 0, landing: landing({state:"left",first:null,position:null,reason:{code:"approvals",message:"",jobs:[],files:[]}}) }))}));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words["merge.approvals"].replace("{count}", "0").replace("{required}", "1"))).toBeInTheDocument();
    expect(journey.querySelector('[aria-current="step"]')).toHaveAttribute("data-state", "failed");
    expect(within(journey).getByText(words["landing.rejoin"])).toBeInTheDocument();
  });
  it("shows the gate's progress on the pull request page", async () => {
    fakeApi(routes(entry("gate", { state: "gating" }), {
      "GET /api/v1/repos/acme/demo": json(200, { ...REPO, required_checks: ["test", "lint", "build"] }),
      [`GET ${P}/checks`]: json(200, { head_sha: HEAD, checks: [{ name: "gate/test", status: "completed", conclusion: "failure" }, { name: "gate/lint", status: "in_progress" }] }),
    }));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText("1/3")).toBeInTheDocument();
  });
  it.each([
    ["already_in_base", "landing.closePull"], ["empty_head", "landing.closePull"], ["gate_failed", "landing.retryGate"],
    ["host_failed", "landing.retryHost"], ["publish_stopped", "landing.publishAgain"], ["conflict", "landing.rejoin"],
  ] as const)("says how to rejoin after leaving for %s", async (code, recovery) => {
    fakeApi(routes(landing({state:"left",first:null,position:null,reason:{code,message:"Service English",jobs:[],files:["src/main.rs"]}})));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words[recovery])).toBeInTheDocument();
    expect(within(journey).getByText(words["landing.conflicts"].replace("{files}", "src/main.rs"))).toBeInTheDocument();
    expect(within(journey).queryByText("Service English") !== null).toBe(code === "publish_stopped");
  });
  it.each(["draft", "base_moved"] as const)("collects requirements again after leaving for %s", async (code) => {
    fakeApi(routes(landing({state:"left",first:null,position:null,reason:{code,message:"",jobs:[],files:[]}})));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", {name:journeyName()});
    expect(await within(journey).findByText(words["landing.notInQueue"])).toBeInTheDocument();
    expect(journey.querySelector('[aria-current="step"]')).toHaveAttribute("data-stop", "conditions");
  });
  const UNANSWERED: PullRequest["unanswered"] = [
    { kind: "review", id: "rev_1", author: "owner", url: "http://branvil.test/acme/demo/-/pulls/3#rev_1" },
    { kind: "thread", id: "cmt_2", author: "ken", url: "http://branvil.test/acme/demo/-/pulls/3#cmt_2" },
  ];
  const left = (code: string) => landing({ state: "left", first: null, position: null, reason: { code, message: "", jobs: [], files: [] } });
  const statementLinks = (journey: HTMLElement) =>
    within(journey).getAllByRole("link").map((a) => [a.textContent, a.getAttribute("href")]);
  it.each([null, "unanswered"])("waits for the author's answer at the requirements (left for %s), linking each statement", async (code) => {
    const l = code ? left(code) : null;
    fakeApi(routes(l, { [`GET ${P}`]: json(200, pull({ landing: l, author: "agent", approvals: 1, unanswered: UNANSWERED })) }));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", { name: journeyName() });
    expect(await within(journey).findByText(words["landing.unanswered"])).toBeInTheDocument();
    const current = journey.querySelector('[aria-current="step"]') as HTMLElement;
    expect(current).toHaveAttribute("data-stop", "conditions");
    expect(current).toHaveAttribute("data-state", "turn");
    expect(within(journey).getByText(words["landing.unansweredHint"])).toBeInTheDocument();
    expect(statementLinks(journey)).toEqual([
      [words["landing.statement.review"].replace("{author}", "owner"), "#rev_1"],
      [words["landing.statement.thread"].replace("{author}", "ken"), "#cmt_2"],
    ]);
  });
  it("still links the statements after leaving the gate for a failure", async () => {
    const l = left("gate_failed");
    fakeApi(routes(l, { [`GET ${P}`]: json(200, pull({ landing: l, author: "agent", approvals: 1, unanswered: UNANSWERED.slice(0, 1) })) }));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", { name: journeyName() });
    expect(await within(journey).findByText(words["landing.retryGate"])).toBeInTheDocument();
    expect(journey.querySelector('[aria-current="step"]')).toHaveAttribute("data-state", "failed");
    expect(statementLinks(journey)).toEqual([[words["landing.statement.review"].replace("{author}", "owner"), "#rev_1"]]);
  });
  it("says in the list that the author's answer is awaited", async () => {
    const p = pull({ landing: left("unanswered"), approvals: 1, unanswered: UNANSWERED });
    fakeApi(routes(null, { "GET /api/v1/repos/acme/demo/pulls": json(200, { items: [p], next_cursor: null }), [`GET ${P}`]: json(200, p) }));
    renderApp("/acme/demo", lang);
    const badge = await listBadge();
    await waitFor(() => expect(badge).toHaveTextContent(words["landing.blockedBy"].replace("{what}", words["landing.unanswered"])));
    expect(badge).toHaveAttribute("title", words["landing.unanswered"]);
    expect(badge).toHaveAttribute("data-stop", "conditions");
    tone(badge, "warning");
  });
});

for (const host of [false, true]) {
  it.each([true, false])(`accepts only a service path skip for ${host ? "publication" : "merge"} (service=%s)`, async (service) => {
    const path_skip = service ? {paths:["src/**"], bases:[{pull:3,base:"main",base_sha:BASE,merge_base:BASE}]} : null;
    fakeApi(routes(landing({state:"passed",first:{...landing().first!, next:host ? "owner_publish" : "owner_merge"}}), {
      "GET /api/v1/repos/acme/demo": json(200, {...REPO,upstream:host ? REPO.upstream : null}),
      [`GET ${P}/checks`]: json(200,{head_sha:HEAD,base_sha:BASE,checks:[{name:"test",status:"completed",conclusion:"skipped",path_skip}],checks_expected:true}),
    }));
    renderApp("/acme/demo/-/pulls/3");
    const action = await screen.findByRole("button",{name:host ? "Publish to GitHub" : dictionaries.en["merge.submit"]});
    await checksLoaded();
    if (service) expect(action).toBeEnabled(); else expect(action).toBeDisabled();
  });
  it.each([
    { why: "not queued", l: null },
    { why: "waiting", l: landing({ state: "waiting", position: 2 }) },
    { why: "gating", l: landing({ state: "gating" }) },
    { why: "left", l: landing({ state: "left", position: null }) },
    { why: "outdated head", l: landing({ head_sha: BASE }) },
    { why: "outdated base", l: landing({ base_sha: HEAD }) },
    { why: "not the first", l: landing({ position: 2 }) },
  ])(`blocks ${host ? "publishing" : "merging"} with successful head checks and approvals but $why`, async ({ l }) => {
    fakeApi(routes(l, { "GET /api/v1/repos/acme/demo": json(200, { ...REPO, upstream: host ? REPO.upstream : null }) }));
    renderApp("/acme/demo/-/pulls/3");
    const action = host ? "Publish to GitHub" : dictionaries.en["merge.submit"];
    await checksLoaded();
    expect(screen.getByRole("button", { name: action })).toBeDisabled();
  });
}


describe.each(["en", "ja"] as const)("resuming stopped publication (%s)", (lang) => {
  const words = dictionaries[lang];
  const stopped = {
    provider: "github", repository: "example/demo", account: "agent", number: 42,
    url: "https://github.com/example/demo/pull/42", state: "open" as const, pushed: HEAD,
    published_at: "2026-10-25T10:00:00Z", stopped: "Host branch changed", error: null, auto: true,
  };
  const left = landing({ state: "left", position: null, reason: { code: "publish_stopped", message: "Host branch changed", jobs: [], files: [] } });

  it("rejoins behind another pull, waits for its gate, then confirms replacing the host branch", async () => {
    let current = pull({ upstream: stopped, landing: left, approvals: 1 });
    const waiting = { ...current, landing: landing({ resume_publish: true, state: "waiting", position: 2, first: { ...landing().first!, number: 2, next: "gate", state: "gating" } }) };
    const fake = fakeApi(routes(left, {
      [`GET ${P}`]: () => json(200, current),
      [`POST ${P}/landing/resume`]: () => { current = waiting; return json(200, current); },
      [`POST ${P}/publish`]: () => { current = { ...current, upstream: { ...stopped, stopped: null } }; return json(200, current); },
    }));
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    const user = userEvent.setup();
    const { client } = renderApp("/acme/demo/-/pulls/3", lang);
    const resume = await screen.findByRole("button", { name: words["landing.resume"] });
    await waitFor(() => expect(resume).toBeEnabled());
    await user.click(resume);
    expect(fake.calls("POST", `${P}/landing/resume`)[0]?.body).toEqual({ expected_head: HEAD, expected_base: BASE });
    const again = await screen.findByRole("button", { name: words["publish.again"].replace("{host}", "GitHub") });
    expect(again).toBeDisabled();
    expect(screen.queryByRole("button", { name: words["landing.resume"] })).not.toBeInTheDocument();
    expect(screen.getByText(words["landing.waitingAt"].replace("{position}", "2"))).toBeInTheDocument();
    expect(confirm).not.toHaveBeenCalled();
    expect(fake.calls("POST", `${P}/publish`)).toHaveLength(0);
    current = { ...current, landing: landing({ resume_publish: true, state: "gating" }) };
    act(() => applyChange({ kind: "pull", path: "acme/demo", id: "3" }, client));
    const gate = screen.getByRole("region", { name: words["landing.journey"].replace("{base}", "main") });
    expect((await within(gate).findAllByText(words["landing.next.gate"]))[0]).toBeInTheDocument();
    expect(again).toBeDisabled();
    current = { ...current, landing: landing({ resume_publish: true, first: { ...landing().first!, next: "owner_publish" } }) };
    act(() => applyChange({ kind: "pull", path: "acme/demo", id: "3" }, client));
    await waitFor(() => expect(again).toBeEnabled());
    expect(within(gate).getAllByText(words["landing.next.owner_publish"].replace("{host}", "GitHub"))[0]).toBeInTheDocument();
    await user.click(again);
    expect(confirm).toHaveBeenCalledWith(words["publish.confirmAgain"].replace("{host}", "GitHub"));
    expect(fake.calls("POST", `${P}/publish`)).toHaveLength(0);
    confirm.mockReturnValue(true);
    await user.click(again);
    await waitFor(() => expect(fake.calls("POST", `${P}/publish`)).toHaveLength(1));
    expect(fake.calls("POST", `${P}/publish`)[0]?.body).toEqual({ expected_head: HEAD, expected_base: BASE });
  });

  it.each(["checks", "approvals", "draft", "mismatched head"] as const)("cannot resume without current readiness (%s)", async (blockedBy) => {
    fakeApi(routes(left, {
      [`GET ${P}`]: json(200, pull({ upstream: stopped, landing: null, approvals: blockedBy === "approvals" ? 0 : 1, draft: blockedBy === "draft" })),
      [`GET ${P}/checks`]: json(200, { head_sha: blockedBy === "mismatched head" ? BASE : HEAD, base_sha: BASE, checks: [{ name: "test", status: "completed", conclusion: blockedBy === "checks" ? "failure" : "success" }], checks_expected: true, base_checks: 1 }),
    }));
    renderApp("/acme/demo/-/pulls/3", lang);
    const resume = await screen.findByRole("button", { name: words["landing.resume"] });
    await waitFor(() => expect(screen.queryByText(words["common.loading"])).not.toBeInTheDocument());
    expect(resume).toBeDisabled();
  });

  it("shows a resume refusal without publishing or dropping the stopped state", async () => {
    const fake = fakeApi(routes(left, {
      [`GET ${P}`]: json(200, pull({ upstream: stopped, landing: left, approvals: 1 })),
      [`POST ${P}/landing/resume`]: apiError(409, "head_moved", "Head changed"),
    }));
    const user = userEvent.setup();
    renderApp("/acme/demo/-/pulls/3", lang);
    const resume = await screen.findByRole("button", { name: words["landing.resume"] });
    await waitFor(() => expect(resume).toBeEnabled());
    await user.click(resume);
    expect(await screen.findByText(words["merge.headMoved"])).toBeInTheDocument();
    expect(fake.calls("POST", `${P}/publish`)).toHaveLength(0);
    expect(screen.getByRole("button", { name: words["landing.resume"] })).toBeInTheDocument();
  });
});
