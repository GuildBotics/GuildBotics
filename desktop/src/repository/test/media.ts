import { vi } from "vitest";

/** The query below which the sidebar stays a rail (`ui/AppShell.tsx`). */
const NARROW = "(max-width: 900px)";

/** Reports a screen narrower than 900px, besides what the test setup reports, until restored. */
export function narrowScreen(): () => void {
  const media = vi.mocked(window.matchMedia);
  const reported = media.getMockImplementation()!;
  media.mockImplementation((query: string) => ({
    ...reported(query),
    matches: query === NARROW || reported(query).matches,
  }));
  return () => media.mockImplementation(reported);
}
