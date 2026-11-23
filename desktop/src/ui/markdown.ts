import DOMPurify from "dompurify";
import { Marked } from "marked";

const marked = new Marked({
  gfm: true,
  breaks: false,
  async: false,
  renderer: {
    // A Mermaid block stays readable source until the page draws it.
    code({ text, lang }) {
      if (lang?.match(/^\S*/)?.[0] !== "mermaid") return false;
      const escaped = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
      return `<pre data-mermaid=""><code>${escaped}</code></pre>\n`;
    },
  },
});

const SAFE_URL = /^(?:https?:|mailto:|#|\/(?!\/))/i;

// Its own instance: Mermaid adds hooks to the shared one that would undo these.
const purify = DOMPurify(window);
purify.addHook("afterSanitizeAttributes", (node) => {
  for (const attr of ["href", "src"]) {
    const value = node.getAttribute(attr);
    if (value !== null && !SAFE_URL.test(value.trim())) node.removeAttribute(attr);
  }
  if (node.tagName === "A" && node.getAttribute("href")) {
    node.setAttribute("target", "_blank");
    node.setAttribute("rel", "noopener noreferrer nofollow");
  }
});

/** Markdown → sanitized HTML string. Exported for tests. */
export function renderMarkdown(source: string): string {
  const html = marked.parse(source) as string;
  const clean = purify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: [
      "style",
      "form",
      "input",
      "button",
      "textarea",
      "select",
      "iframe",
      "object",
      "embed",
      "svg",
      "math",
    ],
    FORBID_ATTR: ["style", "class", "id"],
  });
  return clean;
}
