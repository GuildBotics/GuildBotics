import { afterEach, describe, expect, it, vi } from "vitest";

import { requestNavigation, setNavigationGuard } from "./navigationGuard";

afterEach(() => {
  setNavigationGuard(null);
});

describe("navigationGuard", () => {
  it("proceeds immediately when no guard is registered", () => {
    const proceed = vi.fn();
    requestNavigation(proceed);
    expect(proceed).toHaveBeenCalledTimes(1);
  });

  it("proceeds immediately when the guard does not block", () => {
    const confirm = vi.fn();
    setNavigationGuard({ shouldBlock: () => false, confirm });
    const proceed = vi.fn();

    requestNavigation(proceed);

    expect(proceed).toHaveBeenCalledTimes(1);
    expect(confirm).not.toHaveBeenCalled();
  });

  it("delegates to the guard's confirm when blocking", () => {
    const confirm = vi.fn();
    setNavigationGuard({ shouldBlock: () => true, confirm });
    const proceed = vi.fn();

    requestNavigation(proceed);

    // The guard owns whether/when to proceed; it is not called automatically.
    expect(proceed).not.toHaveBeenCalled();
    expect(confirm).toHaveBeenCalledWith(proceed);
  });

  it("clears the guard when set to null", () => {
    setNavigationGuard({ shouldBlock: () => true, confirm: vi.fn() });
    setNavigationGuard(null);
    const proceed = vi.fn();

    requestNavigation(proceed);

    expect(proceed).toHaveBeenCalledTimes(1);
  });
});
