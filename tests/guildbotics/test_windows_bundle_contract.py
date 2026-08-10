from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).parents[2]


def test_windows_tauri_overlay_builds_only_nsis_with_installer_hooks() -> None:
    config = json.loads(
        (ROOT / "desktop/src-tauri/tauri.windows.conf.json").read_text(encoding="utf-8")
    )

    assert config["bundle"]["targets"] == ["nsis"]
    hook = config["bundle"]["windows"]["nsis"]["installerHooks"]
    assert hook == "windows/installer-hooks.nsh"
    assert (ROOT / "desktop/src-tauri" / hook).is_file()


def test_packaged_desktop_csp_allows_local_api_avatars() -> None:
    config = json.loads(
        (ROOT / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    )

    csp = config["app"]["security"]["csp"]
    image_sources = next(
        directive.split()[1:]
        for directive in csp.split(";")
        if directive.strip().startswith("img-src ")
    )

    assert "http://127.0.0.1:*" in image_sources


def test_release_desktop_uses_the_windows_gui_subsystem() -> None:
    main = (ROOT / "desktop/src-tauri/src/main.rs").read_text(encoding="utf-8")

    assert main.startswith(
        '#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]\n'
    )


def test_nsis_hook_owns_only_the_path_entry_it_adds() -> None:
    hook = (ROOT / "desktop/src-tauri/windows/installer-hooks.nsh").read_text(
        encoding="utf-8"
    )

    assert 'ExpandEnvStrings $0 "$0"' in hook
    assert "GuildBoticsUserPathContainsEntry" in hook
    assert "path_entry_added" in hook
    assert "WriteRegDWORD" in hook
    assert "ReadRegDWORD" in hook
    assert "GuildBoticsRemoveOwnedPathEntry" in hook
    assert "DeleteRegValue" in hook
    assert "WM_SETTINGCHANGE" in hook


def test_nsis_path_normalizer_preserves_caller_registers() -> None:
    hook = (ROOT / "desktop/src-tauri/windows/installer-hooks.nsh").read_text(
        encoding="utf-8"
    )

    match = re.search(
        r"Function \$\{PREFIX\}GuildBoticsNormalizePath\n(?P<body>.*?)\nFunctionEnd",
        hook,
        re.DOTALL,
    )
    assert match is not None
    lines = match.group("body").splitlines()

    assert lines[:4] == [
        "  Exch $0",
        "  Push $1",
        "  Push $2",
        '  ExpandEnvStrings $0 "$0"',
    ]
    assert lines[-3:] == ["    Pop $2", "    Pop $1", "    Exch $0"]


def test_windows_scripts_use_exe_sidecars_and_real_dev_binaries() -> None:
    build_backend = (ROOT / "scripts/desktop-build-backend.sh").read_text(
        encoding="utf-8"
    )
    smoke = (ROOT / "scripts/desktop-smoke-sidecars.sh").read_text(encoding="utf-8")
    dev = (ROOT / "scripts/desktop-write-dev-binaries.sh").read_text(encoding="utf-8")
    frontend = (ROOT / "scripts/desktop-build-frontend.sh").read_text(encoding="utf-8")

    for script in (build_backend, smoke, dev, frontend):
        assert "*-pc-windows-msvc" in script
    assert 'SOURCE_SUFFIX=".exe"' in build_backend
    assert '"$SCRIPT_DIR/desktop-build-backend.sh"' in dev
    assert 'BUILD_ARGS=(--target "$DESKTOP_TARGET")' in frontend
    assert "BUILD_ARGS+=(--bundles nsis)" in frontend
    assert '"${BUILD_ARGS[@]}"' in frontend
    assert "BUILD_BUNDLES=()" not in frontend
    assert 'SMOKE_HOME="$(mktemp -d)"' in smoke
    assert "USERPROFILE" in smoke


def test_rust_tests_disable_packaged_sidecars_only_for_cargo_test() -> None:
    script = (ROOT / "scripts/desktop-test-rust.sh").read_text(encoding="utf-8")

    assert "TAURI_CONFIG=" in script
    assert '"externalBin":[]' in script
    assert 'cargo test "$@"' in script
