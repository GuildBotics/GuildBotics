import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Bootstrap } from "./Bootstrap";
import {
  BackendClosedError,
  canRestartApp,
  getBootstrapLog,
  onBackendClosed,
  restartApp,
  startBackend,
} from "./api/backend";
import i18n from "./i18n";
import { TestMantineProvider } from "./test/TestMantineProvider";
import "./i18n";

const t = i18n.getFixedT("en");

vi.mock("./api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/backend")>()),
  getBootstrapLog: vi.fn(async () => null),
  startBackend: vi.fn(async () => undefined),
  onBackendClosed: vi.fn(() => () => undefined),
  canRestartApp: vi.fn(() => false),
  restartApp: vi.fn(async () => undefined),
}));

vi.mock("./App", () => ({
  App: () => <div>App Mock Loaded</div>,
}));

const startBackendMock = vi.mocked(startBackend);
const getBootstrapLogMock = vi.mocked(getBootstrapLog);
const onBackendClosedMock = vi.mocked(onBackendClosed);
const canRestartAppMock = vi.mocked(canRestartApp);

function renderBootstrap() {
  return render(
    <TestMantineProvider>
      <Bootstrap />
    </TestMantineProvider>,
  );
}

describe("Bootstrap", () => {
  beforeEach(() => {
    startBackendMock.mockReset();
    getBootstrapLogMock.mockReset().mockResolvedValue(null);
    canRestartAppMock.mockReturnValue(false);
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
    getBootstrapLogMock.mockResolvedValue({
      path: "/logs/bootstrap.log",
      tail: "sidecar stderr",
    });

    renderBootstrap();

    expect(await screen.findByText(t("app.loading.failed"))).toBeInTheDocument();
    expect(screen.getByText(/backend exploded/)).toBeInTheDocument();
    expect(screen.getByText(/\/logs\/bootstrap\.log/)).toBeInTheDocument();
    expect(screen.getByText("sidecar stderr")).toBeInTheDocument();
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

  it("leaves the App for the failure screen once the backend stops", async () => {
    let close: (error: BackendClosedError) => void = () => {};
    const stopWatching = vi.fn();
    onBackendClosedMock.mockImplementation((listener) => {
      close = listener;
      return stopWatching;
    });
    getBootstrapLogMock.mockResolvedValue({ path: "/logs/bootstrap.log", tail: "exited" });

    const { unmount } = renderBootstrap();
    await screen.findByText("App Mock Loaded");
    close(new BackendClosedError({ reason: "exited", detail: "code 137" }));

    expect(await screen.findByText(t("app.loading.stopped"))).toBeInTheDocument();
    expect(
      screen.getByText(t("app.loading.reasons.exited", { detail: "code 137" })),
    ).toBeInTheDocument();
    expect(screen.queryByText("App Mock Loaded")).not.toBeInTheDocument();
    unmount();
    expect(stopWatching).toHaveBeenCalled();
  });

  it("offers to restart the app in Desktop, where retrying cannot bring the backend back", async () => {
    const user = userEvent.setup();
    canRestartAppMock.mockReturnValue(true);
    startBackendMock.mockRejectedValue(new BackendClosedError({ reason: "timeout", detail: "45" }));

    renderBootstrap();

    expect(
      await screen.findByText(t("app.loading.reasons.timeout", { detail: "45" })),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: t("app.loading.retry") })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("app.loading.restart") }));
    expect(restartApp).toHaveBeenCalled();
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
