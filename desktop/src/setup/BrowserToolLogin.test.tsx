import { StrictMode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, it, expect, vi } from "vitest";
import { loginCliAgent } from "../api/client";
import i18n from "../i18n";
import { TestMantineProvider } from "../test/TestMantineProvider";
import { BrowserToolLogin } from "./BrowserToolLogin";
vi.mock("../api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/client")>()),
  loginCliAgent: vi.fn(),
}));
describe("private login dialog", () => {
  it.each(["en", "ja"])(
    "relays prompts and codes, refreshes status, and cancels on close in %s",
    async (language) => {
      await i18n.changeLanguage(language);
      const t = i18n.getFixedT(language);
      const send = vi.fn(),
        close = vi.fn();
      vi.mocked(loginCliAgent).mockReturnValue({ send, close });
      const query = new QueryClient();
      const invalidate = vi.spyOn(query, "invalidateQueries");
      const user = userEvent.setup();
      const view = render(
        <StrictMode>
          <QueryClientProvider client={query}>
            <TestMantineProvider>
              <BrowserToolLogin tool="claude" />
            </TestMantineProvider>
          </QueryClientProvider>
        </StrictMode>,
      );
      await user.click(
        screen.getByRole("button", { name: t("setup.intelligence.environment.login") }),
      );
      const [, output, done] = vi.mocked(loginCliAgent).mock.lastCall!;
      const { act } = await import("@testing-library/react");
      act(() => {
        output("\x1b[94");
        output("mhttps://example.test/authorize\x1b[0m\nDevice code: synthetic-code");
      });
      expect(screen.getByRole("link", { name: "https://example.test/authorize" })).toHaveAttribute(
        "href",
        "https://example.test/authorize",
      );
      expect(screen.queryByText(/94m/)).not.toBeInTheDocument();
      expect(screen.getByText(/https:\/\/example.test/)).toBeInTheDocument();
      const field = screen.getByLabelText(t("setup.intelligence.environment.loginCode"));
      await user.type(field, "synthetic-answer");
      await user.click(
        screen.getByRole("button", { name: t("setup.intelligence.environment.loginSend") }),
      );
      expect(send).toHaveBeenCalledWith("synthetic-answer");
      expect(field).toHaveValue("");
      act(() => done(true));
      expect(screen.getByRole("status")).toHaveTextContent(
        t("setup.intelligence.environment.loginSaved"),
      );
      await waitFor(() =>
        expect(invalidate).toHaveBeenCalledWith({ queryKey: ["agent-environment-status"] }),
      );
      expect(invalidate).toHaveBeenCalledWith({ queryKey: ["system-alerts"] });
      act(() => done(false, "Build the agent environment first."));
      expect(screen.getByRole("alert")).toHaveTextContent("Build the agent environment first.");
      view.unmount();
      expect(close).toHaveBeenCalled();
    },
  );
});
