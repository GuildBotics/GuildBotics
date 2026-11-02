// Driving Mantine's combobox and menus in tests the way a person does: open,
// then pick by the visible name.

import { screen, within } from "@testing-library/react";
import type { UserEvent } from "@testing-library/user-event";

/** Chooses `option` in the Select labelled `label` (optionally inside `scope`). */
export async function choose(user: UserEvent, label: string | RegExp, option: string | RegExp, scope?: HTMLElement) {
  const input = await (scope ? within(scope) : screen).findByRole("combobox", { name: label });
  await user.click(input);
  await user.click(await screen.findByRole("option", { name: option }));
}

/** Opens the menu behind the button named `trigger` and clicks its item `item`. */
export async function pickFromMenu(user: UserEvent, trigger: string | RegExp, item: string | RegExp, scope?: HTMLElement) {
  await user.click((scope ? within(scope) : screen).getByRole("button", { name: trigger }));
  await user.click(await screen.findByRole("menuitem", { name: item }));
}
