import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ApiRequestError } from "../api/client";
import i18n from "../i18n";
import { RequestErrorAlert } from "./RequestErrorAlert";

const t = i18n.getFixedT("en");

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

  it("localizes a failure by its code", () => {
    renderAlert(
      new ApiRequestError({
        code: "secret_publish_failed",
        message: "The backend's own sentence.",
        context: { detail: "[Errno 13] Permission denied" },
      }),
    );

    expect(screen.getByText(t("apiErrors.secret_publish_failed"))).toBeInTheDocument();
    expect(screen.queryByText("The backend's own sentence.")).toBeNull();
    expect(screen.getByText("[Errno 13] Permission denied")).toBeInTheDocument();
  });

  it("falls back to the backend's sentence for a code without a translation", () => {
    renderAlert(
      new ApiRequestError({
        code: "something_new",
        message: "The backend's own sentence.",
        context: {},
      }),
    );

    expect(screen.getByText("The backend's own sentence.")).toBeInTheDocument();
  });

  it("still says something for a failure that is not an API error", () => {
    renderAlert(new Error("connection refused"));

    expect(screen.getByText("Error: connection refused")).toBeInTheDocument();
  });
});
