// A real service with disposable data; never connect journeys to the user's Hub.
import { execFileSync, spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve, join } from "node:path";

export default async function setup() {
  const data = mkdtempSync(join(tmpdir(), "branvil-e2e-"));
  const binary = resolve("../../../target/debug/branvil");
  const base = new URL(process.env.BRANVIL_E2E_BASE_URL!);
  if (base.hostname !== "127.0.0.1") throw new Error("E2E requires a disposable loopback service");
  const env = { ...process.env };
  delete env.BRANVIL_TOKEN;
  const run = (...args: string[]) => execFileSync(binary, [...args, "--data-dir", data], { env, stdio: "pipe" });
  let server: ReturnType<typeof spawn> | undefined;
  const cleanup = async () => {
    if (server && server.exitCode === null) {
      const exited = new Promise<void>((done) => server!.once("exit", () => done()));
      server.kill("SIGTERM");
      await exited;
    }
    rmSync(data, { recursive: true, force: true });
  };
  try {
    run("init", "--listen", base.host);
    server = spawn(binary, ["serve", "--data-dir", data], { env, stdio: ["ignore", "pipe", "inherit"] });
    await new Promise<void>((done, fail) => {
      const timer = setTimeout(() => fail(new Error("Branvil did not start")), 30_000);
      server!.once("error", (error) => { clearTimeout(timer); fail(error); });
      server!.once("exit", () => { clearTimeout(timer); fail(new Error("Branvil exited before readiness")); });
      server!.stdout!.on("data", (chunk: Buffer) => {
        if (chunk.toString().includes("branvil listening on")) { clearTimeout(timer); done(); }
      });
    });
    run("workspace", "create", "acme", "--url", base.origin);
    run("repo", "create", "acme/demo", "--url", base.origin);
    process.env.BRANVIL_E2E_BIN = binary;
    process.env.BRANVIL_E2E_DATA_DIR = data;
    process.env.BRANVIL_E2E_MEMBER = "owner";
    process.env.BRANVIL_E2E_WORKSPACE = "acme";
    return cleanup;
  } catch (error) {
    await cleanup();
    throw error;
  }
}
