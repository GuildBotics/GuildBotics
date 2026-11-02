// A Markdown textarea that, given a repository, attaches images: pasted,
// dropped on it, or chosen. Each goes in at the cursor as a placeholder line
// while it uploads, so typing goes on, and becomes `![name](url)` once stored.

import { Button, FileButton, Group, Stack, Text, Textarea, type TextareaProps } from "@mantine/core";
import { ImagePlus } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState, type ClipboardEvent, type DragEvent } from "react";
import { isApiError } from "../api/client";
import { tickets } from "../api/endpoints";
import { useT, type MessageKey } from "../i18n";
import { ErrorMessage, Refusal } from "./common";

const REFUSALS: Partial<Record<string, MessageKey>> = { invalid: "attach.invalid", too_large: "attach.tooLarge" };

/** Numbers the placeholders, so two uploads of one name stay apart. */
let uploads = 0;

/** A file name as an image's alt text: without what would end the text or the line. */
export const altText = (name: string) => name.replace(/[[\]\\\r\n]/g, " ");

/** Whether `text` refers to an attachment: its id, as the service finds references. */
export const hasAttachment = (text: string) => /(?<![0-9A-Za-z])atc_[0-9A-Za-z]{26}(?![0-9A-Za-z])/.test(text);

export function MarkdownField({
  value,
  onChange,
  attachTo,
  onUploading,
  ...props
}: Omit<TextareaProps, "value" | "onChange"> & {
  value: string;
  onChange: (value: string) => void;
  /** The repository images are attached to; without one the field attaches none. */
  attachTo?: string;
  /** Whether an upload is in flight; the form should not submit meanwhile. */
  onUploading?: (uploading: boolean) => void;
}) {
  const t = useT();
  const field = useRef<HTMLTextAreaElement>(null);
  // Cleared after each choice, so choosing the same file again (after a failure) attaches it again.
  const resetChoice = useRef<() => void>(null);
  // The latest text and `onChange`: an upload finishes after renders that changed them.
  const text = useRef(value);
  const change = useRef(onChange);
  const caret = useRef<[number, number] | null>(null);
  const [pending, setPending] = useState(0);
  const [failed, setFailed] = useState<unknown>(null);
  useLayoutEffect(() => {
    change.current = onChange;
  }, [onChange]);
  // A field closed while uploading leaves its form free to submit again.
  useEffect(() => {
    onUploading?.(pending > 0);
    return () => onUploading?.(false);
  }, [pending, onUploading]);
  // Setting the value would put the cursor at the end; put it back where it was in the text.
  useLayoutEffect(() => {
    text.current = value;
    if (caret.current) field.current?.setSelectionRange(...caret.current);
    caret.current = null;
  }, [value]);

  /** Replaces `from`–`to` of the text with `insert`; the field is gone if it was closed meanwhile. */
  const splice = (from: number, to: number, insert: string) => {
    const el = field.current;
    if (!el) return;
    const move = (p: number) => (p < from ? p : p >= to ? p + insert.length - (to - from) : from + insert.length);
    // A splice not rendered yet holds the caret the field does not show yet.
    const [start, end] = caret.current ?? [el.selectionStart, el.selectionEnd];
    caret.current = [move(start), move(end)];
    text.current = text.current.slice(0, from) + insert + text.current.slice(to);
    change.current(text.current);
  };
  const settle = (placeholder: string, insert: string) => {
    const at = text.current.indexOf(placeholder);
    if (at < 0) return;
    // A failed one takes the line break that kept it apart from the next.
    const end = at + placeholder.length;
    splice(at, insert || text.current[end] !== "\n" ? end : end + 1, insert);
  };
  const attach = (files: File[]) => {
    if (!attachTo || !field.current || files.length === 0) return;
    setFailed(null);
    setPending((n) => n + files.length);
    const queued = files.map((file) => ({ file, placeholder: `![${t("attach.placeholder", { name: altText(file.name) })}](#upload-${++uploads})` }));
    splice(field.current.selectionStart, field.current.selectionEnd, queued.map((q) => q.placeholder).join("\n"));
    for (const { file, placeholder } of queued) {
      void tickets
        .attach(attachTo, file, file.name)
        .then(
          (a) => settle(placeholder, `![${altText(file.name)}](${a.url})`),
          (err: unknown) => {
            settle(placeholder, "");
            setFailed(err);
          },
        )
        .finally(() => setPending((n) => n - 1));
    }
  };
  // A paste with text (a spreadsheet's cells come with a picture of them) stays text.
  const onPaste = (e: ClipboardEvent) => {
    if (e.clipboardData.files.length === 0 || e.clipboardData.types.includes("text/plain")) return;
    e.preventDefault();
    attach([...e.clipboardData.files]);
  };
  const onDrop = (e: DragEvent) => {
    if (e.dataTransfer.files.length === 0) return;
    e.preventDefault();
    attach([...e.dataTransfer.files]);
  };

  const textarea = <Textarea ref={field} {...props} value={value} onChange={(e) => onChange(e.currentTarget.value)} />;
  if (!attachTo) return textarea;
  const refusal = isApiError(failed) ? REFUSALS[failed.code] : undefined;
  return (
    <Stack gap={4}>
      {/* Without dragover's default, the browser would open a dropped file instead. */}
      <div onPaste={onPaste} onDrop={onDrop} onDragOver={(e) => e.dataTransfer.types.includes("Files") && e.preventDefault()}>
        {textarea}
      </div>
      <Group gap="xs">
        <FileButton
          multiple
          accept="image/png,image/jpeg,image/gif,image/webp"
          resetRef={resetChoice}
          onChange={(files) => {
            attach(files);
            resetChoice.current?.();
          }}
        >
          {(p) => (
            <Button {...p} size="compact-xs" variant="subtle" color="gray" leftSection={<ImagePlus size={14} />}>
              {t("attach.choose")}
            </Button>
          )}
        </FileButton>
        {pending > 0 && (
          <Text size="xs" c="dimmed" role="status">
            {t("attach.uploading")}
          </Text>
        )}
      </Group>
      {refusal ? <Refusal>{t(refusal)}</Refusal> : failed ? <ErrorMessage error={failed} /> : null}
    </Stack>
  );
}
