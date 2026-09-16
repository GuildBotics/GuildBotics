import { MantineProvider, type MantineProviderProps } from "@mantine/core";

/**
 * The Mantine provider every component test renders under.
 *
 * It exists so that "what Mantine does under test" is decided once. Two
 * settings are forced here and must not be overridden per file:
 *
 * - `respectReducedMotion`, together with the `prefers-reduced-motion` media
 *   query that `src/test/setup.ts` reports as matching, makes every transition
 *   zero-duration. Mantine's `useTransition` then changes state synchronously
 *   instead of chaining `requestAnimationFrame` into a `setTimeout`. Those
 *   timers are the reason a test suite that passes can still fail: a dropdown
 *   left open at the end of a file leaves one pending, and when it fires after
 *   Vitest has disposed that file's jsdom, React reaches for a `window` that is
 *   gone. The file Vitest blames is whichever one happened to be running, not
 *   the one that scheduled it.
 * - `env="test"`, which Mantine reads to render transitions in their final
 *   state. It does not stop the timers on its own -- `useTransition` runs
 *   before the check -- so it is not a substitute for the setting above.
 */
export function TestMantineProvider({ theme, ...props }: MantineProviderProps) {
  return <MantineProvider {...props} theme={{ ...theme, respectReducedMotion: true }} env="test" />;
}
