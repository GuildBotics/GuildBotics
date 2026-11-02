import { describe, expect, it } from "vitest";
import { href, inAppPath, parseRoute } from "../router/routes";

describe("routes", () => {
  it("parses the permanent web URLs of the API", () => {
    expect(parseRoute("/acme/demo/-/tickets/3")).toEqual({ name: "ticket", repo: "acme/demo", key: "3" });
    expect(parseRoute("/acme/demo/-/pulls/3")).toEqual({ name: "pull", repo: "acme/demo", number: 3 });
    expect(parseRoute("/acme/demo/-/commits/abc123")).toEqual({ name: "commit", repo: "acme/demo", sha: "abc123" });
    expect(parseRoute("/acme/demo/-/jobs/att_01")).toEqual({ name: "job", repo: "acme/demo", attemptId: "att_01" });
    expect(parseRoute("/acme/demo")).toEqual({ name: "repo", repo: "acme/demo" });
    // A workspace's page is its task flow; its pull requests have a page of their own.
    expect(parseRoute("/acme")).toEqual({ name: "workspace", workspace: "acme" });
    expect(parseRoute("/acme/-/pulls")).toEqual({ name: "pulls", workspace: "acme" });
    expect(parseRoute("/acme/-/board")).toEqual({ name: "board", workspace: "acme" });
  });

  it("parses app routes", () => {
    expect(parseRoute("/")).toEqual({ name: "home" });
    expect(parseRoute("/-/enter")).toEqual({ name: "enter" });
    expect(parseRoute("/-/authorize")).toEqual({ name: "authorize" });
    expect(parseRoute("/-/settings")).toEqual({ name: "settings", section: null });
    expect(parseRoute("/-/settings/members")).toEqual({ name: "settings", section: "members" });
    // A repository's editor in its workspace's settings.
    expect(parseRoute(href.repoSettings("acme/demo"))).toEqual({ name: "settings", section: null, repo: "acme/demo" });
  });

  it("rejects malformed paths", () => {
    // A repository is exactly `<workspace>/<name>`.
    expect(parseRoute("/acme/team/demo").name).toBe("not_found");
    expect(parseRoute("/acme/team/demo/-/tickets/1").name).toBe("not_found");
    expect(parseRoute("/-/boards/acme").name).toBe("not_found");
    expect(parseRoute("/acme/-/pulls/1").name).toBe("not_found");
    expect(parseRoute("/acme/-/relations").name).toBe("not_found");
    expect(parseRoute("/acme/-/board/1").name).toBe("not_found");
    expect(parseRoute("/acme/-/tickets").name).toBe("not_found");
    expect(parseRoute("/acme/demo/-/pulls/0").name).toBe("not_found");
    expect(parseRoute("/acme/demo/-/pulls/x").name).toBe("not_found");
    expect(parseRoute("/acme/demo/-/wiki/x").name).toBe("not_found");
    expect(parseRoute("/acme/demo/-/tickets/1/extra").name).toBe("not_found");
    // Ticket numbers follow the rule of pull request numbers: from 1, no leading zeros.
    expect(parseRoute("/acme/demo/-/tickets/0").name).toBe("not_found");
    expect(parseRoute("/acme/demo/-/tickets/01").name).toBe("not_found");
    expect(parseRoute("/acme/demo/-/tickets/DEMO-1").name).toBe("not_found");
    expect(parseRoute("/acme/-/tickets/1").name).toBe("not_found");
  });

  it("round-trips href builders", () => {
    expect(parseRoute(href.ticket("a/b", "7"))).toEqual({ name: "ticket", repo: "a/b", key: "7" });
    expect(parseRoute(href.workspace("a"))).toEqual({ name: "workspace", workspace: "a" });
    expect(parseRoute(href.pulls("a"))).toEqual({ name: "pulls", workspace: "a" });
    expect(parseRoute(href.board("a b"))).toEqual({ name: "board", workspace: "a b" });
  });

  it("keeps foreign URLs out of the app", () => {
    const origin = "http://localhost:7878";
    expect(inAppPath("http://127.0.0.1:7878/acme/demo/-/pulls/3", origin)).toBe("/acme/demo/-/pulls/3");
    expect(inAppPath("http://localhost:7878/acme/demo/-/tickets/1#cmt_1", origin)).toBe("/acme/demo/-/tickets/1#cmt_1");
    expect(inAppPath("https://github.com/acme/demo/issues/1", origin)).toBeNull();
    expect(inAppPath("http://127.0.0.1:9999/acme/demo/-/pulls/3", origin)).toBeNull();
    expect(inAppPath("javascript:alert(1)", origin)).toBeNull();
  });
});
