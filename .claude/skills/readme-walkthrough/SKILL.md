---
name: readme-walkthrough
description: Follow README.md or README.ja.md against the Rust app and CLI in an isolated test stack, and report discrepancies without editing the product.
---

# README walkthrough

Start an unseeded browser test stack and follow the README a new user would
read. Deliver a discrepancy report. Use the README as written, including its
order, examples, and control labels. A step that needs correction to work is
a finding. Leave product files and the README untouched. A section argument
such as `quick-start`, `slack`, or a heading limits which steps to report.
Without an argument, walk the whole README.

## Start the test stack

From the repository root, build the screens, the Rust browser test server,
and the product CLI. The test server runs the real HTTP API with an
in-memory keychain.

```sh
npm ci
npm run build --workspace desktop
cargo build --locked -p gb-server --example browser-test-server
cargo build --locked -p guildbotics
cd desktop
NO_PROXY=127.0.0.1 no_proxy=127.0.0.1 \
GUILDBOTICS_E2E_STACK="readme-$(node -p 'require("node:crypto").randomUUID()')" \
GUILDBOTICS_E2E_BACKEND_PORT=8768 \
GUILDBOTICS_E2E_SEED=0 \
GUILDBOTICS_E2E_ADOPT_ENVIRONMENT= \
GUILDBOTICS_E2E_CONTROL_PORT=0 \
node e2e/start-stack.mjs
```

Check that port 8768 is free before starting, or choose another free port.
Wait for the stack to print `ready`. Use the context path printed by this
run to request a one-time entry link from its own loopback server. Pass the
port chosen above as the second argument:

```sh
NO_PROXY=127.0.0.1 no_proxy=127.0.0.1 \
node --input-type=module - /absolute/path/to/guildbotics-e2e/readme-UUID.json 8768 <<'JS'
import { readFileSync } from "node:fs";
import { join } from "node:path";

const { homeDir, backendPort } = JSON.parse(readFileSync(process.argv[2], "utf8"));
if (backendPort !== Number(process.argv[3])) throw new Error("Unexpected stack port");
const token = readFileSync(
  join(homeDir, ".guildbotics", "data", "server", String(backendPort), "owner.token"),
  "utf8",
).trim();
const response = await fetch(`http://127.0.0.1:${backendPort}/-/guildbotics/api/session/links`, {
  method: "POST",
  headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
  body: "{}",
  redirect: "error",
});
if (!response.ok) throw new Error(`Entry link failed: HTTP ${response.status}`);
console.log((await response.json()).url);
JS
```

Open the returned link in a fresh browser session. Keep the link and the
owner token out of the report. The test server serves both the API and the
screens on that port. Check `guildbotics open` against its source and CLI
reference because the product command can fall back to the OS keychain.

Read the initial display language without changing it. Pick the README in
that language even when a section argument was supplied. Read the selected
steps in order and number each verifiable claim. Select controls by their
accessible name and choose options after opening the dropdown. Scroll the
workspace region if controls are below the fold.

## Verify each step

Record the README line, the action, what the screen, CLI or saved files
actually show, and a verdict of match, mismatch, unverified, or out of scope.

- **Setup and files.** Complete the setup in the temporary workspace. Check
  the project, sample commands, AI settings, member participation and
  identity files in the locations described. Compare the translation sample
  with the generated source and check precedence in `gb-config/src/commands.rs`.
  The test stack deliberately prevents microVM startup. Check that the UI
  surfaces the reason. Mark translation output unverified unless separately
  authorized to run the real quick start on a disposable Hub.
- **Pair work and installation.** Check the CLI and skill paths against
  `crates/guildbotics/src/install_cli.rs` and
  `guildbotics/templates/environment/cli_provision.json`. The actual
  installation and LaunchAgent registration are outside the walkthrough.
- **Branvil and Slack.** Check controls up to connection and compare required
  scopes and saved settings with the Rust setup providers. Use disposable
  loopback Branvil data for any end-to-end Branvil checks. Do not contact a
  live Branvil, GitHub or Slack instance, open consent pages for a real
  account, or supply real tokens. To exercise Slack's local rejection, enter
  obviously fake values **without** the `xoxb-` and `xapp-` prefixes. Check
  that the screen reports a wrong token type for both. Values with the correct
  prefixes make the verifier attempt Slack API calls. Report external
  verification as out of scope.
- **Service.** Check the three source toggles, patrol interval,
  consecutive-failure limit, logs, and Activity. In the offline stack,
  verify start/stop only with a temporary workspace and no connected
  services. The first setup requires an active member. Where the runtime is
  separately approved, give that member only a shell routine
  `sleepy.sh` under the workspace's `.guildbotics/config/commands/` with
  `sleep 60`. Set `routine_commands: [sleepy]` in its `member.yml`.
  A routine starts immediately. Stop while it is in flight, then inspect
  cancellation in Diagnostics or the run records. Report force-stop as
  unverified if the run could not be made to enter that state.
- **Operations.** Run only `workspace status`, `workspace current`, and
  `workspace use` from the product CLI. From the repository root, replace
  `stack_home` with the context's `homeDir` and use the stack port:

  ```sh
  stack_home=/absolute/path/to/temporary/stack-home
  env -i PATH="$PATH" HOME="$stack_home" USERPROFILE="$stack_home" \
    GUILDBOTICS_HOME="$stack_home/.guildbotics" GUILDBOTICS_SERVER_PORT=8768 \
    target/debug/guildbotics workspace status
  ```

  Give `workspace use` only the context's `workspaceDir`. Use the UI to
  check synthetic secret storage in the test server's in-memory vault.
  Review other commands against the CLI reference and source, including
  commands that can access the OS keychain in the CLI process such as
  `secrets`, `connect`, `open`, `git-credential`, `run` and
  `environment status`. The temporary HOME does not isolate that keychain.
  List these CLI operations, AI CLI login, provider inference, LaunchAgent
  registration and real-account integrations as out of scope.

## Clean up and report

Send SIGTERM to the stack process started by this run and wait for it. The
harness stops its test server and removes its context file. Verify
that the chosen port is free. If a child remains, identify it by this
stack's PID and port before stopping it. Delete any scratch export containing
synthetic secrets. Use `git status --short` to verify that the checkout has
only its original changes.

Write a Japanese report to
`tmp/readme-walkthrough-report-<YYYYMMDD>.md`. Give the README and language
selection, date, checklist count and counts by verdict. For each mismatch,
show the exact README line, observed result and whether the source or
documentation needs correction. Check the counterpart section in the other
language. List unverified and out-of-scope steps with reasons. Summarize the
findings in the conversation and leave fixes for a separately requested task.
