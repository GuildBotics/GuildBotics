import { useCallback } from "react";
import { MarkdownView } from "../../ui/MarkdownView";
import { useT, type Translate } from "../i18n";
import { useMarkdown } from "../query/hooks";

function describeReference(a: HTMLAnchorElement, t: Translate) {
  const kind = a.dataset.branvilKind;
  const state = a.dataset.branvilState;
  if (
    (kind !== "ticket" && kind !== "pull") ||
    (state !== "open" && state !== "closed" && state !== "merged")
  )
    return;
  const description = `${t(`markdown.${kind}`)}, ${t(`pull.state.${state}`)}`;
  a.title = `${description}: ${a.title}`;
  a.setAttribute("aria-label", `${a.textContent}, ${a.title}`);
}

export function Markdown({
  repo,
  source,
  className,
}: {
  repo: string;
  source: string;
  className?: string;
}) {
  const t = useT();
  const decorateLink = useCallback((link: HTMLAnchorElement) => describeReference(link, t), [t]);
  const resolved = useMarkdown(repo, source);
  // Show the source while resolving (and if the request fails); no content
  // is hidden by a failed enhancement, and source changes cannot use old links.
  return (
    <MarkdownView
      source={resolved.data?.markdown ?? source}
      decorateLink={decorateLink}
      className={className}
    />
  );
}
