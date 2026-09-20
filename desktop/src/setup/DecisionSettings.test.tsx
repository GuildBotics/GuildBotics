import { MemoryRouter } from "react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { getDecisionOptions, type ChatDecisionSelection } from "../api/client";
import i18n from "../i18n";
import { TestMantineProvider } from "../test/TestMantineProvider";
import { DecisionSettings } from "./DecisionSettings";

vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  getDecisionOptions: vi.fn(),
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
function mount(
  engine: "llm" | "cli" | "jev" = "llm",
  selection: ChatDecisionSelection | null = saved,
) {
  const onApiKeyChange = vi.fn();
  const view = render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TestMantineProvider>
        <MemoryRouter>
          <DecisionSettings
            personId="aiko"
            engine={engine}
            apiKey=""
            onApiKeyChange={onApiKeyChange}
            credentialError={false}
            selection={selection}
          />
        </MemoryRouter>
      </TestMantineProvider>
    </QueryClientProvider>,
  );
  return { ...view, onApiKeyChange };
}
beforeEach(async () => {
  vi.resetAllMocks();
  await i18n.changeLanguage("en");
  vi.mocked(getDecisionOptions).mockResolvedValue({
    credential_present: false,
  });
});

it("shows credential input for the draft Jev selection without action buttons", async () => {
  const { onApiKeyChange } = mount("jev");
  expect(await screen.findByText(t("decision.keyMissing"))).toBeInTheDocument();
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /save|check/i })).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(t("decision.key")), "x");
  expect(onApiKeyChange).toHaveBeenCalledWith("x");
  expect(screen.getByText(t("decision.keySaveHint"))).toBeInTheDocument();
});

it.each(["llm", "cli"] as const)("hides Jev credentials for the draft %s engine", (engine) => {
  mount(engine, { ...saved, engine: "jev", model: "jev-latest" });
  expect(screen.queryByLabelText(t("decision.key"))).not.toBeInTheDocument();
  expect(screen.queryByText(t("decision.keyMissing"))).not.toBeInTheDocument();
  expect(getDecisionOptions).not.toHaveBeenCalled();
});

it("shows the saved assignment without calling a model, including while editing", () => {
  mount("jev");
  expect(
    screen.getByText(t("decision.savedEngine", { value: "LLM / openai" })),
  ).toBeInTheDocument();
  expect(screen.getByText(t("decision.savedSlot", { value: "judge" }))).toBeInTheDocument();
  expect(screen.getByText(t("decision.savedModel", { value: "saved-model" }))).toBeInTheDocument();
  expect(screen.getByText(t("decision.inheritedAssignment"))).toBeInTheDocument();
});

it("distinguishes CLI provider defaults from a missing slot", () => {
  mount("cli", {
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
  mount("cli", { ...saved, engine: "jev", provider: "", slot: "", model: "jev-latest" });
  expect(screen.getByText(t("decision.savedEngine", { value: "Jev" }))).toBeInTheDocument();
  expect(screen.getByText(t("decision.savedModel", { value: "jev-latest" }))).toBeInTheDocument();
  expect(screen.queryByText(t("decision.savedSlot", { value: "judge" }))).not.toBeInTheDocument();
});

it("identifies an unresolved slot", () => {
  mount("cli", { ...saved, engine: "cli", provider: "", model: "", resolved: false });
  expect(screen.getByText(t("decision.unresolvedSlot"))).toBeInTheDocument();
  expect(
    screen.queryByText(t("decision.cliDefaultModel"), { exact: false }),
  ).not.toBeInTheDocument();
});
