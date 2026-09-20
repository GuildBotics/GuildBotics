import { MemoryRouter } from "react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import {
  checkDecision,
  getDecisionOptions,
  saveDecisionCredential,
  type ChatDecisionSelection,
} from "../api/client";
import i18n from "../i18n";
import { TestMantineProvider } from "../test/TestMantineProvider";
import { DecisionSettings } from "./DecisionSettings";

vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  checkDecision: vi.fn(),
  getDecisionOptions: vi.fn(),
  saveDecisionCredential: vi.fn(),
}));
const t = i18n.getFixedT("en");
const saved: ChatDecisionSelection = {
  engine: "llm",
  provider: "openai",
  slot: "judge",
  model: "saved-model",
  assignment_inherited: true,
  resolved: true,
};
function mount(unsaved = false, selection: ChatDecisionSelection | null = saved) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TestMantineProvider>
        <MemoryRouter>
          <DecisionSettings personId="aiko" unsaved={unsaved} selection={selection} />
        </MemoryRouter>
      </TestMantineProvider>
    </QueryClientProvider>,
  );
}
beforeEach(async () => {
  vi.resetAllMocks();
  await i18n.changeLanguage("en");
  vi.mocked(getDecisionOptions).mockResolvedValue({
    credential_present: false,
  });
});

it("prepares Jev credentials without a second engine picker", async () => {
  vi.mocked(saveDecisionCredential).mockResolvedValue({ state: "unverified" });
  mount();
  expect(await screen.findByText(t("decision.keyMissing"))).toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(t("decision.key")), "private-test-key");
  await userEvent.click(screen.getByRole("button", { name: t("decision.register") }));
  await waitFor(() =>
    expect(saveDecisionCredential).toHaveBeenCalledWith("private-test-key", expect.anything()),
  );
  await waitFor(() => expect(screen.getByLabelText(t("decision.key"))).toHaveValue(""));
});

it("checks the saved feature and displays the effective model", async () => {
  vi.mocked(checkDecision).mockResolvedValue({ state: "verified", model: "actual-model" });
  mount();
  await userEvent.click(screen.getByRole("button", { name: t("decision.check") }));
  await waitFor(() =>
    expect(checkDecision).toHaveBeenCalledWith({ brain: "chat_decision" }, "aiko"),
  );
  expect(await screen.findByText(/actual-model/)).toBeInTheDocument();
});

it("does not test saved configuration as though it were an unsaved draft", () => {
  mount(true);
  expect(screen.getByRole("button", { name: t("decision.check") })).toBeDisabled();
  expect(screen.getByText(t("decision.saveBeforeCheck"))).toBeInTheDocument();
});

it("shows the saved assignment without calling a model, including while editing", () => {
  mount(true);
  expect(
    screen.getByText(t("decision.savedEngine", { value: "LLM / openai" })),
  ).toBeInTheDocument();
  expect(screen.getByText(t("decision.savedSlot", { value: "judge" }))).toBeInTheDocument();
  expect(screen.getByText(t("decision.savedModel", { value: "saved-model" }))).toBeInTheDocument();
  expect(screen.getByText(t("decision.inheritedAssignment"))).toBeInTheDocument();
  expect(checkDecision).not.toHaveBeenCalled();
});

it("distinguishes CLI provider defaults from a missing slot", () => {
  mount(false, {
    ...saved,
    engine: "cli",
    provider: "codex",
    model: "",
    assignment_inherited: false,
  });
  expect(
    screen.getByText(t("decision.savedEngine", { value: "AI CLI / codex" })),
  ).toBeInTheDocument();
  expect(
    screen.getByText(t("decision.savedModel", { value: t("decision.cliDefaultModel") })),
  ).toBeInTheDocument();
  expect(screen.getByText(t("decision.memberAssignment"))).toBeInTheDocument();
});

it("shows a direct Jev model without implying it uses a slot", () => {
  mount(false, { ...saved, engine: "jev", provider: "", slot: "", model: "jev-latest" });
  expect(screen.getByText(t("decision.savedEngine", { value: "Jev" }))).toBeInTheDocument();
  expect(screen.getByText(t("decision.savedModel", { value: "jev-latest" }))).toBeInTheDocument();
  expect(screen.queryByText(t("decision.savedSlot", { value: "judge" }))).not.toBeInTheDocument();
});

it("identifies an unresolved slot and prevents checking it", () => {
  mount(false, { ...saved, engine: "cli", provider: "", model: "", resolved: false });
  expect(screen.getByText(t("decision.unresolvedSlot"))).toBeInTheDocument();
  expect(screen.getByRole("button", { name: t("decision.check") })).toBeDisabled();
  expect(
    screen.queryByText(t("decision.cliDefaultModel"), { exact: false }),
  ).not.toBeInTheDocument();
});
