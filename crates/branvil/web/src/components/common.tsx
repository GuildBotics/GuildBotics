// Small shared presentational components, built from Mantine as GuildBotics
// Desktop's are.

import { ActionIcon, Alert, Avatar, Badge, Button, Card, CopyButton as MantineCopy, Group, Menu, Text, Tooltip } from "@mantine/core";
import { Anvil, Check, Copy, SmilePlus } from "lucide-react";
import { useState, type ReactNode } from "react";
import { isApiError } from "../api/client";
import { REACTIONS, type Comment, type MemberKind, type Reaction, type ReactionEntry } from "../api/types";
import { useI18n, useT, type Translate } from "../i18n";
import { useMemberAvatar, useSession } from "../query/hooks";
import { Link, useFragmentTarget } from "../router/Router";
import { href } from "../router/routes";
import { Markdown } from "./Markdown";
import { MarkdownField } from "./MarkdownField";

/** What a secret field shows while one is saved (the secret itself is never shown), as in GuildBotics Desktop. */
export const MASKED_SECRET = "••••••••••••";

export function Loading() {
  const t = useT();
  return (
    <Text size="sm" c="dimmed" role="status">
      {t("common.loading")}
    </Text>
  );
}

/** Shows an API error; a 404 of an optional endpoint reads "not available yet". */
export function ErrorMessage({ error, optional }: { error: unknown; optional?: boolean }) {
  const t = useT();
  if (optional && (isApiError(error, undefined, 404) || isApiError(error, undefined, 501))) {
    return (
      <Text size="sm" c="dimmed">
        {t("common.notAvailableYet")}
      </Text>
    );
  }
  return <Refusal>{errorText(error, t)}</Refusal>;
}

/** Shared guidance, including the places that embed an error in a notice. */
export function errorText(error: unknown, t: Translate): string {
  if (isApiError(error, "session_changed")) return t("common.sessionChanged");
  if (isApiError(error, "network")) return t("common.networkError");
  const message = error instanceof Error ? error.message : String(error);
  return isApiError(error, undefined, 403) ? `${t("common.forbidden")} ${message}` : message;
}

/** A refusal or failure the user should read: an alert in GuildBotics' danger tone. */
export function Refusal({ children }: { children: ReactNode }) {
  return (
    <Alert color="danger" role="alert" p="xs">
      <Text size="sm">{children}</Text>
    </Alert>
  );
}

/** A short confirmation (saved, created). */
export function Notice({ children }: { children: ReactNode }) {
  return (
    <Text size="sm" c="var(--color-success-text)" role="status">
      {children}
    </Text>
  );
}

/** A moment in the reader's language; `date` shows only its day. */
export function Time({ value, date }: { value: string | null | undefined; date?: boolean }) {
  const { lang } = useI18n();
  if (!value) return null;
  const d = new Date(value);
  const shown = date ? d.toLocaleDateString(lang, { year: "numeric", month: "2-digit", day: "2-digit" }) : d.toLocaleString(lang);
  return (
    <time dateTime={value} title={value}>
      {Number.isNaN(d.getTime()) ? value : shown}
    </time>
  );
}

/** Every displayed commit ID opens that repository's commit page. */
export function Sha({ repo, value, full = false }: { repo: string; value: string; full?: boolean }) {
  if (!value) return <code>—</code>;
  return (
    <Link to={href.commit(repo, value)}>
      <code className="sha" title={value}>
        {full ? value : value.slice(0, 10)}
      </code>
    </Link>
  );
}

export function CopyButton({ value, label }: { value: string; label?: string }) {
  const t = useT();
  return (
    <MantineCopy value={value} timeout={2000}>
      {({ copied, copy }) => (
        <Tooltip label={copied ? t("common.copied") : (label ?? t("common.copy"))} withArrow>
          <ActionIcon variant="subtle" color="gray" size="sm" onClick={copy} aria-label={label ?? t("common.copy")}>
            {copied ? <Check size={14} /> : <Copy size={14} />}
          </ActionIcon>
        </Tooltip>
      )}
    </MantineCopy>
  );
}

/** GuildBotics' badge tones (styles.css re-skins Mantine's badges by color). */
export type Tone = "success" | "danger" | "warning" | "info" | "neutral";

export function StateBadge({ children, tone = "neutral" }: { children: ReactNode; tone?: Tone }) {
  return (
    <Badge color={tone} variant="light" size="sm">
      {children}
    </Badge>
  );
}

/** A member's avatar: their initial, teal for agents as GuildBotics shows members. */
export function MemberAvatar({ name, kind, size = 22 }: { name: string; kind?: MemberKind; size?: number }) {
  // The initial stands in while the picture loads, or when there is none.
  const src = useMemberAvatar(name);
  return (
    <Avatar src={src} size={size} radius="xl" color={kind === "agent" ? "teal" : "gray"} variant="light" alt={name}>
      {name.toLowerCase() === "branvil" ? <Anvil size={Math.round(size * 0.7)} role="img" aria-label="Branvil" /> : name.slice(0, 1).toUpperCase()}
    </Avatar>
  );
}

/** Each reaction's display emoji; acknowledgement is distinct from agreement. */
const REACTION_EMOJI: Record<Reaction, string> = { ack: "✅", agree: "👍", celebrate: "🎉", support: "❤️" };

/** Gives (`on`) or takes back the signed-in member's reaction. */
export type OnReact = (reaction: Reaction, on: boolean) => void;

/**
 * A comment's reactions: the ones given, as emoji with their counts, and a menu
 * to add one. As on GitHub, the member's own reactions are highlighted and
 * clicking one (or choosing it again in the menu) takes it back.
 */
export function Reactions({ reactions, onReact }: { reactions: ReactionEntry[]; onReact: OnReact }) {
  const t = useT();
  const me = useSession().data?.me.name.toLowerCase();
  const whoGave = (r: Reaction) => reactions.filter((x) => x.reaction === r).map((x) => x.author);
  const mine = (r: Reaction) => whoGave(r).some((a) => a.toLowerCase() === me);
  const given = REACTIONS.filter((r) => whoGave(r).length > 0);
  return (
    <Group gap={6} mt={10} role="group" aria-label={t("reaction.group")}>
      {given.map((r) => {
        const who = whoGave(r);
        return (
          <Tooltip key={r} label={`${t(`reaction.${r}`)}: ${who.join(", ")}`} withArrow>
            <Button
              size="compact-xs"
              variant={mine(r) ? "light" : "default"}
              aria-label={`${t(`reaction.${r}`)} (${who.length})`}
              aria-pressed={mine(r)}
              onClick={() => onReact(r, !mine(r))}
            >
              {REACTION_EMOJI[r]} {who.length}
            </Button>
          </Tooltip>
        );
      })}
      {/* Without the opening animation, which would slide an emoji under the resting
          mouse and pop its tooltip. */}
      <Menu position="bottom-start" withinPortal transitionProps={{ duration: 0 }}>
        <Menu.Target>
          <Tooltip label={t("reaction.add")} withArrow>
            <ActionIcon variant="subtle" color="gray" size="sm" aria-label={t("reaction.add")}>
              <SmilePlus size={14} />
            </ActionIcon>
          </Tooltip>
        </Menu.Target>
        <Menu.Dropdown>
          <Group gap={0} wrap="nowrap">
            {REACTIONS.map((r) => (
              <Tooltip key={r} label={t(`reaction.${r}`)} withArrow>
                <Menu.Item
                  w="auto"
                  fz="lg"
                  px={8}
                  aria-label={t(`reaction.${r}`)}
                  data-active={mine(r) || undefined}
                  onClick={() => onReact(r, !mine(r))}
                >
                  {REACTION_EMOJI[r]}
                </Menu.Item>
              </Tooltip>
            ))}
          </Group>
        </Menu.Dropdown>
      </Menu>
    </Group>
  );
}

/** The header strip of a post or comment: who wrote it and when, then any extras. */
export function PostHead({ author, kind, at, url, children }: { author: string; kind?: MemberKind; at: string; url?: string; children?: ReactNode }) {
  const t = useT();
  return (
    <div className="post-head">
      <MemberAvatar name={author} kind={kind} />
      <strong>{author}</strong>
      <Text span size="xs" c="dimmed">
        {url ? <Link to={url} title={t("comment.link")}><Time value={at} /></Link> : <Time value={at} />}
      </Text>
      {url && <CopyButton value={new URL(url, window.location.origin).href} label={t("comment.copyUrl")} />}
      {children}
    </div>
  );
}

/** A page's main text (a ticket's or pull request's body) under its author's header strip. */
export function Post({ head, children }: { head: ReactNode; children: ReactNode }) {
  return (
    <Card className="post" p={0}>
      {head}
      <div className="post-body">{children}</div>
    </Card>
  );
}

/** A comment; one with `meta.routing == "suppress"` is a status notice. */
export function CommentView({
  repo,
  comment,
  url,
  kind,
  onReact,
  actions,
  children,
}: {
  repo: string;
  comment: Comment;
  url: string;
  kind?: MemberKind;
  onReact: OnReact;
  actions?: ReactNode;
  children?: ReactNode;
}) {
  const t = useT();
  const notice = comment.meta?.["routing"] === "suppress";
  const target = useFragmentTarget(comment.id);
  return (
    <article {...target} className={notice ? "comment notice" : "comment"} id={comment.id} aria-label={notice ? t("comment.notice") : undefined}>
      <PostHead author={comment.author} kind={kind} at={comment.created_at} url={url}>
        {notice && <StateBadge tone="info">{t("comment.notice")}</StateBadge>}
        {actions}
      </PostHead>
      <div className="post-body">
        {children ?? <Markdown repo={repo} source={comment.body} />}
        <Reactions reactions={comment.reactions ?? []} onReact={onReact} />
      </div>
    </article>
  );
}

/**
 * A Markdown textarea and its buttons; it clears itself when a submit
 * succeeds. `actions` are other buttons (resolve a thread) placed before the
 * submit, `trailing` ones (a review's verdicts) after it. `attachTo` (a
 * repository) lets it attach images.
 */
export function ComposeForm({
  label,
  submitLabel,
  onSubmit,
  pending,
  error,
  hint,
  actions,
  trailing,
  submitVariant = "filled",
  attachTo,
}: {
  label: string;
  submitLabel: string;
  onSubmit: (body: string) => Promise<unknown>;
  pending: boolean;
  error?: unknown;
  hint?: ReactNode;
  actions?: (body: string, clear: () => void) => ReactNode;
  trailing?: (body: string, clear: () => void) => ReactNode;
  submitVariant?: "filled" | "default";
  attachTo?: string;
}) {
  const [body, setBody] = useState("");
  const [uploading, setUploading] = useState(false);
  const clear = () => setBody("");
  return (
    <form
      className="compose"
      onSubmit={(e) => {
        e.preventDefault();
        if (!body.trim() || uploading) return;
        void onSubmit(body).then(clear, () => undefined);
      }}
    >
      <MarkdownField aria-label={label} placeholder={label} value={body} onChange={setBody} rows={3} attachTo={attachTo} onUploading={setUploading} />
      {error ? <ErrorMessage error={error} /> : null}
      <Group justify="flex-end" gap="xs" mt="xs">
        {hint && (
          <Text size="xs" c="dimmed" mr="auto">
            {hint}
          </Text>
        )}
        {actions?.(body, clear)}
        <Button type="submit" variant={submitVariant} disabled={pending || uploading || !body.trim()}>
          {submitLabel}
        </Button>
        {trailing?.(body, clear)}
      </Group>
    </form>
  );
}
