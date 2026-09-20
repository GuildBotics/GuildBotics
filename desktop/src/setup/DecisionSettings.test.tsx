import { MemoryRouter } from "react-router";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { getDecisionOptions } from "../api/client";
import i18n from "../i18n";
import { TestMantineProvider } from "../test/TestMantineProvider";
import { DecisionSettings } from "./DecisionSettings";

vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  getDecisionOptions: vi.fn(),
}));
const t = i18n.getFixedT("en");
function mount(engine: "llm" | "cli" | "jev" | null = "llm") {
  const onApiKeyChange = vi.fn();
  const view = render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TestMantineProvider>
        <MemoryRouter>
          <DecisionSettings
            personId="aiko"
            engine={engine ?? undefined}
            apiKey=""
            onApiKeyChange={onApiKeyChange}
            credentialError={false}
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
  vi.mocked(getDecisionOptions).mockResolvedValue({ credential_present: true });
  const { onApiKeyChange } = mount("jev");
  expect(await screen.findByPlaceholderText("••••••••••••")).toBeInTheDocument();
  expect(screen.getByLabelText(t("decision.key"))).toHaveValue("");
  expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /save|check/i })).not.toBeInTheDocument();
  await userEvent.type(screen.getByLabelText(t("decision.key")), "x");
  expect(onApiKeyChange).toHaveBeenCalledWith("x");
  expect(screen.getByText(t("decision.keySaveHint"))).toBeInTheDocument();
});

it.each(["llm", "cli"] as const)("hides Jev credentials for the draft %s engine", (engine) => {
  mount(engine);
  expect(screen.queryByLabelText(t("decision.key"))).not.toBeInTheDocument();
  expect(screen.queryByText(t("decision.keyMissing"))).not.toBeInTheDocument();
  expect(getDecisionOptions).not.toHaveBeenCalled();
});

it("explains the conservative fallback when no engine is configured", () => {
  mount(null);
  expect(screen.getByText(t("decision.unconfigured"))).toBeInTheDocument();
});
