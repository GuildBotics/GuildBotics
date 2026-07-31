import { useMemo } from "react";
import { useTranslation } from "react-i18next";

import { CommandPathHeader } from "./CommandPathHeader";

export type CommandSourceDiffProps = {
  before: string;
  after: string;
  path: string;
};

type DiffKind = "context" | "addition" | "deletion";
const MAX_EDIT_DISTANCE = 1_000;

export type CommandSourceDiffLine = {
  kind: DiffKind;
  oldLine?: number;
  newLine?: number;
  text: string;
};

function sourceLines(source: string): string[] {
  if (!source) {
    return [];
  }
  const lines = source.split("\n").map((line) => line.replace(/\r$/, ""));
  return lines.length > 1 && lines[lines.length - 1] === "" ? lines.slice(0, -1) : lines;
}

/** Calculate an exact line-based shortest edit script using Myers' algorithm. */
export function buildCommandSourceDiff(before: string, after: string): CommandSourceDiffLine[] {
  const oldLines = sourceLines(before);
  const newLines = sourceLines(after);
  const maximumDistance = oldLines.length + newLines.length;
  const frontier = new Map<number, number>([[1, 0]]);
  const trace: Map<number, number>[] = [];

  for (let distance = 0; distance <= Math.min(maximumDistance, MAX_EDIT_DISTANCE); distance += 1) {
    trace.push(new Map(frontier));
    for (let diagonal = -distance; diagonal <= distance; diagonal += 2) {
      const deletion = frontier.get(diagonal - 1) ?? Number.NEGATIVE_INFINITY;
      const insertion = frontier.get(diagonal + 1) ?? Number.NEGATIVE_INFINITY;
      let oldIndex =
        diagonal === -distance || (diagonal !== distance && deletion < insertion)
          ? Math.max(0, insertion)
          : deletion + 1;
      let newIndex = oldIndex - diagonal;

      while (
        oldIndex < oldLines.length &&
        newIndex < newLines.length &&
        oldLines[oldIndex] === newLines[newIndex]
      ) {
        oldIndex += 1;
        newIndex += 1;
      }
      frontier.set(diagonal, oldIndex);

      if (oldIndex >= oldLines.length && newIndex >= newLines.length) {
        return backtrackDiff(trace, oldLines, newLines);
      }
    }
  }

  return [
    ...oldLines.map((text, index) => ({
      kind: "deletion" as const,
      oldLine: index + 1,
      text,
    })),
    ...newLines.map((text, index) => ({
      kind: "addition" as const,
      newLine: index + 1,
      text,
    })),
  ];
}

function backtrackDiff(
  trace: Map<number, number>[],
  oldLines: string[],
  newLines: string[],
): CommandSourceDiffLine[] {
  const reversed: CommandSourceDiffLine[] = [];
  let oldIndex = oldLines.length;
  let newIndex = newLines.length;

  for (let distance = trace.length - 1; distance >= 0; distance -= 1) {
    const frontier = trace[distance];
    const diagonal = oldIndex - newIndex;
    const deletion = frontier.get(diagonal - 1) ?? Number.NEGATIVE_INFINITY;
    const insertion = frontier.get(diagonal + 1) ?? Number.NEGATIVE_INFINITY;
    const previousDiagonal =
      diagonal === -distance || (diagonal !== distance && deletion < insertion)
        ? diagonal + 1
        : diagonal - 1;
    const previousOldIndex = Math.max(0, frontier.get(previousDiagonal) ?? 0);
    const previousNewIndex = previousOldIndex - previousDiagonal;

    while (oldIndex > previousOldIndex && newIndex > previousNewIndex) {
      reversed.push({
        kind: "context",
        oldLine: oldIndex,
        newLine: newIndex,
        text: oldLines[oldIndex - 1],
      });
      oldIndex -= 1;
      newIndex -= 1;
    }

    if (distance === 0) {
      break;
    }
    if (oldIndex === previousOldIndex) {
      reversed.push({ kind: "addition", newLine: newIndex, text: newLines[newIndex - 1] });
      newIndex -= 1;
    } else {
      reversed.push({ kind: "deletion", oldLine: oldIndex, text: oldLines[oldIndex - 1] });
      oldIndex -= 1;
    }
  }

  return reversed.reverse();
}

export function CommandSourceDiff({ before, after, path }: CommandSourceDiffProps) {
  const { t } = useTranslation();
  const lines = useMemo(() => buildCommandSourceDiff(before, after), [after, before]);

  return (
    <div className="command-source-diff">
      <CommandPathHeader path={path} />
      <div
        aria-label={t("commands.authoringProposal.diffLabel", { path })}
        className="command-source-diff-scroll"
        role="table"
        tabIndex={0}
      >
        {lines.map((line, index) => (
          <div
            className={`command-source-diff-line command-source-diff-line-${line.kind}`}
            data-diff-kind={line.kind}
            key={`${line.kind}:${line.oldLine ?? ""}:${line.newLine ?? ""}:${index}`}
            role="row"
          >
            <span className="command-source-diff-number" role="cell">
              {line.oldLine ?? ""}
            </span>
            <span className="command-source-diff-number" role="cell">
              {line.newLine ?? ""}
            </span>
            <span aria-hidden="true" className="command-source-diff-marker">
              {line.kind === "addition" ? "+" : line.kind === "deletion" ? "−" : " "}
            </span>
            <code role="cell">{line.text || " "}</code>
          </div>
        ))}
      </div>
    </div>
  );
}
