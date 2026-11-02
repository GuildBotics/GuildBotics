import { ActionIcon, Group } from "@mantine/core";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Pencil, Trash2 } from "lucide-react";
import { useState } from "react";
import { tickets } from "../../api/endpoints";
import type { Comment, MemberKind } from "../../api/types";
import { CommentView, ErrorMessage, type OnReact } from "../../components/common";
import { BodyEditor } from "../../components/Editable";
import { Markdown } from "../../components/Markdown";
import { useT } from "../../i18n";
import { useSession } from "../../query/hooks";
import { keys } from "../../query/keys";
import { href } from "../../router/routes";

export function TicketComment({ repo, ticketKey, comment, kind, onReact }: {
  repo: string;
  ticketKey: string;
  comment: Comment;
  kind?: MemberKind;
  onReact: OnReact;
}) {
  const t = useT();
  const client = useQueryClient();
  const me = useSession().data?.me;
  const allowed = me?.kind === "owner" || me?.name.toLowerCase() === comment.author.toLowerCase();
  const [editing, setEditing] = useState(false);
  const change = useMutation({
    mutationFn: async (body: string | null) => {
      if (body === null) await tickets.deleteComment(repo, ticketKey, comment.id);
      else await tickets.editComment(repo, ticketKey, comment.id, body);
    },
    onSuccess: () => setEditing(false),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.ticket(repo, ticketKey) });
      void client.invalidateQueries({ queryKey: ["board"] });
    },
  });
  return (
    <CommentView repo={repo} comment={comment} url={`${href.ticket(repo, ticketKey)}#${encodeURIComponent(comment.id)}`} kind={kind} onReact={onReact} actions={allowed && !editing && (
      <Group gap={4} ml="auto">
        <ActionIcon variant="subtle" color="gray" aria-label={t("comment.edit")} disabled={change.isPending} onClick={() => { change.reset(); setEditing(true); }}>
          <Pencil size={16} />
        </ActionIcon>
        <ActionIcon variant="subtle" color="red" aria-label={t("comment.delete")} disabled={change.isPending} onClick={() => {
          if (window.confirm(t("comment.confirmDelete"))) change.mutate(null);
        }}>
          <Trash2 size={16} />
        </ActionIcon>
      </Group>
    )}>
      {editing ? (
        <BodyEditor value={comment.body} label={t("comment.body")} required pending={change.isPending} attachTo={repo}
          onCancel={() => { setEditing(false); change.reset(); }} onSave={(body) => change.mutate(body)} />
      ) : <Markdown repo={repo} source={comment.body} />}
      {change.isError && <ErrorMessage error={change.error} />}
    </CommentView>
  );
}
