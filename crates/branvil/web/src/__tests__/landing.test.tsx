import { QueryClientProvider } from "@tanstack/react-query";
import { createQueryClient } from "../query/hooks";
import { keys } from "../query/keys";
import { render, screen, within } from "@testing-library/react";
import { theme } from "@guildbotics/ui/theme";
import { MantineProvider } from "@mantine/core";
import { describe, expect, it } from "vitest";
import type { Check, Landing, LandingNext, Repository } from "../api/types";
import { dictionaries, I18nProvider } from "../i18n";
import { JourneyBadge, LandingJourney, whereabouts } from "../views/pull/LandingStatus";
import { fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { BASE, HEAD, landing, pull } from "./fixtures";

const P = "/api/v1/repos/acme/demo/pulls/3";
const REPO: Repository = {
  path: "acme/demo", default_branch: "main", clone_url: "", url: "", protected_branches: ["main"],
  responder: { member: "aiko", chosen: false, automatic: "aiko" }, post_merge_jobs: [], required_checks: ["test"], required_approvals: 1, auto_land: false, hold_while_failing: false, ci_network: false, labels: [], revision: 1,
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
  function component(next: LandingNext, blockers: Landing["blockers"] = [], journey = false, repo = REPO, checks: Partial<Check>[] = [
    { name: "test", status: "in_progress" },
    { name: "rust-test", status: "completed", conclusion: "failure" },
    { name: "passed", status: "completed", conclusion: "success" },
  ]) {
    const p = pull({ landing: landing({ next, blockers }), unanswered: blockers.some(b => b.code === "unanswered") ? [{ kind: "comment", id: "question", author: "reviewer", url: "#question" }] : [] });
    const client = createQueryClient();
    client.setQueryData(keys.pullPart(p.repository, p.number, "checks"), { head_sha: HEAD, checks });
    return render(<QueryClientProvider client={client}><MantineProvider theme={theme}><I18nProvider initial={lang}>{journey ? <LandingJourney pull={p} repo={repo} /> : <JourneyBadge pull={p} repo={repo} load={false} />}</I18nProvider></MantineProvider></QueryClientProvider>);
  }
  it.each([false, true].flatMap(published => ["aiko", null].map(assignee => ({ published, assignee }))))("shows the repair link and hold (published=$published, assignee=$assignee)", ({ published, assignee }) => {
    const blockers = [{ code: "held", message: "Repair #42; assignee: aiko.", held: { number: 42, assignee }, files: [], jobs: [] }];
    const p = pull({ landing: landing({ next: "conditions", blockers, host_url: published ? "https://host.test/pr/1" : null }) });
    render(<QueryClientProvider client={createQueryClient()}><MantineProvider theme={theme}><I18nProvider initial={lang}><LandingJourney pull={p} repo={REPO} /><JourneyBadge pull={p} repo={REPO} load={false} /></I18nProvider></MantineProvider></QueryClientProvider>);
    expect(screen.getByRole("link", { name: lang === "ja" ? "修復チケット #42 を開く" : "Open repair ticket #42" })).toHaveAttribute("href", "/acme/demo/-/tickets/42");
    expect(screen.getByText((assignee ? words["landing.held"].replace("{name}", assignee) : lang === "ja" ? "保留中：main の確認が失敗しています（修復 #{number}、担当なし）。main の確認が通ると、保留が解けます。" : "Held: main verification is failing (repair #{number}, unassigned). The hold lifts when main verification passes.").replace("{number}", "42"))).toBeInTheDocument();
    const detail = words[assignee ? "landing.held" : "landing.heldUnassigned"].replace("{number}", "42").replace("{name}", assignee ?? "");
    expect(screen.getAllByText(words["landing.blocker.held"])[1]!.closest(".mantine-Badge-root")).toHaveAttribute("title", detail);
    expect(screen.queryByText(blockers[0]!.message)).not.toBeInTheDocument();
    const caution = words["landing.heldPublished"].replace("{host}", "GitHub");
    expect(screen.queryByText(caution) !== null).toBe(published);
    expect(screen.getAllByText(words["landing.blocker.held"])[1]!.closest(".mantine-Badge-root")).toHaveStyle({ "--badge-color": "var(--color-warning-text)" });
  });
  it("marks a failed local automatic merge at the merge step", () => {
    const { container } = component("owner_fix_auto", [], true, { ...REPO, upstream: null });
    expect(container.querySelector('[data-stop="merge"]')).toHaveAttribute("data-state", "failed");
    expect(container.querySelector('[data-stop="merge"] .lucide-x')).not.toBeNull();
    expect(container.querySelector('[data-stop="host"]')).toBeNull();
  });
  it("names each unmet condition above its detail", () => {
    component("conditions", [
      { code: "approvals", count: 0, required: 1, message: "Two approvals required", jobs: [], files: [] },
      { code: "head_checks", message: "test is running", jobs: ["test"], files: [] },
    ], true);
    expect(screen.getByText(words["landing.blocker.approvals"])).toBeInTheDocument();
    expect(screen.getByText(lang === "ja" ? "承認：0 / 1" : "Approvals: 0 / 1")).toBeInTheDocument();
    expect(screen.queryByText("Two approvals required")).not.toBeInTheDocument();
    expect(screen.getByText(words["landing.blocker.head_checks"])).toBeInTheDocument();
    expect(screen.getByText(lang === "ja" ? "test · 実行中" : "test · Running")).toBeInTheDocument();
    expect(screen.queryByText("test is running")).not.toBeInTheDocument();
  });
  it("uses structured values for every blocker in the journey and badge", () => {
    const blockers = [
      { code: "head_checks", message: "Server checks", jobs: ["rust-test", "test", "missing"], files: [] },
      { code: "approvals", message: `Approvals of ${HEAD}`, count: 0, required: 1, jobs: [], files: [] },
      { code: "unanswered", message: "Server unanswered", jobs: [], files: [] },
      { code: "conflict", message: "Server conflict", jobs: [], files: ["README.md"] },
      { code: "host_failed", message: "Server host", jobs: ["build"], files: [] },
      ...["draft", "already_in_base", "publish_requirements", "checking"].map(code => ({ code, message: `Server ${code}`, jobs: [], files: [] })),
    ];
    const expected = [
      `rust-test · ${words["landing.check.failure"]}`, `test · ${words["landing.check.running"]}`, `missing · ${words["landing.check.missing"]}`,
      lang === "ja" ? "承認：0 / 1" : "Approvals: 0 / 1", words["landing.unanswered"],
      words["landing.detail.conflict"].replace("{files}", "README.md"),
      words["landing.detail.host_failed"].replace("{jobs}", "build"),
      ...["draft", "already_in_base", "publish_requirements", "checking"].map(code => words[`landing.detail.${code}` as keyof typeof words]),
    ];
    const journey = component("conditions", blockers, true);
    for (const text of expected) expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: words["landing.statement.comment"].replace("{author}", "reviewer") })).toHaveAttribute("href", "#question");
    expect(journey.container.textContent).not.toContain(HEAD);
    expect(journey.container.textContent).not.toContain("passed");
    for (const b of blockers) expect(journey.container.textContent).not.toContain(b.message);
    journey.unmount();
    component("conditions", blockers);
    expect(screen.getByText(`${words["landing.blocker.head_checks"]} +8`).closest(".mantine-Badge-root")).toHaveAttribute("title", expected.join(" / "));
  });
  it.each([true, false])("shows server-unmet successes and skips as checking (journey=%s)", journey => {
    const checks: Check[] = [
      { name: "success", status: "completed", conclusion: "success" },
      { name: "reused", status: "completed", conclusion: "success", reused_from: { attempt_id: "previous", sha: BASE, url: "/previous", basis: "paths" } },
      ...(["paths", "post_merge"] as const).map(reason => ({ name: reason, status: "completed" as const, conclusion: "skipped" as const, path_skip: { reason, paths: [], bases: [] } })),
    ];
    const blockers = [{ code: "head_checks", message: "Server detail", jobs: checks.map(c => c.name), files: [] }];
    component("conditions", blockers, journey, REPO, checks);
    const expected = checks.map(c => `${c.name} · ${words["landing.check.checking"]}`);
    if (journey) {
      for (const text of expected) expect(screen.getByText(text)).toBeInTheDocument();
      for (const key of ["success", "pathsSkipped", "postMergeSkipped"] as const) {
        expect(screen.queryByText(new RegExp(`· ${words[`landing.check.${key}`]}$`))).not.toBeInTheDocument();
      }
    } else {
      expect(screen.getByText(words["landing.blocker.head_checks"]).closest(".mantine-Badge-root")).toHaveAttribute("title", expected.join(" / "));
    }
  });
  it.each([
    ["owner_merge_host", "warning"], ["owner_merge", "warning"],
    ["owner_publish", "danger"], ["owner_fix_auto", "danger"], ["owner_fix_following", "danger"],
    ["publishing", "neutral"], ["host_checks", "neutral"], ["auto_merge", "neutral"], ["following", "neutral"], ["host_closed", "neutral"],
  ] as const)("gives %s its badge and %s color", (next, color) => {
    component(next);
    const text = words[`landing.badge.${next}`].replaceAll("{host}", "GitHub").replaceAll("{base}", "main");
    expect(screen.getByText(text).closest(".mantine-Badge-root")).toHaveStyle({ "--badge-color": color === "neutral" ? "var(--color-text-subtle)" : `var(--color-${color}-text)` });
  });
  it.each(["draft", "already_in_base", "conflict", "publish_requirements", "head_checks", "approvals", "unanswered", "host_failed", "checking"] as const)("names the %s blocker", code => {
    component("conditions", [{ code, message: "Server detail", count: 0, required: 1, jobs: [], files: [] }]);
    const badge = screen.getByText(words[`landing.blocker.${code}`]).closest(".mantine-Badge-root");
    expect(badge?.getAttribute("title")).not.toContain("Server detail");
    expect(badge?.getAttribute("title")).not.toContain("landing.");
    expect(badge).toHaveStyle({ "--badge-color": ["conflict", "host_failed"].includes(code) ? "var(--color-danger-text)" : "var(--color-text-subtle)" });
  });
  it("summarizes blockers while keeping every detail in the title", () => {
    component("conditions", [{ code: "approvals", count: 0, required: 1, message: "Needs approval", jobs: [], files: [] }, { code: "conflict", message: "Resolve conflict", jobs: [], files: [] }]);
    const badge = screen.getByText(`${words["landing.blocker.approvals"]} +1`).closest(".mantine-Badge-root");
    expect(badge).toHaveAttribute("title", `${lang === "ja" ? "承認：0 / 1" : "Approvals: 0 / 1"} / ${words["landing.detail.conflict"].replace("{files}", "")}`);
    expect(badge).toHaveStyle({ "--badge-color": "var(--color-danger-text)" });
  });
  it.each(["owner_merge_host", "conditions", "owner_fix_auto"] as const)("restores journey icons for %s", next => {
    const { container } = component(next, [], true);
    const states = next === "owner_merge_host" ? ["done", "done", "turn", "todo"] : next === "conditions" ? ["now", "todo", "todo", "todo"] : ["done", "failed", "todo", "todo"];
    const stops = [...container.querySelectorAll(".journey-stop")];
    expect(stops.map(s => s.getAttribute("data-state"))).toEqual(states);
    for (const [i, stop] of stops.entries()) {
      expect(stop.querySelector(".journey-dot svg")).not.toBeNull();
      if (states[i] === "done") expect(stop.querySelector(".lucide-check")).not.toBeNull();
    }
    const heading = container.querySelector("section > div > div");
    expect(heading?.querySelector(next === "owner_fix_auto" ? ".lucide-x" : next === "conditions" ? ".lucide-list-checks" : ".lucide-git-merge")).not.toBeNull();
    if (next === "owner_fix_auto") {
      expect(heading?.querySelector("svg")).toHaveAttribute("stroke", "var(--color-danger-text)");
      expect(screen.getByText(words["landing.next.owner_fix_auto"])).toHaveStyle({ color: "var(--color-danger-text)" });
    }
    expect(container.textContent).not.toMatch(/queue|gate|検問|列/i);
  });
  it("shows all three ready PRs as ready to merge in the list", async () => {
    const items = [1, 2, 3].map(number => pull({ number, landing: landing({ next: "owner_merge_host" }) }));
    fakeApi(routes(items[0]!.landing, { "GET /api/v1/repos/acme/demo/pulls": json(200, { items, next_cursor: null }) }));
    renderApp("/acme/demo", lang);
    expect(await screen.findAllByText(words["landing.badge.owner_merge_host"].replace("{host}", "GitHub"))).toHaveLength(3);
  });
  it.each(["publishing", "owner_publish", "host_checks", "owner_merge_host", "following", "owner_fix_following", "host_closed", "owner_fix_auto", "auto_merge", "owner_merge"] as Exclude<LandingNext, "conditions">[])("shows %s without a queue or checkpoint", async next => {
    fakeApi(routes(landing({ next })));
    renderApp("/acme/demo/-/pulls/3", lang);
    const journey = await screen.findByRole("region", { name: words["landing.journey"].replace("{base}", "main") });
    expect(journey.querySelector('[data-stop="queue"]')).toBeNull();
    expect(journey.querySelector('[data-stop="gate"]')).toBeNull();
    expect(within(journey).getByText(words[`landing.next.${next}`].replaceAll("{host}", "GitHub").replaceAll("{base}", "main"))).toBeInTheDocument();
  });
  it("translates conflict details", async () => {
    fakeApi(routes(landing({ next: "conditions", blockers: [{ code: "conflict", message: "Merge main into topic and resolve README.md", files: ["README.md"], jobs: [] }] })));
    renderApp("/acme/demo/-/pulls/3", lang);
    expect(await screen.findByText(words["landing.detail.conflict"].replace("{files}", "README.md"))).toBeInTheDocument();
    expect(screen.queryByText("Merge main into topic and resolve README.md")).not.toBeInTheDocument();
    expect(screen.getByText(words["landing.blocker.conflict"])).toBeInTheDocument();
  });
  it("offers direct publication when sending stopped", async () => {
    const l = landing({ next: "owner_publish" });
    const p = pull({ landing: l, approvals: 1, upstream: { provider: "github", repository: "example/demo", account: "agent", number: 4, url: "https://host.test/pull/4", state: "open", pushed: HEAD, published_at: "now", stopped: "host refused", error: null, auto: false } });
    fakeApi(routes(l, { [`GET ${P}`]: json(200,p) }));
    renderApp("/acme/demo/-/pulls/3",lang);
    const button = await screen.findByRole("button",{ name: words["publish.again"].replaceAll("{host}","GitHub") });
    expect(button).toBeEnabled();
    for (const alert of screen.getAllByRole("alert")) expect(alert).not.toHaveTextContent(lang === "ja" ? "検問" : "Resume landing");
    expect(screen.queryByRole("button",{ name: /Resume landing|検問の列に戻す/ })).not.toBeInTheDocument();
  });

});

it("omits the host step for a feature-base pull", () => {
  expect(whereabouts(pull({ base: "ticket/parent", landing: landing({ next: "owner_merge" }) }), REPO, false)).toEqual({ stops: ["conditions", "merge", "base"], at: "merge", mark: "turn" });
});
