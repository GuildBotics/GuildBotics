import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { installDragScroll, scrollStep } from "../dragScroll";

describe("scroll step", () => {
  it("scrolls toward the edge the pointer is near, faster the nearer", () => {
    // A 400px box: the band is 64px at each edge; 16ms at most 24px.
    expect(scrollStep(200, 0, 400, 16)).toBe(0);
    expect(scrollStep(336, 0, 400, 16)).toBe(0);
    expect(scrollStep(337, 0, 400, 16)).toBe(1);
    expect(scrollStep(368, 0, 400, 16)).toBe(12);
    expect(scrollStep(400, 0, 400, 16)).toBe(24);
    expect(scrollStep(450, 0, 400, 16)).toBe(24);
    expect(scrollStep(0, 0, 400, 16)).toBe(-24);
  });

  it("narrows the band of a small box to a quarter of it", () => {
    expect(scrollStep(74, 0, 100, 16)).toBe(0);
    expect(scrollStep(100, 0, 100, 16)).toBe(24);
  });
});

describe("drag scroll", () => {
  const frames: FrameRequestCallback[] = [];
  const runFrame = (now: number) => frames.splice(0).forEach((f) => f(now));

  beforeAll(() => installDragScroll());
  // Globals and mocks are restored after each test (vite.config.ts).
  beforeEach(() => {
    vi.stubGlobal("requestAnimationFrame", (f: FrameRequestCallback) => frames.push(f));
    vi.stubGlobal("cancelAnimationFrame", () => frames.splice(0));
    vi.spyOn(performance, "now").mockReturnValue(0);
  });
  afterEach(() => {
    document.dispatchEvent(new Event("dragend"));
    document.body.innerHTML = "";
  });

  /** A box 400px high at the top of the viewport, its content `height` high; and an element in it. */
  const box = (overflowY: string, height: number) => {
    const el = document.createElement("div");
    el.style.overflowY = overflowY;
    el.getBoundingClientRect = () => new DOMRect(0, 0, 200, 400);
    // jsdom does no layout: keep scrollTop within the content as a browser does.
    let top = 0;
    Object.defineProperty(el, "scrollTop", { get: () => top, set: (v: number) => (top = Math.max(0, Math.min(height - 400, v))) });
    const inner = document.createElement("span");
    el.append(inner);
    document.body.append(el);
    return { el, inner };
  };
  const dragOver = (target: Element, y: number) => target.dispatchEvent(new MouseEvent("dragover", { bubbles: true, clientX: 100, clientY: y }));

  it("keeps scrolling the box under a drag near its edge until the drag ends", () => {
    const { el, inner } = box("auto", 2000);
    dragOver(inner, 400);
    runFrame(16);
    runFrame(32);
    expect(el.scrollTop).toBe(48);
    document.dispatchEvent(new Event("drop"));
    runFrame(48);
    expect(el.scrollTop).toBe(48);
  });

  it("leaves a box that does not scroll, and stops at the end of the content", () => {
    const hidden = box("hidden", 2000);
    dragOver(hidden.inner, 400);
    runFrame(16);
    expect(hidden.el.scrollTop).toBe(0);

    const short = box("auto", 410);
    dragOver(short.inner, 400);
    runFrame(16);
    expect(short.el.scrollTop).toBe(10);
    runFrame(32);
    expect(frames).toHaveLength(0);
  });

  it("scrolls the page itself within the viewport, though its overflow is visible", () => {
    // jsdom has no scrollingElement and does no layout: a 600px-high viewport over a page 2000px high.
    const page = document.documentElement;
    let top = 0;
    const fakes = ["clientWidth", "clientHeight", "scrollTop"] as const;
    Object.defineProperties(page, {
      clientWidth: { value: 800, configurable: true },
      clientHeight: { value: 600, configurable: true },
      scrollTop: { get: () => top, set: (v: number) => (top = Math.max(0, Math.min(1400, v))), configurable: true },
    });
    Object.defineProperty(document, "scrollingElement", { value: page, configurable: true });
    try {
      const inner = document.createElement("div");
      document.body.append(inner);
      dragOver(inner, 600);
      runFrame(16);
      expect(page.scrollTop).toBe(24);
    } finally {
      for (const name of fakes) delete (page as unknown as Record<string, unknown>)[name];
      delete (document as unknown as Record<string, unknown>)["scrollingElement"];
    }
  });

  it("stops when the drag has left the page", () => {
    const { el, inner } = box("auto", 2000);
    dragOver(inner, 400);
    runFrame(16);
    runFrame(2000);
    expect(el.scrollTop).toBe(24);
    expect(frames).toHaveLength(0);
  });
});
