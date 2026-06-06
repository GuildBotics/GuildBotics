import { MantineProvider } from "@mantine/core";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Bootstrap } from "./Bootstrap";
import { startBackend } from "./api/backend";
import i18n from "./i18n";
import "./i18n";

const t = i18n.getFixedT("en");

vi.mock("./api/backend", () => ({
  startBackend: vi.fn(async () => undefined),
}));

vi.mock("./App", () => ({
  App: () => <div>App Mock Loaded</div>,
}));

const startBackendMock = vi.mocked(startBackend);

function renderBootstrap() {
  return render(
    <MantineProvider>
      <Bootstrap />
    </MantineProvider>,
  );
}

describe("Bootstrap", () => {
  beforeEach(() => {
    startBackendMock.mockReset();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows the loading indicator while the backend starts", async () => {
    let resolveStart: () => void = () => {};
    startBackendMock.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveStart = resolve;
      }),
    );

    renderBootstrap();

    expect(screen.getByText(t("app.loading.title"))).toBeInTheDocument();
    expect(screen.getByText(t("app.loading.body"))).toBeInTheDocument();
    expect(screen.queryByText("App Mock Loaded")).not.toBeInTheDocument();

    resolveStart();
    await screen.findByText("App Mock Loaded");
  });

  it("renders the App when startBackend resolves", async () => {
    startBackendMock.mockResolvedValue(undefined);

    renderBootstrap();

    expect(await screen.findByText("App Mock Loaded")).toBeInTheDocument();
    expect(screen.queryByText(t("app.loading.title"))).not.toBeInTheDocument();
  });

  it("shows an error alert with a retry button when startBackend fails", async () => {
    startBackendMock.mockRejectedValue(new Error("backend exploded"));

    renderBootstrap();

    expect(await screen.findByText(t("app.loading.failed"))).toBeInTheDocument();
    expect(screen.getByText(/backend exploded/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: t("app.loading.retry") })).toBeInTheDocument();
    expect(screen.queryByText("App Mock Loaded")).not.toBeInTheDocument();
  });

  it("returns to loading on retry and can then succeed", async () => {
    const user = userEvent.setup();
    startBackendMock.mockRejectedValueOnce(new Error("first failure"));

    renderBootstrap();

    const retry = await screen.findByRole("button", { name: t("app.loading.retry") });

    let resolveRetry: () => void = () => {};
    startBackendMock.mockReturnValueOnce(
      new Promise<void>((resolve) => {
        resolveRetry = resolve;
      }),
    );

    await user.click(retry);

    expect(await screen.findByText(t("app.loading.title"))).toBeInTheDocument();
    expect(screen.queryByText(t("app.loading.failed"))).not.toBeInTheDocument();

    resolveRetry();

    expect(await screen.findByText("App Mock Loaded")).toBeInTheDocument();
  });

  it("does not update state after unmount when startBackend resolves late", async () => {
    const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
    let resolveStart: () => void = () => {};
    startBackendMock.mockReturnValue(
      new Promise<void>((resolve) => {
        resolveStart = resolve;
      }),
    );

    const { unmount } = renderBootstrap();
    expect(screen.getByText(t("app.loading.title"))).toBeInTheDocument();

    unmount();
    resolveStart();

    // Allow the resolved promise microtask + any scheduled work to flush.
    await waitFor(() => {
      expect(startBackendMock).toHaveBeenCalledTimes(1);
    });

    expect(
      errorSpy.mock.calls.some((call) =>
        String(call[0]).includes("state update on an unmounted component"),
      ),
    ).toBe(false);
    errorSpy.mockRestore();
  });
});
