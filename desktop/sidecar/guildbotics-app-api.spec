# PyInstaller spec for the GuildBotics desktop sidecar (Local API daemon).
#
# Built by `.github/workflows/desktop-macos.yml` and reproducible locally with:
#
#   uv run --with pyinstaller python -m PyInstaller \
#       desktop/sidecar/guildbotics-app-api.spec --noconfirm
#
# Notes:
# - GuildBotics resolves brains / commands dynamically from config via
#   `guildbotics.utils.import_utils.load_class`, so the whole `guildbotics`
#   package (submodules + data files) must be collected, not just the modules
#   reachable by PyInstaller's static import graph.
# - `weasyprint` is intentionally NOT bundled. `ToPdfCommand` imports it lazily
#   and raises a friendly `CommandError` when its native libraries are missing,
#   so the sidecar stays buildable without GTK/Pango/Cairo. PDF conversion in
#   the packaged GUI is a known v1 limitation; the CLI remains the fallback.

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

# `SPECPATH` is injected by PyInstaller and points at this spec file's directory,
# so the entry point resolves correctly regardless of the working directory the
# build is invoked from (the CI workflow runs PyInstaller from the repo root).
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
ENTRY_POINT = os.path.join(REPO_ROOT, "guildbotics", "app_api", "__main__.py")

datas = []
binaries = []
hiddenimports = []

# Collect the whole guildbotics package (code + templates/locales/assets).
hiddenimports += collect_submodules("guildbotics")
datas += collect_data_files("guildbotics")

# Third-party packages that rely on dynamic imports / bundled data files.
for pkg in (
    "uvicorn",
    "fastapi",
    "starlette",
    "websockets",
    "agno",
    "google.genai",
    "openai",
    "anthropic",
):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports

a = Analysis(
    [ENTRY_POINT],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # weasyprint is loaded lazily by ToPdfCommand; exclude it so the build does
    # not fail on missing GTK/Pango/Cairo native libraries.
    excludes=["weasyprint"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="guildbotics-app-api",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch="arm64",
    codesign_identity=None,
    entitlements_file=None,
)
