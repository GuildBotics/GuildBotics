import { readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

// Spec-side reader for the run context published by `e2e/start-stack.mjs`.
//
// The context describes ONE run's live processes (temp workspace, temp HOME,
// ports, token, seeded ids), so it lives in the OS temp dir next to the temp
// dirs it points at, never inside the repository. The path is derived from the
// stack name alone, which both sides already know statically, so no handshake
// is needed. `start-stack.mjs` mirrors this derivation on the writer side.

export function stackContextPath(stackName: string): string {
  return join(tmpdir(), "guildbotics-e2e", `${stackName}.json`);
}

export type StackContext = {
  stackName: string;
  workspaceDir: string;
  homeDir: string;
  configDir: string;
  /** Log the harness's AI CLI tool stubs append to when they are launched. */
  cliStubLog: string;
  backendPort: number;
  frontendPort: number;
  controlPort: number;
  token: string;
  host: string;
  seeded: boolean;
  seededWithoutLlmKey: boolean;
  deferBackend: boolean;
  memberId: string | null;
};

export function readStackContext(stackName: string): StackContext {
  return JSON.parse(readFileSync(stackContextPath(stackName), "utf-8")) as StackContext;
}
