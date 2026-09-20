import { useState } from "react";
import { MemoryRouter } from "react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import {
  checkDecision,
  getDecisionOptions,
  getDecisionStatus,
  saveDecisionCredential,
  type DecisionConfig,
} from "../api/client";
import i18n from "../i18n";
import { TestMantineProvider } from "../test/TestMantineProvider";
import { DecisionSettings } from "./DecisionSettings";

vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  checkDecision: vi.fn(),
  getDecisionOptions: vi.fn(),
  getDecisionStatus: vi.fn(),
  saveDecisionCredential: vi.fn(),
}));
const t = i18n.getFixedT("en");
const config: DecisionConfig = { engine: "jev", provider: "", model: "jev-1.13.0" };
const ready = {
  available: true,
  state: "unverified",
  reason: "unchecked",
  recovery: "credentials" as const,
};
const missing = { ...ready, available: false, state: "missing", reason: "Register credentials" };
const valid = vi.fn();

function Harness({ initial = config }: { initial?: DecisionConfig }) {
  const [value, setValue] = useState(initial);
  return <DecisionSettings value={value} onChange={setValue} onValidity={valid} />;
}
function mount(initial = config) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TestMantineProvider>
        <MemoryRouter>
          <Harness initial={initial} />
        </MemoryRouter>
      </TestMantineProvider>
    </QueryClientProvider>,
  );
}
beforeEach(async () => {
  vi.resetAllMocks();
  await i18n.changeLanguage("en");
  vi.mocked(getDecisionOptions).mockResolvedValue({
    selected: config,
    options: [
      { ...ready, engine: "jev", provider: "", models: [config.model] },
      { ...ready, engine: "agno", provider: "openai", models: [] },
    ],
  });
  vi.mocked(getDecisionStatus).mockResolvedValue(ready);
});

it("retains an unavailable saved selection and offers registration without selecting it", async () => {
  vi.mocked(getDecisionOptions).mockResolvedValue({
    selected: config,
    options: [{ ...missing, engine: "jev", provider: "", models: [config.model] }],
  });
  vi.mocked(getDecisionStatus).mockResolvedValue(missing);
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("Register credentials");
  expect(screen.getByRole("combobox", { name: t("decision.model") })).toHaveValue(config.model);
  expect(screen.getByLabelText(t("decision.key"))).toBeEnabled();
  await waitFor(() => expect(valid).toHaveBeenLastCalledWith(false));
});

it("does not treat typed text as registered and refreshes only after storage succeeds", async () => {
  vi.mocked(getDecisionStatus).mockResolvedValue(missing);
  vi.mocked(saveDecisionCredential).mockImplementation(async () => {
    vi.mocked(getDecisionStatus).mockResolvedValue(ready);
    return ready;
  });
  mount();
  const user = userEvent.setup();
  await user.type(screen.getByLabelText(t("decision.key")), "test-key");
  expect(saveDecisionCredential).not.toHaveBeenCalled();
  await user.click(screen.getByRole("button", { name: t("decision.register") }));
  await waitFor(() => expect(valid).toHaveBeenLastCalledWith(true));
  expect(screen.getByLabelText(t("decision.key"))).toHaveValue("");
  expect(screen.getByText(/authentication unchecked/)).toBeVisible();
});

it("clears the model when the engine changes", async () => {
  mount();
  const user = userEvent.setup();
  await waitFor(() => expect(getDecisionOptions).toHaveBeenCalled());
  await user.click(screen.getByRole("combobox", { name: t("decision.engine") }));
  await user.click(screen.getByRole("option", { name: "Agno / openai" }));
  expect(screen.getByRole("textbox", { name: t("decision.model") })).toHaveValue("");
});

it.each(["authentication_error", "connection_error"])(
  "shows %s while retaining the selected model",
  async (state) => {
    vi.mocked(checkDecision).mockResolvedValue({
      status: { ...ready, state, reason: "retry connection" },
      models: [],
    });
    mount();
    await userEvent.click(screen.getByRole("button", { name: t("decision.check") }));
    expect(await screen.findByText(/retry connection/)).toBeVisible();
    expect(screen.getByRole("combobox", { name: t("decision.model") })).toHaveValue(config.model);
    expect(saveDecisionCredential).not.toHaveBeenCalled();
  },
);

it("reports a model error from the backend and prevents saving", async () => {
  vi.mocked(getDecisionStatus).mockResolvedValue({
    ...missing,
    state: "invalid",
    reason: "model mismatch",
    recovery: "model",
  });
  mount();
  expect(await screen.findByText(/model mismatch/)).toBeVisible();
  await waitFor(() => expect(valid).toHaveBeenLastCalledWith(false));
});

it("refreshes a removed credential after a successful connection check", async () => {
  vi.mocked(checkDecision).mockResolvedValue({
    status: { ...ready, state: "verified" },
    models: [config.model],
  });
  mount();
  await userEvent.click(screen.getByRole("button", { name: t("decision.check") }));
  expect(await screen.findByText(/Connection verified/)).toBeVisible();
  vi.mocked(getDecisionStatus).mockResolvedValue(missing);
  await userEvent.click(screen.getByRole("button", { name: t("decision.retry") }));
  await waitFor(() => expect(valid).toHaveBeenLastCalledWith(false));
  expect(screen.getByRole("combobox", { name: t("decision.model") })).toHaveValue(config.model);
  expect(screen.getByRole("alert")).toHaveTextContent("Not registered");
});

it("shows Japanese registration and connection states", async () => {
  await i18n.changeLanguage("ja");
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent("登録済み・認証未確認");
  expect(screen.getByLabelText("Jev API キー")).toBeVisible();
  expect(screen.getByRole("button", { name: "Jev の認証情報を保存" })).toBeDisabled();
});
