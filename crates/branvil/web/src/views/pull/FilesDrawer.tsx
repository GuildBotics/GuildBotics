import { Badge, Drawer, Group, Text } from "@mantine/core";
import { ChevronRight } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { MAX_FILES } from "../../api/endpoints";
import type { FileChange } from "../../api/types";
import { ErrorMessage, Loading, Sha } from "../../components/common";
import { useT } from "../../i18n";
import { usePullFiles } from "../../query/hooks";

function lineClass(line: string): string {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "";
}

const STATUS_COLOR: Record<FileChange["status"], string> = {
  added: "success",
  removed: "danger",
  modified: "neutral",
  renamed: "info",
};

/** One file's unified diff, rendered as text lines (no HTML). */
function Patch({ file }: { file: FileChange }) {
  const t = useT();
  if (file.patch === null) {
    return (
      <Text size="sm" c="dimmed" p="sm">
        {t("files.binary")}
      </Text>
    );
  }
  return (
    <>
      <pre className="patch" tabIndex={0}>
        {file.patch.split("\n").map((line, i) => (
          <span key={i} className={lineClass(line)}>
            {line}
            {"\n"}
          </span>
        ))}
      </pre>
      {!file.patch_complete && (
        <Text size="xs" c="dimmed" p="xs">
          {t("files.patchCut")}
        </Text>
      )}
    </>
  );
}

/** A file's diff, closed until opened; a focused file (remounted by its key) starts open and in view. */
function FileRow({ file, focused }: { file: FileChange; focused: boolean }) {
  const t = useT();
  const [open, setOpen] = useState(focused);
  const ref = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView?.({ block: "start" });
  }, [focused]);
  const name = file.previous_path && file.previous_path !== file.path ? `${file.previous_path} → ${file.path}` : file.path;
  return (
    <details className="file" ref={ref} open={open} onToggle={(e) => setOpen((e.currentTarget as HTMLDetailsElement).open)}>
      <summary>
        <ChevronRight size={14} style={{ transform: open ? "rotate(90deg)" : undefined, color: "var(--mantine-color-dimmed)" }} />
        <Badge color={STATUS_COLOR[file.status]} variant="light" size="sm">
          {t(`files.status.${file.status}`)}
        </Badge>
        <code>{name}</code>
        <Group gap={6} ml="auto">
          <span className="add">+{file.additions}</span>
          <span className="del">−{file.deletions}</span>
        </Group>
      </summary>
      {open && <Patch file={file} />}
    </details>
  );
}

/** The files a pull request changes, in a drawer from the right (as GuildBotics' drawers). */
export function FilesDrawer({
  repo,
  number,
  headSha,
  baseSha,
  opened,
  focus,
  onClose,
}: {
  repo: string;
  number: number;
  headSha: string;
  baseSha: string;
  opened: boolean;
  /** A file to open and scroll to (a thread's path). */
  focus: string | null;
  onClose: () => void;
}) {
  const t = useT();
  const files = usePullFiles(repo, number, headSha);
  const items = files.data?.items ?? [];
  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      position="right"
      size={780}
      closeButtonProps={{ "aria-label": t("common.close") }}
      title={
        <Group gap="sm">
          {t("pull.changes")}
          <Text span size="xs" c="dimmed">
            <Sha repo={repo} value={baseSha} /> → <Sha repo={repo} value={headSha} />
          </Text>
        </Group>
      }
    >
      {files.isPending ? (
        <Loading />
      ) : files.isError ? (
        <ErrorMessage error={files.error} />
      ) : (
        <>
          <Text size="sm" mb="sm">
            {t("files.summary", { count: items.length })} <span className="add">+{items.reduce((n, f) => n + f.additions, 0)}</span>{" "}
            <span className="del">−{items.reduce((n, f) => n + f.deletions, 0)}</span>
          </Text>
          {items.length === 0 ? (
            <Text size="sm" c="dimmed">
              {t("files.none")}
            </Text>
          ) : (
            items.map((f) => (
              <FileRow
                key={`${f.previous_path ?? ""}>${f.path}${f.path === focus ? "#focus" : ""}`}
                file={f}
                focused={f.path === focus || items.length === 1}
              />
            ))
          )}
          {!files.data.complete && (
            <Text size="xs" c="dimmed" mt="sm">
              {t("files.more", { max: MAX_FILES })}
            </Text>
          )}
        </>
      )}
    </Drawer>
  );
}
