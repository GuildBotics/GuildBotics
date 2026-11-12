import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { getRunFiles } from "../api/client";
import { TestMantineProvider } from "../test/TestMantineProvider";
import "../i18n";
import { RunFiles } from "./RunFiles";

vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  getRunFiles: vi.fn(async () => ({ files: ["nested/report.pdf"] })),
}));

it("offers a download only after the run finishes", async () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = (finished: boolean) => (
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <RunFiles traceId="run-id" finished={finished} />
      </TestMantineProvider>
    </QueryClientProvider>
  );
  const { rerender } = render(view(false));
  expect(getRunFiles).not.toHaveBeenCalled();
  rerender(view(true));
  const link = await screen.findByRole("link", { name: "nested/report.pdf" });
  expect(link).toHaveAttribute(
    "href",
    "/-/guildbotics/api/diagnostics/traces/run-id/file?path=nested%2Freport.pdf",
  );
  expect(link).toHaveAttribute("download", "report.pdf");
});

it("shows oversized outputs without a download link", async () => {
  vi.mocked(getRunFiles).mockResolvedValue({ files: [], oversized: ["large.bin"] });
  const client = new QueryClient();
  render(
    <QueryClientProvider client={client}>
      <TestMantineProvider>
        <RunFiles traceId="large" finished />
      </TestMantineProvider>
    </QueryClientProvider>,
  );
  expect(await screen.findByText(/large.bin/)).toBeInTheDocument();
  expect(screen.queryByRole("link")).not.toBeInTheDocument();
});
