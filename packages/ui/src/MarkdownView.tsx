import { Modal, useComputedColorScheme } from "@mantine/core";
import { useEffect, useRef, useState } from "react";

export type MarkdownTranslate = (
  key:
    | "markdown.diagramError"
    | "markdown.diagramSource"
    | "markdown.diagram"
    | "markdown.diagramFullSize"
    | "common.close",
) => string;

/** Display sanitized HTML produced by renderMarkdown, including lazy diagrams. */
export function MarkdownView({
  html,
  t,
  className,
}: {
  html: string;
  t: MarkdownTranslate;
  className?: string;
}) {
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
          const { drawDiagram } = await import("@guildbotics/ui/diagram");
          image = await drawDiagram(pre.textContent ?? "", container);
        } catch (e) {
          if (!cancelled)
            showDiagramError(
              pre,
              `${t("markdown.diagramError")}: ${e instanceof Error ? e.message : String(e)}`,
            );
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
          const img = (e.target as Element)
            .closest("button[data-diagram-open]")
            ?.querySelector("img");
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
function showDiagram(
  pre: HTMLPreElement,
  image: { src: string; alt: string },
  t: MarkdownTranslate,
): void {
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
