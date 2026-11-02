// Scrolling while a card is dragged. A browser scrolls a box under a native
// drag only when the pointer lingers in a thin band at its edge, and slowly,
// so the board's lanes and the relations figure barely move. The page scrolls
// the box itself instead: near an edge, faster the nearer.

/** How far from a box's edge scrolling starts, in pixels. */
const BAND = 64;
/** How fast a box scrolls with the pointer on its edge, in pixels per millisecond. */
const MAX_SPEED = 1.5;
/** A drag that has not been over the page for this long has left it. */
const IDLE_MS = 1000;

/** How far one axis of a box from `start` to `end` scrolls in `ms` with the pointer at `pos`: at least a pixel inside the band. */
export function scrollStep(pos: number, start: number, end: number, ms: number): number {
  const band = Math.min(BAND, (end - start) / 4);
  const depth = pos < start + band ? pos - (start + band) : pos > end - band ? pos - (end - band) : 0;
  const step = MAX_SPEED * ms * Math.max(-1, Math.min(1, depth / band));
  return Math.sign(step) * Math.max(1, Math.round(Math.abs(step)));
}

const scrollable = (overflow: string) => overflow === "auto" || overflow === "scroll";

/** Moves the innermost box around `from` that can scroll toward the pointer; whether one did. */
function scrollToward(from: Element | null, x: number, y: number, ms: number): boolean {
  for (let el = from; el; el = el.parentElement) {
    // The page itself scrolls within the viewport, whatever its overflow says.
    const page = el === el.ownerDocument.scrollingElement;
    const style = getComputedStyle(el);
    const rect = page ? new DOMRect(0, 0, el.clientWidth, el.clientHeight) : el.getBoundingClientRect();
    const dx = page || scrollable(style.overflowX) ? scrollStep(x, rect.left, rect.right, ms) : 0;
    const dy = page || scrollable(style.overflowY) ? scrollStep(y, rect.top, rect.bottom, ms) : 0;
    const left = el.scrollLeft;
    const top = el.scrollTop;
    el.scrollLeft = left + dx;
    el.scrollTop = top + dy;
    if (el.scrollLeft !== left || el.scrollTop !== top) return true;
  }
  return false;
}

/** Scrolls the boxes under every native drag on the page. */
export function installDragScroll(): void {
  let pointer: { target: Element | null; x: number; y: number; at: number } | null = null;
  let frame = 0;
  let last = 0;
  const tick = (now: number) => {
    frame = 0;
    if (!pointer || now - pointer.at > IDLE_MS) return;
    if (scrollToward(pointer.target, pointer.x, pointer.y, Math.min(Math.max(now - last, 1), 50))) frame = requestAnimationFrame(tick);
    last = now;
  };
  const over = (e: DragEvent) => {
    pointer = { target: e.target instanceof Element ? e.target : null, x: e.clientX, y: e.clientY, at: performance.now() };
    if (frame) return;
    last = performance.now();
    frame = requestAnimationFrame(tick);
  };
  const stop = () => {
    pointer = null;
    cancelAnimationFrame(frame);
    frame = 0;
  };
  document.addEventListener("dragover", over, true);
  document.addEventListener("drop", stop, true);
  document.addEventListener("dragend", stop, true);
}
