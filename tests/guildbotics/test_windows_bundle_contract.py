from __future__ import annotations

import json
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
    assert "--bundles nsis" in frontend
