import { MantineProvider } from "@mantine/core";
import { QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import { I18nProvider } from "../i18n";
import { createQueryClient } from "../query/hooks";
import { fakeApi, json } from "../test/fakeApi";
import { ReviewersCell } from "../views/pulls/ReviewersCell";
import { NEW_HEAD, pull } from "./fixtures";

function renderReviewers(pr = pull(), language: "en" | "ja" = "en") {
  const fake = fakeApi({
    "GET /api/v1/members": json(200, { items: [], next_cursor: null }),
  });
  const client = createQueryClient();
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MantineProvider env="test">
      <QueryClientProvider client={client}>
        <I18nProvider initial={language}>{children}</I18nProvider>
      </QueryClientProvider>
    </MantineProvider>
  );
  return { ...render(<a href="/acme/demo/-/pulls/3"><ReviewersCell pull={pr} /></a>, { wrapper }), fake, client };
}

describe("pull request list reviewers", () => {
  it("shows the server's verdicts and requests with the detail view's marks, without reading activity", () => {
    const { container, fake } = renderReviewers(pull({ reviewers: [], review_states: [
      { member: "approved-agent", requested: false, verdict: "approve", outdated: false },
      { member: "comment-agent", requested: false, verdict: "comment", outdated: true },
      { member: "changes-agent", requested: false, verdict: "request_changes", outdated: false },
      { member: "again-agent", requested: true, verdict: "approve", outdated: true },
      { member: "pending-agent", requested: true, verdict: null, outdated: false },
    ] }));
    const summary = screen.getByRole("img", {
      name: "Reviewers: approved-agent (Approved), comment-agent (Commented (older revision)), changes-agent (Changes requested), again-agent (Requested again), pending-agent (Requested)",
    });
    expect(summary).toHaveAttribute("title", summary.getAttribute("aria-label"));
    expect(container.querySelectorAll(".review-mark.approve")).toHaveLength(1);
    expect(container.querySelectorAll(".review-mark.comment.stale")).toHaveLength(1);
    expect(container.querySelectorAll(".review-mark.request_changes")).toHaveLength(1);
    expect(container.querySelectorAll(".review-mark.pending")).toHaveLength(2);
    expect(container.querySelectorAll(".review-mark.stale")).toHaveLength(1);
    expect(container.querySelectorAll(".review-mark-anchor")).toHaveLength(5);
    // The row is the only navigation target; avatars add no interactive controls.
    const link = screen.getByRole("link");
    expect(within(link).queryByRole("button")).not.toBeInTheDocument();
    expect(within(link).queryByRole("link")).not.toBeInTheDocument();
    expect(container.querySelector("[tabindex]")).not.toBeInTheDocument();
    expect(fake.requests.some((request) => request.path.endsWith("/activity"))).toBe(false);
  });

  it("updates closed pull requests from the server's summary rather than inferring state from the head", () => {
    const state = { member: "review-agent", requested: false, verdict: "approve" as const, outdated: false };
    const { container, rerender } = renderReviewers(pull({ state: "closed", review_states: [state] }));
    expect(screen.getByRole("img", { name: "Reviewers: review-agent (Approved)" })).toBeInTheDocument();
    expect(container.querySelector(".review-mark.approve")).not.toHaveClass("stale");
    // A head change alone does not override the server's decision.
    rerender(<a href="/acme/demo/-/pulls/3"><ReviewersCell pull={pull({ state: "closed", head_sha: NEW_HEAD, review_states: [state] })} /></a>);
    expect(container.querySelector(".review-mark.approve")).not.toHaveClass("stale");
    rerender(<a href="/acme/demo/-/pulls/3"><ReviewersCell pull={pull({ state: "closed", head_sha: NEW_HEAD, review_states: [{ ...state, outdated: true }] })} /></a>);
    expect(screen.getByRole("img", { name: "Reviewers: review-agent (Approved (older revision))" })).toBeInTheDocument();
    expect(container.querySelector(".review-mark.approve")).toHaveClass("stale");
  });

  it.each(["en", "ja"] as const)("uses a reviewer-specific empty label in %s", (language) => {
    const { container } = renderReviewers(pull({ review_states: [] }), language);
    expect(screen.getByRole("img", { name: language === "ja" ? "レビュアー: なし" : "Reviewers: no one" })).toHaveTextContent("—");
    expect(container.querySelector(".review-mark-anchor")).not.toBeInTheDocument();
  });
});
