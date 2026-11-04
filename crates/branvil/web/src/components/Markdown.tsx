// The only place that renders HTML: Markdown is converted by marked and the
// result is sanitized by DOMPurify before it reaches the DOM.

import { MarkdownView } from "@guildbotics/ui/MarkdownView";
import { renderMarkdown as renderSafeMarkdown } from "@guildbotics/ui/markdown";
import { useMemo } from "react";
import { useT, type Translate } from "../i18n";
import { useMarkdown } from "../query/hooks";

/** Markdown → sanitized HTML string. Exported for tests. */
export function renderMarkdown(source: string, t?: Translate): string {
  const clean = renderSafeMarkdown(source);
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
  return <MarkdownView html={html} t={t} className={className} />;
}
