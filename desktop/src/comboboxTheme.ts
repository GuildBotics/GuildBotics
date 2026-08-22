import type { MantineThemeComponents } from "@mantine/core";

/**
 * Keep an open dropdown on screen when the page moves under it.
 *
 * Mantine hides an open dropdown with `display: none` as soon as floating-ui
 * reports the trigger as clipped (`hideDetached`, on by default). Nothing
 * brings it back: the observers that would notice the trigger returning do not
 * fire on a `display: none` element, so a dropdown opened just before a layout
 * shift stays invisible while the trigger goes on announcing
 * `aria-expanded="true"`. The user sees a control that says it is open and no
 * list, and a screen reader is pointed at a list that is not rendered.
 *
 * A dropdown still anchored to a trigger that scrolled away is the smaller
 * problem, and it is the one every other component here already has. Escape
 * and clicking outside still close it.
 */
export const comboboxThemeComponents: MantineThemeComponents = {
  Select: { defaultProps: { comboboxProps: { hideDetached: false } } },
  MultiSelect: { defaultProps: { comboboxProps: { hideDetached: false } } },
  TagsInput: { defaultProps: { comboboxProps: { hideDetached: false } } },
};
