import type { ComponentProps } from "react";
import { isApiError } from "../../api/client";
import { MarkdownField as Field } from "../../ui/MarkdownField";
import { tickets } from "../api/endpoints";
import { useT, type MessageKey } from "../i18n";
import { ErrorMessage, Refusal } from "./common";

const REFUSALS: Partial<Record<string, MessageKey>> = {
  invalid: "attach.invalid",
  too_large: "attach.tooLarge",
};

/** Whether `text` refers to an attachment: its id, as the service finds references. */
export const hasAttachment = (text: string) =>
  /(?<![0-9A-Za-z])atc_[0-9A-Za-z]{26}(?![0-9A-Za-z])/.test(text);

export function MarkdownField({
  attachTo,
  ...props
}: Omit<ComponentProps<typeof Field>, "upload" | "renderError"> & { attachTo?: string }) {
  const t = useT();
  return (
    <Field
      {...props}
      upload={
        attachTo ? async (file) => (await tickets.attach(attachTo, file, file.name)).url : undefined
      }
      renderError={(error) => {
        const refusal = isApiError(error) ? REFUSALS[error.code] : undefined;
        return refusal ? <Refusal>{t(refusal)}</Refusal> : <ErrorMessage error={error} />;
      }}
    />
  );
}
