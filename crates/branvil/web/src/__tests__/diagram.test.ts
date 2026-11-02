import { describe, expect, it, vi } from "vitest";
import { svgImage } from "../components/diagram";

// Only the image step is under test; Mermaid needs a layout engine to draw.
vi.mock("mermaid", () => ({ default: {} }));

function decode(src: string): Document {
  return new DOMParser().parseFromString(decodeURIComponent(src.slice(src.indexOf(",") + 1)), "image/svg+xml");
}

describe("svgImage", () => {
  it("makes Mermaid's HTML-written SVG a sized image named by its title and description", () => {
    const { src, alt } = svgImage(
      '<svg xmlns="http://www.w3.org/2000/svg" width="100%" viewBox="0 0 120 40" style="max-width: 120px;">'
        + "<title>承認の流れ</title><desc>レビューを経てマージ</desc><text>a&nbsp;b</text></svg>",
    );
    expect(src).toMatch(/^data:image\/svg\+xml;charset=utf-8,/);
    const image = decode(src);
    expect(image.querySelector("parsererror")).toBeNull();
    const root = image.documentElement;
    expect(root.getAttribute("width")).toBe("120");
    expect(root.getAttribute("height")).toBe("40");
    expect(root.hasAttribute("style")).toBe(false);
    expect(root.querySelector("text")?.textContent).toBe("a b");
    expect(alt).toBe("承認の流れ: レビューを経てマージ");
  });

  it("names nothing when the source gives no title", () => {
    expect(svgImage('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1"></svg>').alt).toBe("");
  });
});
