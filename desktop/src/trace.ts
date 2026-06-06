import type { PromptTraceEntry } from "./api/client";

export type PromptTraceGroup = {
  id: string;
  kind: string;
  request: PromptTraceEntry | null;
  response: PromptTraceEntry | null;
  single: PromptTraceEntry | null;
  timestamp: string;
  personId: string;
  brain: string;
};

export function buildTraceGroups(entries: PromptTraceEntry[]): PromptTraceGroup[] {
  const used = new Set<number>();
  const groups: PromptTraceGroup[] = [];
  entries.forEach((entry, index) => {
    if (used.has(index)) {
      return;
    }
    if (isTraceResponse(entry)) {
      const requestIndex = entries.findIndex(
        (candidate, candidateIndex) =>
          candidateIndex > index &&
          !used.has(candidateIndex) &&
          isTraceRequest(candidate) &&
          tracePairKey(candidate) === tracePairKey(entry),
      );
      const request = requestIndex >= 0 ? entries[requestIndex] : null;
      used.add(index);
      if (requestIndex >= 0) {
        used.add(requestIndex);
      }
      groups.push(traceGroup({ request, response: entry, single: null, index }));
      return;
    }
    used.add(index);
    groups.push(
      traceGroup({
        request: isTraceRequest(entry) ? entry : null,
        response: null,
        single: isTraceRequest(entry) ? null : entry,
        index,
      }),
    );
  });
  return groups;
}

function traceGroup({
  request,
  response,
  single,
  index,
}: {
  request: PromptTraceEntry | null;
  response: PromptTraceEntry | null;
  single: PromptTraceEntry | null;
  index: number;
}): PromptTraceGroup {
  const representative = response ?? request ?? single;
  return {
    id: `${representative?.timestamp ?? ""}-${representative?.event ?? ""}-${index}`,
    kind: traceKind(representative),
    request,
    response,
    single,
    timestamp: representative?.timestamp ?? "",
    personId: representative?.person_id ?? "",
    brain: representative?.brain ?? "",
  };
}

function isTraceRequest(entry: PromptTraceEntry) {
  return entry.event.endsWith(".request");
}

function isTraceResponse(entry: PromptTraceEntry) {
  return entry.event.endsWith(".response");
}

function tracePairKey(entry: PromptTraceEntry) {
  return [
    traceKind(entry),
    entry.person_id,
    entry.brain,
    traceFieldValue(entry, "model"),
    traceFieldValue(entry, "cli_agent"),
  ].join("|");
}

function traceKind(entry: PromptTraceEntry | null | undefined) {
  if (!entry) {
    return "trace";
  }
  if (entry.event.startsWith("llm.")) {
    return "llm";
  }
  if (entry.event.startsWith("cli_agent.")) {
    return "cli";
  }
  if (entry.event.startsWith("chat.")) {
    return "chat";
  }
  return "trace";
}

function traceFieldValue(entry: PromptTraceEntry, key: string) {
  const value = entry.fields[key];
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  return "";
}
