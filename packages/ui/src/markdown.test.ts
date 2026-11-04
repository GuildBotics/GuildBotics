import { describe, expect, it } from "vitest";
import { renderMarkdown } from "./markdown";

describe("shared Markdown rendering", () => {
  it("preserves GFM and Mermaid source while excluding executable HTML", () => {
    const html = renderMarkdown(
      "# Heading\n\n**bold**\n\n```mermaid\ngraph TD; A-->B\n```\n\n<script>alert(1)</script>\n\n[x](javascript:alert(1))",
    );
    expect(html).toContain("<h1>Heading</h1>");
    expect(html).toContain("<strong>bold</strong>");
    expect(html).toContain('data-mermaid=""');
    expect(html).not.toMatch(/<script|javascript:/);
  });

  it.each(["https://example.com", "mailto:a@example.com", "/files/image.png", "#heading"])(
    "allows %s",
    (url) => {
      const element = document.createElement("div");
      element.innerHTML = renderMarkdown(`[link](${url})`);
      const link = element.querySelector("a")!;
      expect(link.getAttribute("href")).toBe(url);
      expect(link.rel).toBe("noopener noreferrer nofollow");
    },
  );

  it.each(["//external.example/x", "data:text/html,bad", "file:///etc/passwd"])(
    "removes %s",
    (url) => {
      expect(renderMarkdown(`<img src="${url}"><a href="${url}">link</a>`)).not.toContain(url);
    },
  );
});
