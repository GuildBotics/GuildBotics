"""Tests for Branvil's development launcher, crates/branvil/scripts/dev.sh.

The launcher runs in a copy of the workspace's layout with fake tools: ``npm``,
``rustc``, a ``cargo`` that answers ``metadata`` the way Cargo does for the
workspace and builds a fake ``branvil``, and that ``branvil``, which records
its calls, prints the line ``branvil serve`` prints once it is ready, and
serves until the test releases it. The build goes through the development
image's cargo wrapper, copied as it is.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
WORKSPACE_MANIFEST = """[workspace]
members = ["crates/*"]

[profile.dev]
debug = "line-tables-only"
"""
RUSTC = "rustc 1.96.0 (test)\nrelease: 1.96.0"

HELPER = """\
import json, os, shlex, shutil, signal, sys, time
from pathlib import Path

tool, *args = sys.argv[1:]
env = os.environ


def option(name):
    return args[args.index(name) + 1] if name in args else None


def record(**extra):
    event = {
        "tool": tool,
        "args": args,
        "pid": os.getpid(),
        "binary": env.get("DEV_TEST_BINARY", ""),
        "token_present": "BRANVIL_TOKEN" in env,
        "url_present": "BRANVIL_URL" in env,
        **extra,
    }
    with open(env["DEV_TEST_EVENTS"], "a", encoding="utf-8") as out:
        out.write(json.dumps(event) + "\\n")


if tool == "rustc":
    print(env["DEV_TEST_RUSTC"])
elif tool == "npm":
    record()
    if args[-1] == "ci":
        marker = Path("web/node_modules/.package-lock.json")
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("installed", encoding="utf-8")
elif tool in ("cargo", "other-cargo"):
    manifest = Path(option("--manifest-path")).resolve()
    # The workspace: the topmost manifest above the crate's.
    root = [d for d in manifest.parents if (d / "Cargo.toml").is_file()][-1]
    target = Path(option("--target-dir") or env.get("CARGO_TARGET_DIR") or root / "target")
    record(stale=(target / "debug/stale").exists())
    if args[0] == "metadata":
        print(json.dumps({"target_directory": str(target), "workspace_root": str(root)}))
    elif args[0] == "clean":
        shutil.rmtree(target)
    elif args[0] == "build":
        made = target / "debug/branvil"
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_text(
            '#!/bin/sh\\nexport DEV_TEST_BINARY="$0"\\nexec '
            + shlex.join([sys.executable, sys.argv[0], "branvil"]) + ' "$@"\\n',
            encoding="utf-8",
        )
        made.chmod(0o755)
        sys.exit(int(env.get("DEV_TEST_BUILD_EXIT", "0")))
elif tool == "branvil":
    record()
    command = args[0]
    if command == "init":
        data = Path(option("--data-dir"))
        data.mkdir(parents=True, exist_ok=True)
        (data / "branvil.redb").touch()
    elif command == "serve":
        def stop(signum=None, _=None):
            sys.exit(0)

        signal.signal(signal.SIGINT, stop)
        print("branvil listening on http://127.0.0.1:7878 (base URL http://127.0.0.1:7878)", flush=True)
        release = Path(env["DEV_TEST_RELEASE"])
        while not release.exists():
            time.sleep(0.01)
    elif command == "open":
        if "--print" in args:
            print("http://example.invalid/-/enter#test-only-link")
            sys.exit(int(env.get("DEV_TEST_PRINT_EXIT", "0")))
        sys.exit(int(env.get("DEV_TEST_BROWSER_EXIT", "0")))
"""

pytestmark = pytest.mark.usefixtures("shell_commands")


class Launcher:
    """A copy of the workspace with fake tools, and the launcher runs in it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "checkout with spaces"
        crate = self.workspace / "crates/branvil"
        (crate / "scripts").mkdir(parents=True)
        (crate / "web").mkdir()
        shutil.copy(ROOT / "crates/branvil/scripts/dev.sh", crate / "scripts/dev.sh")
        wrapper = self.workspace / "docker/agent-environment/cargo-wrapper.sh"
        wrapper.parent.mkdir(parents=True)
        shutil.copy(ROOT / "docker/agent-environment/cargo-wrapper.sh", wrapper)
        (self.workspace / "Cargo.toml").write_text(WORKSPACE_MANIFEST, "utf-8")
        (crate / "Cargo.toml").write_text('[package]\nname = "branvil"\n', "utf-8")
        self.script = crate / "scripts/dev.sh"
        self.web = crate / "web"
        self.lock = self.web / "package-lock.json"
        self.lock.write_text("{}", encoding="utf-8")
        os.utime(self.lock, (1000, 1000))
        self.target = self.workspace / "target"
        self.data = crate / "data with spaces"
        tools = root / "tools"
        tools.mkdir()
        helper = root / "helper.py"
        helper.write_text(HELPER, encoding="utf-8")
        # tmutil: macOS, where the wrapper excludes a target it creates from backups.
        for tool in ["npm", "cargo", "rustc", "other-cargo", "tmutil"]:
            path = tools / tool
            path.write_text(
                "#!/bin/sh\nexec "
                + shlex.join([sys.executable, str(helper), tool])
                + ' "$@"\n',
                encoding="utf-8",
            )
            path.chmod(0o755)
        self.other_cargo = tools / "other-cargo"
        self.log = root / "events.jsonl"
        env = {key: value for key, value in os.environ.items() if key != "REAL_CARGO"}
        self.env = {
            **env,
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "DEV_TEST_EVENTS": str(self.log),
            "DEV_TEST_RUSTC": RUSTC,
            "BRANVIL_DATA_DIR": str(self.data),
            "BRANVIL_TOKEN": "test-unrelated-credential",
            "BRANVIL_URL": "http://example.invalid/unrelated",
        }
        self.runs = 0
        self.procs: list[subprocess.Popen[bytes]] = []

    def events(self, tool: str | None = None) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        events = [
            json.loads(line)
            for line in self.log.read_text(encoding="utf-8").splitlines(keepends=True)
            if line.endswith("\n")
        ]
        return [event for event in events if tool is None or event["tool"] == tool]

    def opens(self) -> list[dict[str, Any]]:
        return [e for e in self.events("branvil") if e["args"][0] == "open"]

    def server(self) -> dict[str, Any]:
        return next(e for e in self.events("branvil") if e["args"][0] == "serve")

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.script), *args],
            cwd=self.root,
            env={**self.env, **extra_env},
            capture_output=True,
            text=True,
            check=False,
        )

    def start(
        self, *args: str, **extra_env: str
    ) -> tuple[subprocess.Popen[bytes], Path, Path]:
        """Starts the launcher in a session of its own and waits until it serves."""
        self.runs += 1
        output = self.root / f"output-{self.runs}.txt"
        release = self.root / f"release-{self.runs}"
        with output.open("w", encoding="utf-8") as out:
            proc = subprocess.Popen(
                ["bash", str(self.script), *args],
                cwd=self.root,
                env={**self.env, "DEV_TEST_RELEASE": str(release), **extra_env},
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.procs.append(proc)
        wait_until(lambda: "Branvil is running." in output.read_text("utf-8"), proc)
        return proc, output, release

    @staticmethod
    def finish(run: tuple[subprocess.Popen[bytes], Path, Path]) -> None:
        proc, _, release = run
        release.touch()
        assert proc.wait(timeout=5) == 0

    def stop(self) -> None:
        """Stops every process of each launcher's group: its server can outlive it."""
        for proc in self.procs:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                deadline = time.monotonic() + 5
                try:
                    os.killpg(proc.pid, sig)
                    while time.monotonic() < deadline:
                        proc.poll()
                        os.killpg(proc.pid, 0)
                        time.sleep(0.02)
                except ProcessLookupError, PermissionError:
                    break  # only zombies left: EPERM on macOS
            proc.wait(timeout=5)


def wait_until(
    predicate: Callable[[], bool], proc: subprocess.Popen[bytes] | None = None
) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if predicate():
            return
        if proc is not None:
            assert proc.poll() is None, "The launcher exited before the expected event"
        time.sleep(0.02)
    pytest.fail("The launcher did not reach the expected event")


@pytest.fixture
def launcher(tmp_path: Path) -> Iterator[Launcher]:
    launcher = Launcher(tmp_path)
    yield launcher
    launcher.stop()


def test_the_launcher_runs_a_debug_build_of_the_crate_in_the_workspace_target(
    launcher: Launcher,
) -> None:
    launcher.finish(launcher.start("--no-open"))

    [build] = [e for e in launcher.events("cargo") if e["args"][0] == "build"]
    assert build["args"] == [
        "build",
        "--manifest-path",
        str(launcher.workspace / "crates/branvil/Cargo.toml"),
        "--target-dir",
        str(launcher.target),
    ]
    assert launcher.server()["binary"] == str(launcher.target / "debug/branvil")


def test_a_target_built_under_other_conditions_is_cleaned_before_the_build(
    launcher: Launcher,
) -> None:
    (launcher.target / "debug").mkdir(parents=True)
    (launcher.target / "debug/stale").touch()

    launcher.finish(launcher.start("--no-open"))
    launcher.finish(launcher.start("--no-open"))

    assert [
        (e["args"][0], e["stale"])
        for e in launcher.events("cargo")
        if e["args"][0] != "metadata"
    ] == [("clean", True), ("build", False), ("build", False)]
    assert (launcher.target / ".build-conditions").read_text("utf-8").startswith(RUSTC)


def test_the_image_cargo_named_by_real_cargo_builds(launcher: Launcher) -> None:
    launcher.finish(launcher.start("--no-open", REAL_CARGO=str(launcher.other_cargo)))

    assert {e["tool"] for e in launcher.events() if e["tool"].endswith("cargo")} == {
        "other-cargo"
    }


def test_a_failed_build_ends_the_launcher_with_its_status(launcher: Launcher) -> None:
    result = launcher.run("--no-open", DEV_TEST_BUILD_EXIT="101")

    assert result.returncode == 101
    assert launcher.events("branvil") == []


@pytest.mark.parametrize(
    ("state", "installs"),
    [
        ("missing-directory", True),
        ("missing-marker", True),
        ("current", False),
        ("changed-lock", True),
    ],
)
def test_dependency_installation_tracks_the_lockfile(
    launcher: Launcher, state: str, installs: bool
) -> None:
    modules = launcher.web / "node_modules"
    if state != "missing-directory":
        modules.mkdir()
    if state in ("current", "changed-lock"):
        marker = modules / ".package-lock.json"
        marker.write_text("installed", encoding="utf-8")
        os.utime(marker, (2000, 2000))
    if state == "changed-lock":
        launcher.lock.write_text('{"updated": true}', encoding="utf-8")
        os.utime(launcher.lock, (3000, 3000))

    launcher.finish(launcher.start("--no-open"))

    assert [call["args"] for call in launcher.events("npm")] == (
        [["--prefix", "web", "ci"]] if installs else []
    ) + [["--prefix", "web", "run", "build"]]


def test_a_browser_failure_prints_a_link_with_the_same_local_credentials(
    launcher: Launcher,
) -> None:
    run = launcher.start(DEV_TEST_BROWSER_EXIT="7")
    wait_until(lambda: len(launcher.opens()) == 2, run[0])
    launcher.finish(run)

    first, fallback = launcher.opens()
    assert "--print" not in first["args"]
    assert "--print" in fallback["args"]
    for call in (first, fallback):
        assert not call["token_present"]
        assert not call["url_present"]
        assert call["args"][-1] == str(launcher.data)
    output = run[1].read_text(encoding="utf-8")
    assert "http://example.invalid/-/enter#test-only-link" in output
    assert "60 seconds" in output


def test_a_failed_print_fallback_keeps_the_server_until_stopped(
    launcher: Launcher,
) -> None:
    run = launcher.start(DEV_TEST_BROWSER_EXIT="7", DEV_TEST_PRINT_EXIT="8")
    wait_until(lambda: len(launcher.opens()) == 2, run[0])
    launcher.finish(run)

    with pytest.raises(ProcessLookupError):
        os.kill(launcher.server()["pid"], 0)


def test_a_successful_open_prints_no_other_link(launcher: Launcher) -> None:
    run = launcher.start()
    wait_until(lambda: len(launcher.opens()) == 1, run[0])
    launcher.finish(run)

    [opened] = launcher.opens()
    assert "--print" not in opened["args"]


def test_termination_after_a_browser_failure_stops_the_server(
    launcher: Launcher,
) -> None:
    proc, _, _ = launcher.start(DEV_TEST_BROWSER_EXIT="7")
    wait_until(lambda: len(launcher.opens()) == 2, proc)
    proc.terminate()

    assert proc.wait(timeout=5) == 143
    with pytest.raises(ProcessLookupError):
        os.kill(launcher.server()["pid"], 0)


def test_no_open_skips_both_the_browser_and_the_link(launcher: Launcher) -> None:
    launcher.finish(launcher.start("--no-open", DEV_TEST_BROWSER_EXIT="7"))

    assert launcher.opens() == []
