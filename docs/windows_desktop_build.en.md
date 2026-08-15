# Building GuildBotics Desktop on Windows

GuildBotics Desktop supports Windows x86_64 through the MSVC toolchain and produces an NSIS per-user installer. Build and smoke-test the installer on a Windows machine; macOS tests cover the portable logic but cannot validate Windows Job Objects, registry changes, or NSIS execution.

## Prerequisites

Install the following before cloning the repository:

- Visual Studio Build Tools 2022 with the **Desktop development with C++** workload and the Windows SDK.
- [rustup](https://rustup.rs/) with the stable `x86_64-pc-windows-msvc` toolchain.
- Node.js 24.
- [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.12 or later.
- Git for Windows. Run the repository build scripts from Git Bash.
- WebView2 Runtime. Windows 11 normally includes it; the NSIS installer uses Tauri's download-bootstrapper mode when it is absent.

NSIS is downloaded by Tauri during the build. WiX is not needed because the Windows bundle target is NSIS only. Code signing is outside the current scope, so the resulting installer is unsigned.

## Fresh-clone bootstrap

From Git Bash in the repository root:

```bash
uv sync --extra test --extra dev
cd desktop
npm ci
cd ..
```

## Native tests before bundling

Run the Windows-specific Python behavior and the Rust unit tests before building sidecars:

```bash
uv run --no-sync python -m pytest \
  tests/guildbotics/utils/test_processes.py \
  tests/guildbotics/runtime/test_service_control.py \
  tests/guildbotics/runtime/test_service_lock.py \
  tests/guildbotics/cli/test_start_command.py \
  tests/guildbotics/cli/test_stop_command.py \
  tests/guildbotics/cli/test_member_command.py \
  tests/guildbotics/intelligences/agent_runtime/test_environment.py

scripts/desktop-test-rust.sh
```

These tests must run natively on Windows. In particular, confirm that the GuildBotics process can create a nested Job Object, assign a suspended AI CLI process to it, and resume the process.
The Rust test wrapper clears Tauri's `externalBin` only for tests, so it works before the PyInstaller sidecars are built. Package builds still use the normal Tauri configuration and continue to require both sidecars.

## Build

From the repository root in Git Bash:

```bash
scripts/desktop-build-backend.sh
scripts/desktop-smoke-sidecars.sh
cd desktop
npm run tauri build -- --bundles nsis
```

The PyInstaller executables are written as:

- `desktop/src-tauri/binaries/guildbotics-app-api-x86_64-pc-windows-msvc.exe`
- `desktop/src-tauri/binaries/guildbotics-cli-x86_64-pc-windows-msvc.exe`

The NSIS installer is produced under `desktop/src-tauri/target/release/bundle/nsis/`. The Windows Tauri overlay fixes the bundle target to NSIS so a normal Windows build does not attempt an MSI build.

For `tauri dev`, `scripts/desktop-dev-tauri.sh` first builds real PyInstaller `.exe` sidecars. For faster frontend iteration, run `scripts/desktop-dev-backend.sh` in one Git Bash terminal and the Vite dev command in another; this avoids rebuilding the sidecars for each frontend change.

## Installation and PATH behavior

On first launch, Desktop copies the managed member CLI to `%USERPROFILE%\.guildbotics\bin\guildbotics.exe`. The NSIS install hook adds `%USERPROFILE%\.guildbotics\bin` to the current user's PATH only when an equivalent entry is absent. It records ownership only when it adds the entry, and uninstall removes only that owned entry.

Open a new cmd, PowerShell, or Git Bash session before testing bare `guildbotics`. Windows resolves the system PATH before the user PATH; a different system-wide `guildbotics` installation can therefore take precedence. Spawned AI CLI processes are unaffected because GuildBotics prepends its managed bin directory to their PATH.

## Smoke checklist

- Install with the generated NSIS installer and launch the app without an extra console window.
- Complete setup and create a workspace; verify the expected files are written.
- Save a secret and confirm it is stored in Windows Credential Manager. GuildBotics stores credential blobs as UTF-8 so PEM private keys remain within the 2,560-byte Credential Manager limit.
- Confirm `%USERPROFILE%\.guildbotics\bin\guildbotics.exe` exists and bare `guildbotics` resolves to it in new cmd, PowerShell, and Git Bash sessions.
- Start and stop the scheduler; verify duplicate start is rejected and the file-based graceful stop leaves no process behind.
- Run a command and confirm activity streaming in Desktop.
- Force-stop an AI CLI and verify no descendant process remains in Task Manager.
- Run an AI CLI workflow that commits and pushes with a multiline commit message containing spaces, Japanese text, `$`, and backticks. This validates the UTF-8 `--content-file` path.
- Run `to_pdf` and confirm the existing `PDF conversion requires WeasyPrint native dependencies.` error. The bundled CLI intentionally excludes WeasyPrint; only a normal Python installation with GTK/Pango/Cairo can provide it.
- Uninstall. The app, shortcuts, and installer-owned PATH entry must disappear. `%USERPROFILE%\.guildbotics`, AI CLI skills, workspaces, and Credential Manager entries must remain because they are user data.

Native `.sh` custom commands still require Bash and are not supported by the native Windows command path. Windows ARM64 and Windows code signing are also outside this build target.

## Moving an existing setup

Use `guildbotics secrets export` on the source machine and transfer the result through a secure channel, then run `guildbotics secrets import` on Windows. Copy the workspace directory when configuration and memory documents are needed; those documents are under `<workspace>/.guildbotics/state/documents`.

Windows builds predating the UTF-8 credential adapter used Python keyring's UTF-16 blob format. Those development credentials are in a different Credential Manager namespace and are not read by current builds; import the export file again after upgrading.
