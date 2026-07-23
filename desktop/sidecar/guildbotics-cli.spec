# PyInstaller spec for the GuildBotics member CLI bundled with the desktop app.
#
# The desktop bundle installs this binary as `guildbotics` in a user-accessible
# location on first launch, so external AI CLI tools can call `guildbotics member`.

import os

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))
ENTRY_POINT = os.path.join(REPO_ROOT, "guildbotics", "cli", "__main__.py")

datas = []
binaries = []
hiddenimports = []

hiddenimports += collect_submodules("guildbotics")
datas += collect_data_files("guildbotics")
datas += collect_data_files(
    "guildbotics",
    include_py_files=True,
    includes=["templates/commands/**/*.py"],
)

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
    name="guildbotics-cli",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=os.environ.get("GUILDBOTICS_PYINSTALLER_TARGET_ARCH") or None,
    codesign_identity=None,
    entitlements_file=None,
)
