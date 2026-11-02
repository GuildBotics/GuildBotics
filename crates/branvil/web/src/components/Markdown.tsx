// The only place that renders HTML: Markdown is converted by marked and the
// result is sanitized by DOMPurify before it reaches the DOM.

import { Modal, useComputedColorScheme } from "@mantine/core";
import DOMPurify from "dompurify";
import { Marked } from "marked";
import { useEffect, useMemo, useRef, useState } from "react";
import { useT, type Translate } from "../i18n";
import { useMarkdown } from "../query/hooks";

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
export function renderMarkdown(source: string, t?: Translate): string {
  const html = marked.parse(source) as string;
  const clean = purify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ["style", "form", "input", "button", "textarea", "select", "iframe", "object", "embed", "svg", "math"],
    FORBID_ATTR: ["style", "class", "id"],
  });
  if (!t) return clean;
  const element = document.createElement("div");
  element.innerHTML = clean;
  for (const a of element.querySelectorAll<HTMLAnchorElement>("a[data-branvil-kind][data-branvil-state]")) {
    const kind = a.dataset.branvilKind;
    const state = a.dataset.branvilState;
    if ((kind !== "ticket" && kind !== "pull") || (state !== "open" && state !== "closed" && state !== "merged")) continue;
    const description = `${t(`markdown.${kind}`)}, ${t(`pull.state.${state}`)}`;
    a.title = `${description}: ${a.title}`;
    a.setAttribute("aria-label", `${a.textContent}, ${a.title}`);
  }
  return element.innerHTML;
}

export function Markdown({ repo, source, className }: { repo: string; source: string; className?: string }) {
  const t = useT();
  const resolved = useMarkdown(repo, source);
  // Show the source while resolving (and if the request fails); no content
  // is hidden by a failed enhancement, and source changes cannot use old links.
  const markdown = resolved.data?.markdown ?? source;
  const html = useMemo(() => renderMarkdown(markdown, t), [markdown, t]);
  const ref = useRef<HTMLDivElement>(null);
  // Diagrams take the tokens' colors, which change with the color scheme.
  const scheme = useComputedColorScheme("light");
  const [fullSize, setFullSize] = useState<{ src: string; alt: string; opened: boolean }>();
  useEffect(() => {
    const container = ref.current;
    const blocks = [...(container?.querySelectorAll<HTMLPreElement>("pre[data-mermaid]") ?? [])];
    if (!container || blocks.length === 0) return;
    let cancelled = false;
    void (async () => {
      for (const pre of blocks) {
        if (cancelled) return;
        let image;
        try {
          const { drawDiagram } = await import("./diagram");
          image = await drawDiagram(pre.textContent ?? "", container);
        } catch (e) {
          if (!cancelled) showDiagramError(pre, `${t("markdown.diagramError")}: ${e instanceof Error ? e.message : String(e)}`);
          continue;
        }
        if (!cancelled) showDiagram(pre, image, t);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [html, scheme, t]);
  return (
    <>
      <div
        ref={ref}
        className={["markdown", className].filter(Boolean).join(" ")}
        dangerouslySetInnerHTML={{ __html: html }}
        onClick={(e) => {
          const img = (e.target as Element).closest("button[data-diagram-open]")?.querySelector("img");
          if (img) setFullSize({ src: img.src, alt: img.alt, opened: true });
        }}
      />
      <Modal
        opened={fullSize?.opened ?? false}
        onClose={() => setFullSize((shown) => shown && { ...shown, opened: false })}
        title={fullSize?.alt}
        size="auto"
        closeButtonProps={{ "aria-label": t("common.close") }}
        centered
      >
        {fullSize && <img className="diagram-full-size" src={fullSize.src} alt={fullSize.alt} />}
      </Modal>
    </>
  );
}

// What this code put beside each Mermaid block: the body's own HTML may carry the same data attributes.
const drawnImages = new WeakMap<HTMLPreElement, HTMLImageElement>();
const errorNotes = new WeakMap<HTMLPreElement, HTMLElement>();

/** Puts the drawn diagram in place of its source, which stays one click away. */
function showDiagram(pre: HTMLPreElement, image: { src: string; alt: string }, t: Translate): void {
  errorNotes.get(pre)?.remove();
  errorNotes.delete(pre);
  let img = drawnImages.get(pre);
  if (!img) {
    img = document.createElement("img");
    const figure = document.createElement("figure");
    figure.dataset.diagram = "";
    const open = document.createElement("button");
    open.type = "button";
    open.dataset.diagramOpen = "";
    open.append(img);
    const details = document.createElement("details");
    const summary = document.createElement("summary");
    summary.textContent = t("markdown.diagramSource");
    pre.replaceWith(figure);
    details.append(summary, pre);
    figure.append(open, details);
    drawnImages.set(pre, img);
  }
  img.src = image.src;
  img.alt = image.alt || t("markdown.diagram");
  img.parentElement!.title = `${t("markdown.diagramFullSize")}: ${img.alt}`;
}

/** Leaves the source in view, with what Mermaid said about it (announced once, not at each redraw). */
function showDiagramError(pre: HTMLPreElement, message: string): void {
  let note = errorNotes.get(pre);
  if (!note) {
    note = document.createElement("p");
    note.dataset.diagramError = "";
    note.setAttribute("role", "alert");
    pre.before(note);
    errorNotes.set(pre, note);
  }
  if (note.textContent !== message) note.textContent = message;
}
