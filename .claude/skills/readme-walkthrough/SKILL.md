---
name: readme-walkthrough
description: Follow README.md or README.ja.md against the Rust app and CLI in an isolated test stack, and report discrepancies without editing the product.
---

# README walkthrough

Start an unseeded browser test stack and follow the README a new user would
read. Deliver a discrepancy report. Use the README as written, including its
order, examples, and control labels. A step that needs correction to work is
a finding. Leave product files and the README untouched.

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
GUILDBOTICS_E2E_STACK=readme \
GUILDBOTICS_E2E_BACKEND_PORT=8768 \
GUILDBOTICS_E2E_FRONTEND_PORT=1423 \
node e2e/start-stack.mjs
```

Check that those ports are free before starting. Leave seeding and environment
adoption disabled. Open `http://127.0.0.1:1423` after the stack prints its
context path. Use its `homeDir`, `workspaceDir` and backend port for CLI
steps. Read only those fields. Run the built CLI with `HOME` and
`USERPROFILE` set to that home, `GUILDBOTICS_HOME` set to its
`.guildbotics` directory, and `GUILDBOTICS_SERVER_PORT` set to the test
port. Clear inherited workspace, config, identity, Branvil and credential
overrides. For steps using the member command transport, invoke the example
binary with `member …` so its executable hash matches the server's.

Read the initial display language without changing it. Use the README in that
language, reread it from the start, and number each verifiable claim. Select
controls by their accessible name and choose options after opening the
dropdown. Scroll the workspace region if controls are below the fold.

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
  account, or supply real tokens. Report which setup paths remain unverified.
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
- **Operations.** Use the temporary stack for safe workspace and server
  reads and the in-memory vault for synthetic secrets. Compare other defaults,
  paths and CLI options with source and the CLI reference. OS keychain
  writes, AI CLI login, provider inference, LaunchAgent registration and
  real-account integrations require separate verification. List every
  applicable out-of-scope step.

## Clean up and report

Send SIGTERM to the stack process started by this run and wait for it. The
harness stops its backend and frontend and removes its context file. Verify
that ports 8768 and 1423 are free. If a child remains, identify it by this
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
