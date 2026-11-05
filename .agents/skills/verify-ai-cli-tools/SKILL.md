---
name: verify-ai-cli-tools
description: Verify pinned AI CLI releases on an existing GuildBotics update PR. Run selected Rust real-device tests with the owner's approval and observation for each execution.
---

# Verify AI CLI releases on an update PR

Inspect the PR's current head, changed pins, and verification already reported. Work in a separate checkout of that exact head. During the transition in #97, use the production checkout's own instructions for an update to the Python version.

## Prepare an isolated snapshot

Compile the Rust binary and test targets with the checkout's pinned toolchain:

```bash
cargo build --locked -p guildbotics
cargo test --locked -p guildbotics --test server --no-run
cargo test --locked -p gb-server --lib --no-run
cargo test --locked -p gb-environment --lib --no-run
```

On macOS or Linux, create a short disposable root to fit the microVM runtime's Unix socket paths:

```bash
export GUILDBOTICS_REAL_DEVICE_ROOT="$(mktemp -d /tmp/gbv.XXXXXX)"
GUILDBOTICS_REAL_DEVICE_ROOT="$(cd "$GUILDBOTICS_REAL_DEVICE_ROOT" && pwd -P)"
ROOT="$GUILDBOTICS_REAL_DEVICE_ROOT"
mkdir -p "$ROOT/guest-home" "$ROOT/machine" "$ROOT/workspace/.guildbotics/config"
export MSB_CONFIG_PATH="$ROOT/runtime-config.toml"
```

Leave `runtime-config.toml` absent so the test runtime uses its own storage paths.

Before **each** snapshot build, login, or real-device test, show the owner the exact command and revision, what traffic can leave the device, the effect on authentication, and the quota it can spend. Obtain explicit approval and run only while the owner can observe. The ignored-test flag merely selects the test.

After build approval:

```bash
HOME="$ROOT/guest-home" GUILDBOTICS_HOME="$ROOT/machine" \
  target/debug/guildbotics environment build --workspace "$ROOT/workspace"
```

The build downloads the base image and packages. It uses no provider login or inference quota. The build's recipe includes the pins. Rebuild after changing pins or the recipe. The same disposable guest HOME must be used for the snapshot and its tests.

## Run one approved test at a time

The `real_device` tests are marked `#[ignore]`, so regular Cargo and nextest runs compile them and skip their bodies. To inspect names, use `-- --list` with the test targets above. Substitute one complete name in an exact-match invocation and check that the output says it ran one test:

```bash
cargo test --locked -p guildbotics --test server \
  real_device::real_device_snapshot_loads_the_read_only_guest_package \
  -- --ignored --exact --nocapture

NO_PROXY=127.0.0.1 no_proxy=127.0.0.1 cargo test --locked -p gb-server --lib \
  tool_login::tests::real_device::real_device_provider_turn_keeps_the_login_on_the_host \
  -- --ignored --exact --nocapture
```

- The server tests for code mounting, commands, PDF, artifact extraction, and member git use a disposable workspace, runtime, and local Git repository. They use no provider login or inference quota.
- The server crate's synthetic login tests exercise the installed snapshot, credential loans, and private guest memory. The environment crate's owner-exit test kills the test-owned process and checks that its VM is removed. The fixture reclaims its named scratch disk. The gateway case forwards only to a loopback recorder. It uses synthetic credentials and has no inference quota cost.
- The server crate's `tool_login::tests::real_device::provider_contracts` tests run the pinned CLIs with synthetic credentials. Select the affected CLI's exact test name. A loopback HTTP and TLS recorder supplies scripted responses and captures direct provider destinations. These tests use no provider login or inference quota.
- The `real_device_{copilot,grok,antigravity}_resume` tests make provider requests and spend inference quota. The Antigravity working-directory and read-only tests and Copilot read-only test also make inference requests. Antigravity's read-only test attempts `example.com` and expects the VM to block it.
- The `real_device_copilot_usage` and `real_device_antigravity_usage` tests contact the provider for quota information. They start no inference turn. A provider may rotate authentication during a turn or usage probe.
- For Codex and Claude Code, run `real_device_codex_pong` or `real_device_claude_pong` in the server test target to check one turn through the product. For Codex also run `real_device_codex_sandbox` to check that a shell can use `/dev/null` and write to the VM disk while the permission profile denies reads of `~/.codex/auth.json`. These are separately approved inference turns with the tool's verification account. The account runs for the #95 port are tracked in #108.
- Keep bubblewrap out of the snapshot image so Codex uses its bundled helper. In the Codex turn logs, look for “Codex could not find bubblewrap on PATH”.

For account tests, use a separate OS account with a dedicated keychain and provider test accounts. On that OS account, get approval for each `environment login TOOL` using the same `HOME`, `GUILDBOTICS_HOME`, and `--workspace` as the build. A login contacts that provider and saves a sealed login under the disposable machine root. Only then add `GUILDBOTICS_REAL_DEVICE_ACCOUNT=1` to the exact test command. The tests copy that tool's sealed login to their own temporary machine state. Refresh may invalidate a provider token even when local files are separate, so use test accounts throughout.

## Record the result

Report each approved command and its result before asking to run another. On the same PR, record the tested revision and pins, executed commands, pass/fail/skip counts, and which behaviors remain unverified. Zero executed tests, a missing snapshot, and an unmet login prerequisite provide no verification.

Compare a failed contract with the pinned tool's observed requests and the broker routes or credential format. Keep credentials out of logs and fixtures. Follow AGENTS.md for failures already present on the base. After a pin, adapter, or recipe correction, rerun the affected check with fresh approval and attribute only observations from that revision.

Keep the authentication boundary intact when fixing a contract. If a tool still fails, restore its pin and associated changes to the PR base. Ask the owner to decide when the remedy is unclear.

When all builds and tests have ended, remove the disposable snapshots with `environment remove --workspace "$ROOT/workspace"` using the same two home variables. Remove the disposable root after snapshot removal succeeds. The OS account's keychain is separate from that root.
