import assert from "node:assert/strict";
import { once } from "node:events";
import test from "node:test";

import { start, stop } from "./child.mjs";

const node = (source) => start(process.execPath, ["-e", source], { stdio: "ignore" });
const options = { timeout: 10_000 };

test("stop settles for a child that already exited with a code", options, async () => {
  const server = node("process.exit(1)");
  await once(server.child, "exit");
  await stop(server);
  assert.equal(await server.ended, "exit code 1");
});

for (const signal of ["SIGTERM", "SIGKILL"]) {
  test(`stop settles for a child that already ended by ${signal}`, options, async () => {
    const server = node("setInterval(() => {}, 1000)");
    server.child.kill(signal);
    await once(server.child, "exit");
    assert.equal(server.child.exitCode, null);
    await stop(server);
    assert.equal(await server.ended, signal);
  });
}

test("stop ends a running child and settles once it has", options, async () => {
  const server = node("setInterval(() => {}, 1000)");
  await once(server.child, "spawn");
  await stop(server);
  assert.equal(await server.ended, "SIGTERM");
});

test("a child that never started ends with the reason, and stop settles", options, async () => {
  const server = start("guildbotics-e2e-no-such-command", [], { stdio: "ignore" });
  assert.match(await server.ended, /ENOENT/);
  await stop(server);
});
