# PyInstaller runtime hook: hold a shared lock on this program's executable
# for the life of the process.
#
# The desktop app installs each build in its own directory and moves away from
# an old build only when it can lock the executable exclusively
# (`install_programs` in desktop/src-tauri/src/lib.rs), so a running program
# keeps the files it started with. On Windows a running executable cannot be
# opened for writing, which gives the same answer.

import sys

if sys.platform != "win32":
    import fcntl

    # Never closed, so that the lock lasts as long as the process.
    _program_lock = open(sys.executable, "rb")  # noqa: SIM115
    fcntl.flock(_program_lock, fcntl.LOCK_SH)
