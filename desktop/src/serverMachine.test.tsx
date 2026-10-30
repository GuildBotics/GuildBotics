import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { getServerMachineName } from "./api/client";
import i18n from "./i18n";
import "./i18n";
import { ServerMachineBadge } from "./serverMachine";
import { TestMantineProvider } from "./test/TestMantineProvider";

vi.mock("./api/client", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api/client")>()),
  getServerMachineName: vi.fn(),
}));

const t = i18n.getFixedT("en");

function renderBadge() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <TestMantineProvider>
      <QueryClientProvider client={client}>
        <ServerMachineBadge />
      </QueryClientProvider>
    </TestMantineProvider>,
  );
}

describe("ServerMachineBadge", () => {
  it("names the machine the server runs on, which 'this machine' means", async () => {
    vi.mocked(getServerMachineName).mockResolvedValue("studio");
    renderBadge();

    const badge = await screen.findByLabelText(
      t("app.serverMachine.tooltip", { machine: "studio" }),
    );
    expect(badge).toHaveTextContent("studio");
  });

  it("shows nothing while the name is unknown", () => {
    vi.mocked(getServerMachineName).mockReturnValue(new Promise(() => undefined));
    const { container } = renderBadge();

    expect(container.querySelector(".mantine-Badge-root")).toBeNull();
  });
});
