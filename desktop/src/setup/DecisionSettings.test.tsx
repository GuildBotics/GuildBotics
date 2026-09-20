import { MemoryRouter } from "react-router";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { beforeEach, expect, it, vi } from "vitest";
import { checkDecision, getDecisionOptions, saveDecisionCredential } from "../api/client";
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
function mount(unsaved = false) {
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <TestMantineProvider>
        <MemoryRouter>
          <DecisionSettings personId="aiko" unsaved={unsaved} />
        </MemoryRouter>
      </TestMantineProvider>
    </QueryClientProvider>,
  );
}
beforeEach(async () => {
  vi.resetAllMocks();
  await i18n.changeLanguage("en");
  vi.mocked(getDecisionOptions).mockResolvedValue({
    models: ["jev-latest"],
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
