import { beforeEach, describe, expect, it } from "vitest";

import type { CommandFileDetail } from "../api/client";
import {
  blockingMessageKey,
  buildFileRunArgs,
  deriveSaveStatus,
  hasMissingRequiredArgument,
  loadEditorState,
  saveEditorState,
} from "./commandEditorState";

function detail(overrides: Partial<CommandFileDetail> = {}): CommandFileDetail {
  return {
    id: "id",
    command: "greet",
    label: "Greet",
    description: "",
    relative_path: "greet.md",
    format: "markdown",
    content: "",
    revision: "rev-1",
    arguments: [
      { name: "topic", kind: "positional", required: true, default: "" },
      { name: "mode", kind: "keyword", required: false, default: "" },
    ],
    inputs: { defined_args: "auto", extra_args: "optional", message: "optional" },
    ...overrides,
  };
}

beforeEach(() => {
  window.localStorage.clear();
});

describe("deriveSaveStatus", () => {
  it("prefers saving and conflict over dirty", () => {
    expect(deriveSaveStatus("a", "b", true, false)).toBe("saving");
    expect(deriveSaveStatus("a", "b", false, true)).toBe("conflict");
    expect(deriveSaveStatus("a", "b", false, false)).toBe("dirty");
    expect(deriveSaveStatus("a", "a", false, false)).toBe("clean");
  });
});

describe("buildFileRunArgs", () => {
  it("emits positional and keyword args plus extra args", () => {
    const args = buildFileRunArgs(detail(), { topic: "release", mode: "fast" }, "extra key=value");
    expect(args).toEqual(["release", "mode=fast", "extra", "key=value"]);
  });

  it("skips defined args when hidden", () => {
    const file = detail({
      inputs: { defined_args: "hidden", extra_args: "hidden", message: "optional" },
    });
    expect(buildFileRunArgs(file, { topic: "x" }, "y")).toEqual([]);
  });
});

describe("hasMissingRequiredArgument", () => {
  it("detects a blank required argument", () => {
    const file = detail();
    expect(hasMissingRequiredArgument(file.arguments, file.inputs, {})).toBe(true);
    expect(hasMissingRequiredArgument(file.arguments, file.inputs, { topic: "x" })).toBe(false);
  });

  it("ignores arguments when hidden", () => {
    const file = detail({
      inputs: { defined_args: "hidden", extra_args: "hidden", message: "optional" },
    });
    expect(hasMissingRequiredArgument(file.arguments, file.inputs, {})).toBe(false);
  });
});

describe("blockingMessageKey", () => {
  it("maps shadow codes by source", () => {
    expect(blockingMessageKey("command_file_shadowed", { shadow_source: "member" })).toBe(
      "commands.shadow.member",
    );
    expect(blockingMessageKey("command_file_shadowed", { shadow_source: "template" })).toBe(
      "commands.shadow.template",
    );
    expect(blockingMessageKey("command_file_shadowed", {})).toBe("commands.shadow.generic");
  });

  it("maps other codes directly", () => {
    expect(blockingMessageKey("command_file_changed", {})).toBe(
      "commands.errors.command_file_changed",
    );
  });
});

describe("editor state persistence", () => {
  it("round-trips per workspace and ignores draft content", () => {
    saveEditorState(
      {
        selectedFileId: "greet-id",
        person: "bot",
        argValues: { topic: "x" },
        extraArgs: "e",
        message: "m",
        cwd: "/tmp",
        showAdvanced: true,
        history: [],
        activeTraceId: null,
        activeTab: "output",
      },
      "/workspace/a",
    );

    const loaded = loadEditorState("/workspace/a");
    expect(loaded.selectedFileId).toBe("greet-id");
    expect(loaded.showAdvanced).toBe(true);
    expect(loaded.cwd).toBe("/tmp");
    // A different workspace has independent state.
    expect(loadEditorState("/workspace/b").selectedFileId).toBeNull();
  });

  it("returns defaults without a storage dir", () => {
    expect(loadEditorState(undefined).selectedFileId).toBeNull();
  });
});
