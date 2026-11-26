import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Releases, VerificationCommit } from "../api/types";
import { apiError, fakeApi, json, renderApp, SESSION } from "../test/fakeApi";
import { choose } from "../test/mantine";

const NEWEST = "d".repeat(40);
const POINT = "c".repeat(40);
const OLDEST_POINT = "b".repeat(40);
const RELEASED = "a".repeat(40);
const page = (items: unknown[]) => json(200, { items, next_cursor: null });
const repository = (path: string) => ({
  path,
  default_branch: "main",
  upstream: null,
  required_approvals: 0,
});
const commit = (sha: string, title: string): VerificationCommit => ({
  sha,
  title,
  author: "Test",
  authored_at: "2026-11-26T03:00:00Z",
  mark: "verified",
});
const note = (message: string) => ({ job: "schema", attempt_id: "att_1", message });
const releases = (extra: Partial<Releases> = {}): Releases => ({
  tag_prefix: "release/",
  schedule: ["12:00", "*:30"],
  time_zone: "Asia/Tokyo",
  next_at: "2026-11-26T03:00:00.000Z",
  problem: null,
  next_name: "release/2026-11-26-1",
  points: [
    { sha: POINT, notes: [note("format 48 → 49")] },
    { sha: OLDEST_POINT, notes: [note("format 47 → 48")] },
  ],
  releases: [
    { name: "release/2026-11-25-1", sha: RELEASED, title: "Old", authored_at: "2026-11-25" },
  ],
  ...extra,
});
const shell = {
  "GET /api/v1/session": json(200, SESSION),
  "GET /api/v1/workspaces": page([{ name: "acme", lanes: ["new"] }]),
  "GET /api/v1/admin/diagnostics": json(200, {}),
  "GET /api/v1/members": page([]),
  "GET /api/v1/repos": page([repository("acme/demo"), repository("acme/web")]),
  "GET /api/v1/repos/acme/demo/verification/commits": page([
    commit(NEWEST, "Newest"),
    commit(POINT, "Point"),
    commit(OLDEST_POINT, "Oldest point"),
    commit(RELEASED, "Old"),
  ]),
};
/** The release button of the verified commit titled `title`, if it has one. */
const releaseButton = (title: string) => {
  const section = screen.getByRole("region", { name: /Verified commits|確認済みのコミット/ });
  const row = within(section).getByText(title).parentElement!.parentElement!;
  return within(row).queryByRole("button", { name: /Release|リリースする/ });
};

describe("releases page", () => {
  it("is reached from the sidebar and shows the chosen repository from the top", async () => {
    const user = userEvent.setup();
    fakeApi({
      ...shell,
      "GET /api/v1/repos/acme/demo/releases": json(200, releases()),
      "GET /api/v1/repos/acme/web/releases": json(200, releases({ schedule: [], points: [] })),
      "GET /api/v1/repos/acme/web/verification/commits": page([]),
    });
    renderApp("/workspaces/acme/pulls");
    await user.click(await screen.findByRole("link", { name: "Releases" }));
    expect(window.location.pathname).toBe("/workspaces/acme/releases");
    expect(
      await screen.findByText(
        /Released every day at 12:00, every hour at minute 30 \(Asia\/Tokyo\)/,
      ),
    ).toBeInTheDocument();
    await choose(user, "Repository", "acme/web");
    expect(window.location.pathname).toBe("/workspaces/acme/repos/web/releases");
    expect(await screen.findByText(/Releases are made only from this page/)).toBeInTheDocument();
  });

  it("marks release points and lets only the oldest one be released first", async () => {
    fakeApi({ ...shell, "GET /api/v1/repos/acme/demo/releases": json(200, releases()) });
    renderApp("/workspaces/acme/repos/demo/releases", "ja");
    await screen.findByText("Oldest point");
    expect(
      screen.getByText(/⚠️ のコミットは、それだけでリリースする必要があります/),
    ).toBeInTheDocument();
    const marks = screen.getAllByRole("link", { name: "それだけでリリースが必要なコミット" });
    expect(marks).toHaveLength(2);
    expect(marks[0]).toHaveAttribute("href", "/workspaces/acme/repos/demo/jobs/att_1");
    expect(releaseButton("Oldest point")).toBeEnabled();
    expect(releaseButton("Point")).toBeDisabled();
    expect(releaseButton("Newest")).toBeDisabled();
    expect(releaseButton("Old")).toBeNull();
  });

  it("sends the default name unchanged, a changed one, and explains a refusal", async () => {
    const user = userEvent.setup();
    let reply = json(200, releases({ points: [] }));
    const api = fakeApi({
      ...shell,
      "GET /api/v1/repos/acme/demo/releases": json(200, releases({ points: [] })),
      "POST /api/v1/repos/acme/demo/releases": () => reply,
    });
    renderApp("/workspaces/acme/repos/demo/releases");
    await screen.findByText("Newest");
    const send = async (name?: string) => {
      await user.click(releaseButton("Newest")!);
      const dialog = await screen.findByRole("dialog");
      const input = within(dialog).getByRole("textbox", { name: /Tag name/ });
      expect(input).toHaveValue("2026-11-26-1");
      if (name !== undefined) {
        await user.clear(input);
        await user.type(input, name);
      }
      await user.click(within(dialog).getByRole("button", { name: "Release" }));
      return dialog;
    };
    await send();
    await waitFor(() =>
      expect(api.calls("POST", "/api/v1/repos/acme/demo/releases")).toHaveLength(1),
    );
    expect(api.calls("POST", "/api/v1/repos/acme/demo/releases")[0]!.body).toEqual({
      sha: NEWEST,
    });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    reply = apiError(409, "release_point_first", "release the point first");
    const dialog = await send("v2");
    expect(api.calls("POST", "/api/v1/repos/acme/demo/releases")[1]!.body).toEqual({
      sha: NEWEST,
      name: "v2",
    });
    expect(
      await within(dialog).findByText("Release the older commit marked ⚠️ first."),
    ).toBeInTheDocument();
  });
});
