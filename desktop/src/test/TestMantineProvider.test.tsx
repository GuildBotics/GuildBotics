import { Transition } from "@mantine/core";
import { render } from "@testing-library/react";
import { expect, it, vi } from "vitest";

import { TestMantineProvider } from "./TestMantineProvider";

function Fixture({ mounted }: { mounted: boolean }) {
  return (
    <TestMantineProvider>
      <Transition mounted={mounted} duration={250} exitDuration={250}>
        {(styles) => <div data-testid="panel" style={styles} />}
      </Transition>
    </TestMantineProvider>
  );
}

it("runs a transition without scheduling anything that can outlive the test", () => {
  // A transition that opens and closes must leave no pending timer or frame.
  // One that survives fires after Vitest disposes this file's jsdom, and the
  // uncaught error is then reported against whichever file is running.
  vi.useFakeTimers();
  try {
    const { rerender } = render(<Fixture mounted={false} />);

    rerender(<Fixture mounted />);
    rerender(<Fixture mounted={false} />);

    expect(vi.getTimerCount()).toBe(0);
  } finally {
    vi.useRealTimers();
  }
});

it("renders an open transition and drops a closed one", () => {
  const { rerender, queryByTestId } = render(<Fixture mounted />);
  expect(queryByTestId("panel")).toBeInTheDocument();

  rerender(<Fixture mounted={false} />);
  expect(queryByTestId("panel")).not.toBeInTheDocument();
});
