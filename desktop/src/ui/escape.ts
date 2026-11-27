/**
 * Whether Escape belongs to where it is pressed: a field being typed in (a
 * title's edit cancels itself), or what Mantine marks as its own (an open
 * dropdown). Closing a drawer or menu there would lose what was typed. Of inputs,
 * only those typed in have a selection (not a checkbox's chip).
 */
export const ownsEscape = (el: HTMLElement) =>
  ((el instanceof HTMLTextAreaElement ||
    (el instanceof HTMLInputElement && el.selectionStart !== null)) &&
    !el.readOnly) ||
  el.dataset.mantineStopPropagation === "true";
