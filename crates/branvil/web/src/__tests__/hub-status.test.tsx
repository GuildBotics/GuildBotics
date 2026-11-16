import { screen } from "@testing-library/react";
import { it, expect } from "vitest";
import { fakeApi, json, renderApp, SESSION } from "../test/fakeApi";

it.each([true, false])(
  "shows Hub maintenance instead of the standalone stop message: %s",
  async (embedded) => {
    fakeApi({
      "GET /api/v1/session": json(200, SESSION),
      "GET /api/v1/workspaces": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/members": json(200, { items: [], next_cursor: null }),
      "GET /api/v1/admin/diagnostics": json(200, {
        runner: { state: "stopping", mode: "auto", detail: "" },
        hub: embedded
          ? {
              current: {
                sha: "abcdef012345",
                subject: "Build title",
                url: "https://example.com/commit/abcdef012345",
              },
              target: null,
              operation: {
                kind: "update",
                phase: "draining",
                since: "one",
                waiting: { runs: 3, ci_jobs: 2 },
              },
            }
          : null,
      }),
    });
    renderApp("/", "en");
    if (embedded) {
      expect(
        await screen.findByText(/3 running tasks and 2 CI jobs/),
      ).toBeInTheDocument();
      expect(screen.getByRole("link", { name: "abcdef01" })).toHaveAttribute(
        "href",
        "https://example.com/commit/abcdef012345",
      );
      expect(screen.queryByText(/Branvil is stopping/)).not.toBeInTheDocument();
    } else {
      expect(
        await screen.findByText(/Branvil is stopping/),
      ).toBeInTheDocument();
    }
  },
);
