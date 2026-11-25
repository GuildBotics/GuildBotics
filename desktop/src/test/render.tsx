import { createTheme } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import type { PropsWithChildren, ReactNode } from "react";
import { HashRouter, MemoryRouter } from "react-router";
import { TestMantineProvider } from "./TestMantineProvider";

const theme = createTheme({ primaryColor: "dark", defaultRadius: "md" });

type Options = {
  router?: "hash" | "memory";
  initialPath?: string;
  strict?: boolean;
};

export function renderWithProviders(ui: ReactNode, options: Options = {}) {
  const { router, initialPath = "/", strict } = options;
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });

  function Providers({ children }: PropsWithChildren) {
    if (router === "memory") {
      children = <MemoryRouter initialEntries={[initialPath]}>{children}</MemoryRouter>;
    } else if (router === "hash") {
      children = <HashRouter>{children}</HashRouter>;
    }
    return (
      <TestMantineProvider theme={theme}>
        <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
      </TestMantineProvider>
    );
  }

  return { ...render(ui, { wrapper: Providers, reactStrictMode: strict }), queryClient };
}
