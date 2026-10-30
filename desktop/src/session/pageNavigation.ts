// Leaving this document for another one. Kept apart so tests can stand in for
// the browser's navigation, which jsdom does not perform.

/** Go to `url`, leaving this page in the history. */
export function leavePage(url: string) {
  window.location.assign(url);
}

/** Go to `url` in place of this page, which the history then forgets. */
export function replacePage(url: string) {
  window.location.replace(url);
}
