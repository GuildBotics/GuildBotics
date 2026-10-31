import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Bootstrap } from "./Bootstrap";
import {
  BackendClosedError,
  backendEntry,
  getBootstrapLog,
  letQuitsThrough,
  restartApp,
} from "./api/backend";
import i18n from "./i18n";
import { replacePage } from "./session/pageNavigation";
import { TestMantineProvider } from "./test/TestMantineProvider";
import "./i18n";

const t = i18n.getFixedT("en");

vi.mock("./api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/backend")>()),
  backendEntry: vi.fn(),
  getBootstrapLog: vi.fn(),
  letQuitsThrough: vi.fn(),
  restartApp: vi.fn(),
}));
vi.mock("./session/pageNavigation", () => ({ replacePage: vi.fn() }));

const backendEntryMock = vi.mocked(backendEntry);

function renderBootstrap() {
  return render(
    <TestMantineProvider>
      <Bootstrap />
    </TestMantineProvider>,
  );
}

/** A promise settled by the test. */
function deferred<T>() {
  let resolve: (value: T) => void = () => {};
  const promise = new Promise<T>((done) => {
    resolve = done;
  });
  return { promise, resolve };
}

describe("Bootstrap", () => {
  beforeEach(() => {
    vi.mocked(getBootstrapLog).mockResolvedValue(null);
    vi.mocked(letQuitsThrough).mockReturnValue(() => undefined);
  });

  afterEach(async () => {
    window.history.replaceState(null, "", "/");
    await i18n.changeLanguage("en");
  });

  it("waits for the backend, then goes to the server's page through the entry link", async () => {
    window.history.replaceState(null, "", "/bootstrap.html?next=/quick.html");
    const entry = deferred<string>();
    backendEntryMock.mockReturnValue(entry.promise);

    renderBootstrap();

    expect(screen.getByText(t("app.loading.title"))).toBeInTheDocument();
    expect(backendEntryMock).toHaveBeenCalledWith("/quick.html");
    entry.resolve("http://127.0.0.1:8765/-/enter?next=/quick.html#code");
    await waitFor(() =>
      expect(replacePage).toHaveBeenCalledWith(
        "http://127.0.0.1:8765/-/enter?next=/quick.html#code",
      ),
    );
  });

  it("opens the App when no page is named", () => {
    window.history.replaceState(null, "", "/bootstrap.html");
    backendEntryMock.mockReturnValue(new Promise(() => undefined));

    renderBootstrap();

    expect(backendEntryMock).toHaveBeenCalledWith("/");
  });

  it("lets quits through, since nothing here can be cut off", () => {
    const stop = vi.fn();
    vi.mocked(letQuitsThrough).mockReturnValue(stop);
    backendEntryMock.mockReturnValue(new Promise(() => undefined));

    const view = renderBootstrap();
    expect(letQuitsThrough).toHaveBeenCalledTimes(1);

    view.unmount();
    expect(stop).toHaveBeenCalledTimes(1);
  });

  it("shows why the backend closed, with the startup log and a restart", async () => {
    backendEntryMock.mockRejectedValue(new BackendClosedError({ reason: "timeout", detail: "45" }));
    vi.mocked(getBootstrapLog).mockResolvedValue({
      path: "/logs/bootstrap.log",
      tail: "boot failed at step 3",
    });
    const user = userEvent.setup();

    renderBootstrap();

    expect(await screen.findByText(t("app.loading.closed"))).toBeInTheDocument();
    expect(
      screen.getByText(t("app.loading.reasons.timeout", { detail: "45" })),
    ).toBeInTheDocument();
    expect(await screen.findByText("/logs/bootstrap.log")).toBeInTheDocument();
    expect(screen.getByText("boot failed at step 3")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: t("app.loading.restart") }));
    expect(restartApp).toHaveBeenCalled();
    expect(replacePage).not.toHaveBeenCalled();
  });

  it("shows a failure that is not the host's closure as it is", async () => {
    backendEntryMock.mockRejectedValue(new Error("command backend_entry not found"));

    renderBootstrap();

    expect(await screen.findByText("Error: command backend_entry not found")).toBeInTheDocument();
  });

  it.each([
    ["en", "cli_managed"],
    ["en", "backend_in_use"],
    ["ja", "cli_managed"],
    ["ja", "backend_in_use"],
  ])("explains the backend owner conflict and next action: %s / %s", async (language, reason) => {
    await i18n.changeLanguage(language);
    const localized = i18n.getFixedT(language);
    const message = localized(`app.loading.reasons.${reason}`);
    const restart = localized("app.loading.restart");
    backendEntryMock.mockRejectedValue(new BackendClosedError({ reason, detail: "" }));
    renderBootstrap();
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(message).toContain(restart);
    expect(message).not.toContain("`");
    if (reason === "cli_managed") expect(message).toContain("guildbotics stop");
    await userEvent.setup().click(screen.getByRole("button", { name: restart }));
    expect(restartApp).toHaveBeenCalledTimes(1);
    expect(replacePage).not.toHaveBeenCalled();
  });

  it("does not leave a page that was closed meanwhile", async () => {
    const entry = deferred<string>();
    backendEntryMock.mockReturnValue(entry.promise);

    const view = renderBootstrap();
    view.unmount();
    entry.resolve("http://127.0.0.1:8765/-/enter#code");
    await entry.promise;

    expect(replacePage).not.toHaveBeenCalled();
  });
});
