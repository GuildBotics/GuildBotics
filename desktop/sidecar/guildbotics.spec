# PyInstaller spec for the Python programs bundled with the desktop app: the
# Local API daemon (`guildbotics-app-api`) and the member CLI (`guildbotics`),
# which the desktop app installs under ~/.guildbotics/bin for AI CLI tools.
#
# Built by the desktop packaging workflows and reproducible locally with:
#
#   uv run --with pyinstaller python -m PyInstaller \
#       desktop/sidecar/guildbotics.spec --noconfirm
#
# Notes:
# - Both programs are one directory (`dist/guildbotics/`) sharing `_internal/`.
#   A one-file build unpacks its whole content to a temporary directory on
#   every start, which took seconds per CLI call (and more where antivirus
#   scans what was unpacked).
# - On macOS the programs and every library in `_internal/` are signed here with
#   `APPLE_SIGNING_IDENTITY` when it is set: Tauri signs only what it bundles as
#   executables, and the directory is bundled as a resource.
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
PACKAGE_ROOT = os.path.join(REPO_ROOT, "guildbotics")
PROGRAMS = {
    "guildbotics-app-api": os.path.join(PACKAGE_ROOT, "app_api", "__main__.py"),
    "guildbotics": os.path.join(PACKAGE_ROOT, "cli", "__main__.py"),
}

datas = []
binaries = []
hiddenimports = []

# Collect the whole guildbotics package (code + templates/locales/assets).
hiddenimports += collect_submodules("guildbotics")
datas += collect_data_files("guildbotics")
datas += collect_data_files(
    "guildbotics",
    include_py_files=True,
    includes=["templates/commands/**/*.py"],
)

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
    # Windows has no system IANA timezone database. Bundle the dependency's
    # zoneinfo files so reset timestamps work in the packaged programs too.
    "tzdata",
    # The agent environment runtime: the wheel carries msb and libkrunfw under
    # `microsandbox/_bundled`, which GuildBotics copies to a fixed path on
    # first use (`agent_environment/runtime.py`).
    "microsandbox",
):
    pkg_datas, pkg_binaries, pkg_hiddenimports = collect_all(pkg)
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hiddenimports


def _program(name, entry_point):
    analysis = Analysis(
        [entry_point],
        pathex=[],
        binaries=binaries,
        datas=datas,
        hiddenimports=hiddenimports,
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[os.path.join(SPECPATH, "hold_program_lock.py")],
        # weasyprint is loaded lazily by ToPdfCommand; exclude it so the build
        # does not fail on missing GTK/Pango/Cairo native libraries.
        excludes=["weasyprint"],
        noarchive=False,
    )
    exe = EXE(
        PYZ(analysis.pure),
        analysis.scripts,
        [],
        exclude_binaries=True,
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=os.environ.get("GUILDBOTICS_PYINSTALLER_TARGET_ARCH") or None,
        codesign_identity=os.environ.get("APPLE_SIGNING_IDENTITY") or None,
        entitlements_file=None,
    )
    return [exe, analysis.binaries, analysis.datas]


COLLECT(
    *(part for name, entry in PROGRAMS.items() for part in _program(name, entry)),
    strip=False,
    upx=False,
    name="guildbotics",
)
