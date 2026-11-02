// Persist file and test timings, including successful CI runs.
import { mkdirSync, writeFileSync } from "node:fs";
import { availableParallelism, platform, release } from "node:os";
import { join } from "node:path";
import type { Reporter, TestModule, TestRunEndReason } from "vitest/node";

export default class TestTiming implements Reporter {
  private startedAt = 0;

  onTestRunStart() {
    this.startedAt = Date.now();
  }

  onTestRunEnd(modules: ReadonlyArray<TestModule>, _errors: ReadonlyArray<unknown>, reason: TestRunEndReason) {
    const finishedAt = Date.now();
    const files = modules.map((module) => ({
      file: module.relativeModuleId,
      state: module.state(),
      ...module.diagnostic(),
      tests: [...module.children.allTests()].map((test) => ({
        name: test.fullName,
        state: test.result().state,
        ...test.diagnostic(),
      })),
    }));
    const directory = process.env.BRANVIL_TEST_RESULTS ?? "test-results";
    mkdirSync(directory, { recursive: true });
    writeFileSync(join(directory, "timing.json"), JSON.stringify({
      startedAt: this.startedAt, finishedAt, elapsedMs: finishedAt - this.startedAt, reason,
      node: process.version, platform: platform(), release: release(), cpus: availableParallelism(),
      commit: process.env.BRANVIL_SHA, attempt: process.env.BRANVIL_ATTEMPT_ID,
      files,
    }, null, 2) + "\n");
  }
}
