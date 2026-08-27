import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiRequestError } from "../api/client";
import { RequestErrorAlert } from "./RequestErrorAlert";

function renderAlert(cause: unknown) {
  return render(
    <MantineProvider>
      <RequestErrorAlert cause={cause} title="It failed" />
    </MantineProvider>,
  );
}

describe("RequestErrorAlert", () => {
  it("renders nothing while there is no failure", () => {
    renderAlert(null);

    expect(screen.queryByText("It failed")).toBeNull();
  });

  it("shows the backend's sentence verbatim, with its diagnostic detail", () => {
    // The backend already rendered the sentence in the screen's language;
    // re-wording it here would eventually say something different.
    renderAlert(
      new ApiRequestError({
        code: "secret_publish_failed",
        message: "Hub マシンは値を受け取りましたが、記録に失敗しました。",
        context: { detail: "[Errno 13] Permission denied" },
      }),
    );

    expect(
      screen.getByText("Hub マシンは値を受け取りましたが、記録に失敗しました。"),
    ).toBeInTheDocument();
    expect(screen.getByText("[Errno 13] Permission denied")).toBeInTheDocument();
  });

  it("still says something for a failure that is not an API error", () => {
    renderAlert(new Error("connection refused"));

    expect(screen.getByText("Error: connection refused")).toBeInTheDocument();
  });
});
