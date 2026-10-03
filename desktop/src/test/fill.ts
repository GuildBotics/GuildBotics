import type { userEvent } from "@testing-library/user-event";

type User = ReturnType<typeof userEvent.setup>;

/**
 * Enters `text` into a field the way a user who pastes it would.
 *
 * The field is focused with a click and receives the text as one `input`
 * event, so a controlled component re-renders once instead of once per
 * character. `user.type` dispatches a keydown/input/keyup per character, and a
 * large form re-renders on each of them: on the setup page that put single
 * tests at several seconds. Use `user.type` only where the keystrokes are what
 * the test is about (key handlers, Enter, IME composition, typing in progress).
 */
export async function fill(user: User, element: HTMLElement, text: string): Promise<void> {
  await user.click(element);
  await user.paste(text);
}
