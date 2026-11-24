"""Guest-only supervisor for a job using an OverlayFS RAM workspace."""

import contextlib
import ctypes
import json
import os
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

WORK = Path("/work")
RETURN = Path("/.branvil-ram-return")
CACHE = Path("/.branvil-cache")
stop = False


class WorkspaceRollbackError(RuntimeError):
    pass


def run(*args):
    subprocess.run(args, check=True, stdin=subprocess.DEVNULL)


def stop_job(_signum, _frame):
    global stop
    stop = True


def plain(path):
    """Look up a descendant of /work without accepting any symbolic links."""
    current = WORK
    parts = path.relative_to(WORK).parts
    if not parts or ".." in parts:
        return None
    try:
        for part in parts:
            current /= part
            mode = current.lstat().st_mode
            if stat.S_ISLNK(mode):
                return None
        return mode
    except (FileNotFoundError, NotADirectoryError):
        return None


def mount_workspace(paths, mib):
    base = Path(tempfile.mkdtemp(prefix=".branvil-ram-", dir="/"))
    mounted = []
    try:
        run(
            "mount",
            "-t",
            "tmpfs",
            "-o",
            f"size={mib}m,mode=0700,nosuid,nodev",
            "tmpfs",
            base,
        )
        mounted.append(base)
        layers = {}
        for index, relative in enumerate(paths):
            target = WORK / relative
            mode = plain(target)
            if mode is None or not stat.S_ISDIR(mode):
                raise ValueError(f"{relative!r} is missing or is not a directory without symlinks")
            layer = base / str(index)
            layer.mkdir()
            lower, upper, scratch = (
                layer / name for name in ("lower", "upper", "scratch")
            )
            for directory in (lower, upper, scratch):
                directory.mkdir()
            original = target.stat()
            os.chown(upper, original.st_uid, original.st_gid)
            upper.chmod(stat.S_IMODE(mode))
            run("mount", "--bind", target, lower)
            mounted.append(lower)
            run("mount", "-o", "remount,bind,ro", lower)
            run(
                "mount",
                "-t",
                "overlay",
                "-o",
                f"lowerdir={lower},upperdir={upper},workdir={scratch},index=off,metacopy=off,nosuid,nodev",
                "overlay",
                target,
            )
            mounted.append(target)
            layers[target] = upper
        return layers
    except (OSError, ValueError, subprocess.CalledProcessError):
        # No job has run yet. Only use the disk path if all partial mounts
        # were detached, so its artifact collector sees the same workspace.
        try:
            for path in reversed(mounted):
                run("umount", "-l", path)
            base.rmdir()
        except (OSError, subprocess.CalledProcessError) as error:
            raise WorkspaceRollbackError(
                "cannot detach the partial RAM workspace"
            ) from error
        raise


def produced(path, layers):
    for target, upper in layers.items():
        if path == target or target in path.parents:
            return (upper / path.relative_to(target)).is_file()
    return None


def files(paths):
    seen = set()
    for relative in paths:
        path = WORK / relative
        mode = plain(path)
        if mode is None:
            continue
        if stat.S_ISDIR(mode):
            candidates = (
                Path(directory) / name
                for directory, _, names in os.walk(path)
                for name in names
            )
        else:
            candidates = (path,)
        for file in candidates:
            if file in seen or not stat.S_ISREG(file.lstat().st_mode):
                continue
            seen.add(file)
            yield file


def identity(path):
    info = path.stat()
    return info.st_ino, info.st_ctime_ns, info.st_size


def collect(paths, layers, before):
    if not paths:
        return
    part = RETURN / "artifacts.part"
    archive = RETURN / "artifacts.tar"
    try:
        with part.open("wb") as out:
            with tarfile.open(fileobj=out, mode="w") as tar:
                for path in files(paths):
                    updated = produced(path, layers)
                    if updated or (
                        updated is None and identity(path) != before.get(path)
                    ):
                        tar.add(path, arcname=path.relative_to(WORK), recursive=False)
            out.flush()
            os.fsync(out.fileno())
        part.rename(archive)
    except OSError as error:
        part.unlink(missing_ok=True)
        print(
            f"[branvil] packing RAM workspace artifacts failed: {error}",
            file=sys.stderr,
        )


def protect(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0:
        raise ValueError(f"{path} is not a root-owned directory")
    if info.st_mode & 0o077:
        path.chmod(0o700)
        if path.lstat().st_mode & 0o077:
            raise ValueError(f"{path} is accessible to the job user")


def use_disk(error, args, *, detach=False):
    (RETURN / "disk").touch()
    if detach:
        # Remove the return mount before an unprivileged process starts.
        run("umount", "-l", RETURN)
    print(
        f"[branvil] RAM workspace disabled: {error}; using disk",
        file=sys.stderr,
        flush=True,
    )
    os.execvp(args[0], args)


def mount_cache(image, target):
    run("mount", "-t", "ext4", "-o", "loop", image, target)


def probe_cache():
    with tempfile.TemporaryDirectory(prefix=".branvil-cache-probe-") as directory:
        root = Path(directory)
        image, target = root / "probe.raw", root / "mount"
        target.mkdir()
        run("mkfs.ext4", "-q", "-F", image, "8M")
        mount_cache(image, target)
        run("umount", target)


def save_cache(command):
    (RETURN / "cache.requested").touch()
    image = RETURN / "cache.raw"
    deadline = time.monotonic() + 60
    while not image.is_file():
        if stop or (RETURN / "cache.unavailable").is_file():
            return
        if time.monotonic() >= deadline:
            raise TimeoutError("the runner did not provide a cache image")
        time.sleep(0.05)
    if stop:
        return
    result = RETURN / "cache-result"
    result.mkdir(mode=0o700)
    CACHE.mkdir(mode=0o700)
    mount_cache(image, CACHE)
    try:
        subprocess.run(
            ["/bin/sh", "-e", "-c", command],
            cwd=WORK,
            check=True,
            stdin=subprocess.DEVNULL,
        )
    finally:
        run("umount", CACHE)
    (result / "cache.kept").rename(RETURN / "cache.kept")


def main(mib, paths, artifacts, uid, keep_cache, command):
    drop = [
        "setpriv",
        "--no-new-privs",
        "--pdeathsig=keep",  # Changing the UID would clear the namespace init's signal.
        f"--reuid={uid}",
        f"--regid={uid}",
        "--clear-groups",
    ]
    shell = ["/bin/sh", "-c", command]
    signal.signal(signal.SIGUSR1, stop_job)
    try:
        protect(RETURN)
    except (OSError, ValueError) as error:
        use_disk(error, drop + shell, detach=True)

    try:
        # Reap the new PID namespace's init if its unshare parent is killed.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            raise OSError(ctypes.get_errno(), "cannot supervise the job process tree")
        layers = mount_workspace(paths, mib)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        use_disk(error, drop + shell)

    (RETURN / "pid").write_text(str(os.getpid()), encoding="ascii")
    print(f"[branvil] RAM workspace enabled ({mib} MiB)", file=sys.stderr, flush=True)
    before = {
        path: identity(path)
        for path in files(artifacts)
        if produced(path, layers) is None
    }
    process = subprocess.Popen(
        ["unshare", "--pid", "--fork", "--kill-child", "--mount-proc", *drop, *shell],
        start_new_session=True,
    )
    while not stop:
        try:
            process.wait(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            pass
    if stop:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        while True:
            try:
                os.waitpid(-1, 0)
            except ChildProcessError:
                break
    code = process.returncode
    code = code if code >= 0 else 128 - code
    if not stop:
        # The runner's command deadline excludes artifact packing.
        (RETURN / "exit.part").write_text(str(code), encoding="ascii")
        (RETURN / "exit.part").rename(RETURN / "exit")
    collect(artifacts, layers, before)
    if not stop and code == 0 and keep_cache:
        try:
            save_cache(keep_cache)
        except (OSError, subprocess.CalledProcessError) as error:
            print(
                f"[branvil] saving the RAM workspace cache failed: {error}",
                file=sys.stderr,
            )
    (RETURN / "finished").touch()
    return code


if __name__ == "__main__":
    if sys.argv[1:] == ["--probe-cache"]:
        probe_cache()
        sys.exit(0)
    mib, paths, artifacts, uid, keep_cache = json.loads(sys.argv[1])
    sys.exit(main(mib, paths, artifacts, uid, keep_cache, sys.argv[2]))
