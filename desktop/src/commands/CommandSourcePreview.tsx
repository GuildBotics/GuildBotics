import { useTranslation } from "react-i18next";

import { CommandFilePathBar } from "./CommandFilePathBar";

export type CommandSourcePreviewProps = {
  path: string;
  source: string;
};

function sourceLines(source: string): string[] {
  if (!source) {
    return [""];
  }
  const lines = source.split("\n").map((line) => line.replace(/\r$/, ""));
  return lines.length > 1 && lines[lines.length - 1] === "" ? lines.slice(0, -1) : lines;
}

/** Scrollable read-only source viewer for command proposal review. */
export function CommandSourcePreview({ path, source }: CommandSourcePreviewProps) {
  const { t } = useTranslation();

  return (
    <div className="command-source-preview">
      <CommandFilePathBar path={path} />
      <div
        aria-label={t("commands.authoringProposal.sourceLabel", { path })}
        className="command-source-preview-scroll"
        role="region"
        tabIndex={0}
      >
        {sourceLines(source).map((line, index) => (
          <div className="command-source-preview-line" key={index}>
            <span aria-hidden="true" className="command-source-preview-number">
              {index + 1}
            </span>
            <code>{line || " "}</code>
          </div>
        ))}
      </div>
    </div>
  );
}
