"""Tests for the development launcher, scripts/dev.sh.

The launcher runs in a copy of the repository's layout with fake tools:
``npm``, ``rustc``, a ``cargo`` that answers ``metadata`` the way Cargo does
for the workspace and builds a fake ``guildbotics``, and that ``guildbotics``,
which records its calls and the signals it gets, says what ``guildbotics
server start`` says once GuildBotics and Branvil listen, and serves until the
test releases it or a signal stops it. The build goes through the development
image's cargo wrapper and repository hook, copied as they are.
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
BRANVIL_LISTENED = Path(env["DEV_TEST_EVENTS"] + ".branvil")


def option(name):
    return args[args.index(name) + 1] if name in args else None


def record(**extra):
    event = {
        "tool": tool,
        "args": args,
        "pid": os.getpid(),
        "binary": env.get("DEV_TEST_BINARY", ""),
        "env": {
            name: env.get(name)
            for name in (
                "GUILDBOTICS_HOME",
                "BRANVIL_DATA_DIR",
                "GUILDBOTICS_SERVER_PORT",
                "GUILDBOTICS_IDENTITY_ROOT",
                "GUILDBOTICS_CONFIG_DIR",
                "GUILDBOTICS_WORKSPACE_ROOT",
                "GUILDBOTICS_LOG",
                "BRANVIL_TOKEN",
                "BRANVIL_URL",
            )
        },
        "branvil_listened": BRANVIL_LISTENED.exists(),
        **extra,
    }
    with open(env["DEV_TEST_EVENTS"], "a", encoding="utf-8") as out:
        out.write(json.dumps(event) + "\\n")


if tool == "rustc":
    print(env["DEV_TEST_RUSTC"])
elif tool == "npm":
    record()
    if args[-1] == "ci":
        marker = Path("node_modules/.package-lock.json")
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
        made = target / "debug/guildbotics"
        made.parent.mkdir(parents=True, exist_ok=True)
        made.write_text(
            '#!/bin/sh\\nexport DEV_TEST_BINARY="$0"\\nexec '
            + shlex.join([sys.executable, sys.argv[0], "guildbotics"]) + ' "$@"\\n',
            encoding="utf-8",
        )
        made.chmod(0o755)
        sys.exit(int(env.get("DEV_TEST_BUILD_EXIT", "0")))
elif tool == "guildbotics":
    record()
    if args[:2] == ["branvil", "init"]:
        data = Path(option("--data-dir"))
        data.mkdir(parents=True, exist_ok=True)
        (data / "branvil.redb").touch()
    elif args[:2] == ["server", "start"]:
        if "DEV_TEST_SERVER_FAILS" in env:
            sys.exit(int(env["DEV_TEST_SERVER_FAILS"]))

        # Every signal delivered, one byte each: Python runs a handler once
        # for signals that arrive before it does.
        wakeup, woken = os.pipe()
        os.set_blocking(wakeup, False)
        os.set_blocking(woken, False)
        signal.set_wakeup_fd(woken)
        for stop in (signal.SIGINT, signal.SIGTERM):
            signal.signal(stop, lambda *_: None)
        port = env["GUILDBOTICS_SERVER_PORT"]
        print(f"\\nGUILDBOTICS_APP_API_PORT={port}", flush=True)
        BRANVIL_LISTENED.unlink(missing_ok=True)
        branvil_at = time.monotonic() + float(env.get("DEV_TEST_BRANVIL_DELAY", "0"))
        release = Path(env["DEV_TEST_RELEASE"])
        stopped = None
        while not release.exists():
            if not BRANVIL_LISTENED.exists() and time.monotonic() >= branvil_at:
                BRANVIL_LISTENED.touch()
                print("INFO gb_server: Branvil ready on the shared listener", file=sys.stderr, flush=True)
            try:
                for signum in os.read(wakeup, 64):
                    record(signal=signal.Signals(signum).name)
                    print(f"stopping on {signal.Signals(signum).name}", file=sys.stderr, flush=True)
                    stopped = stopped or time.monotonic()
            except BlockingIOError:
                pass
            # Long enough for a second signal, had the launcher sent one.
            if stopped and time.monotonic() - stopped > 0.5:
                break
            time.sleep(0.01)
        sys.exit(int(env.get("DEV_TEST_SERVER_EXIT", "0")))
    elif args == ["open"]:
        sys.exit(int(env.get("DEV_TEST_OPEN_EXIT", "0")))
    elif args[:2] == ["branvil", "open"]:
        if "--print" in args:
            print("http://example.invalid/-/enter#test-only-link")
            sys.exit(int(env.get("DEV_TEST_PRINT_EXIT", "0")))
        time.sleep(float(env.get("DEV_TEST_OPEN_SLEEP", "0")))
        sys.exit(int(env.get("DEV_TEST_BROWSER_EXIT", "0")))
"""


class Launcher:
    """A copy of the repository with fake tools, and the launcher runs in it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "checkout with spaces"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copy(ROOT / "scripts/dev.sh", self.repo / "scripts/dev.sh")
        wrapper = self.repo / "docker/agent-environment/cargo-wrapper.sh"
        wrapper.parent.mkdir(parents=True)
        shutil.copy(ROOT / "docker/agent-environment/cargo-wrapper.sh", wrapper)
        hook = self.repo / ".cargo/before-cargo.sh"
        hook.parent.mkdir()
        shutil.copy(ROOT / ".cargo/before-cargo.sh", hook)
        (self.repo / "Cargo.toml").write_text(WORKSPACE_MANIFEST, "utf-8")
        crate = self.repo / "crates/guildbotics"
        crate.mkdir(parents=True)
        (crate / "Cargo.toml").write_text('[package]\nname = "guildbotics"\n', "utf-8")
        lock = self.repo / "package-lock.json"
        lock.write_text("{}", encoding="utf-8")
        os.utime(lock, (1000, 1000))
        self.script = self.repo / "scripts/dev.sh"
        self.target = self.repo / "target"
        self.home = root / "home"
        self.data = root / "data with spaces"
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
        env = {
            key: value
            for key, value in os.environ.items()
            if key not in ("REAL_CARGO", "GUILDBOTICS_SERVER_PORT", "GUILDBOTICS_LOG")
        }
        self.env = {
            **env,
            "HOME": str(self.home),
            "PATH": str(tools) + os.pathsep + os.environ["PATH"],
            "DEV_TEST_EVENTS": str(self.log),
            "DEV_TEST_RUSTC": RUSTC,
            "BRANVIL_TOKEN": "test-unrelated-credential",
            "BRANVIL_URL": "http://example.invalid/unrelated",
            "GUILDBOTICS_IDENTITY_ROOT": str(root / "production identities"),
            "GUILDBOTICS_CONFIG_DIR": str(root / "production/.guildbotics/config"),
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

    def calls(self, *prefix: str) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events("guildbotics")
            if e["args"][: len(prefix)] == list(prefix) and "signal" not in e
        ]

    def signals(self) -> list[str]:
        return [e["signal"] for e in self.events("guildbotics") if "signal" in e]

    def server(self) -> dict[str, Any]:
        return self.calls("server", "start")[0]

    def run(self, *args: str, **extra_env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(self.script), *args],
            cwd=self.root,
            env={**self.env, **extra_env},
            capture_output=True,
            text=True,
            check=False,
            # Its output ends once nothing it started is left.
            timeout=20,
        )

    def start(
        self, *args: str, **extra_env: str
    ) -> tuple[subprocess.Popen[bytes], Path, Path]:
        """Starts the launcher and waits until it serves."""
        run = self.spawn(*args, **extra_env)
        wait_until(lambda: "Press Ctrl-C to stop." in run[1].read_text("utf-8"), run[0])
        return run

    def spawn(
        self, *args: str, **extra_env: str
    ) -> tuple[subprocess.Popen[bytes], Path, Path]:
        """Starts the launcher in a session of its own."""
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


def test_the_launcher_runs_a_debug_build_of_the_server_in_the_workspace_target(
    launcher: Launcher,
) -> None:
    launcher.finish(launcher.start("--no-open"))

    [build] = [e for e in launcher.events("cargo") if e["args"][0] == "build"]
    assert build["args"] == [
        "build",
        "--manifest-path",
        str(launcher.repo / "crates/guildbotics/Cargo.toml"),
        "--target-dir",
        str(launcher.target),
    ]
    assert launcher.server()["binary"] == str(launcher.target / "debug/guildbotics")


def test_the_launcher_becomes_the_server_and_leaves_no_child_to_reap(
    launcher: Launcher,
) -> None:
    run = launcher.start("--no-open")
    proc = run[0]
    time.sleep(0.5)

    # Signals reach the server itself, with nothing in between.
    assert launcher.server()["pid"] == proc.pid
    children = subprocess.run(
        ["ps", "-A", "-o", "ppid=,stat="], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    states = [line.split()[1] for line in children if line.split()[0] == str(proc.pid)]
    assert not any(state.startswith("Z") for state in states), states
    launcher.finish(run)


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
    assert (
        (launcher.target / ".build-conditions-v2").read_text("utf-8").startswith(RUSTC)
    )


def test_the_image_cargo_named_by_real_cargo_builds(launcher: Launcher) -> None:
    launcher.finish(launcher.start("--no-open", REAL_CARGO=str(launcher.other_cargo)))

    assert {e["tool"] for e in launcher.events() if e["tool"].endswith("cargo")} == {
        "other-cargo"
    }


def test_a_failed_build_ends_the_launcher_with_its_status(launcher: Launcher) -> None:
    result = launcher.run("--no-open", DEV_TEST_BUILD_EXIT="101")

    assert result.returncode == 101
    assert launcher.events("guildbotics") == []


@pytest.mark.parametrize(
    ("state", "installs"),
    [
        ("missing-directory", True),
        ("missing-marker", True),
        ("current", False),
        ("changed-lock", True),
    ],
)
def test_dependency_installation_tracks_the_workspace_lockfile(
    launcher: Launcher, state: str, installs: bool
) -> None:
    modules = launcher.repo / "node_modules"
    if state != "missing-directory":
        modules.mkdir()
    if state in ("current", "changed-lock"):
        marker = modules / ".package-lock.json"
        marker.write_text("installed", encoding="utf-8")
        os.utime(marker, (2000, 2000))
    if state == "changed-lock":
        lock = launcher.repo / "package-lock.json"
        lock.write_text('{"updated": true}', encoding="utf-8")
        os.utime(lock, (3000, 3000))

    launcher.finish(launcher.start("--no-open"))

    assert [call["args"] for call in launcher.events("npm")] == (
        [["ci"]] if installs else []
    ) + [
        ["run", "build", "--workspace", "desktop", "--workspace", "crates/branvil/web"],
    ]


def test_the_server_runs_on_data_and_a_port_of_its_own(launcher: Launcher) -> None:
    launcher.finish(launcher.start("--no-open"))
    launcher.finish(
        launcher.start(
            "--no-open", "--data-dir", "relative data", GUILDBOTICS_SERVER_PORT="9911"
        )
    )

    default, chosen = (e["env"] for e in launcher.calls("server", "start"))
    assert default["GUILDBOTICS_HOME"] == str(launcher.home / ".gbb/dev/home")
    assert default["BRANVIL_DATA_DIR"] == str(launcher.home / ".gbb/dev/branvil")
    assert default["GUILDBOTICS_SERVER_PORT"] == "8866"
    assert chosen["GUILDBOTICS_HOME"] == str(launcher.root / "relative data/home")
    assert chosen["BRANVIL_DATA_DIR"] == str(launcher.root / "relative data/branvil")
    assert chosen["GUILDBOTICS_SERVER_PORT"] == "9911"


def test_the_server_reads_neither_identities_nor_a_workspace_named_elsewhere(
    launcher: Launcher,
) -> None:
    launcher.finish(launcher.start("--no-open"))
    launcher.finish(launcher.start("--no-open", GUILDBOTICS_LOG="warn"))
    launcher.finish(launcher.start("--no-open", GUILDBOTICS_LOG=""))

    first, second, third = (e["env"] for e in launcher.calls("server", "start"))
    assert first["GUILDBOTICS_IDENTITY_ROOT"] is None
    assert first["GUILDBOTICS_CONFIG_DIR"] is None
    # Whatever the log shows, it says when Branvil listens.
    assert first["GUILDBOTICS_LOG"] is None
    assert second["GUILDBOTICS_LOG"] == "warn,branvil::cli=info"
    assert third["GUILDBOTICS_LOG"] == "branvil::cli=info"


@pytest.mark.parametrize("device_has_one", [True, False])
def test_the_devices_runtime_is_linked_under_the_datas_home(
    launcher: Launcher, device_has_one: bool
) -> None:
    runtime = launcher.home / ".guildbotics/data/msb"
    if device_has_one:
        runtime.mkdir(parents=True)

    launcher.finish(launcher.start("--no-open", "--data-dir", str(launcher.data)))
    launcher.finish(launcher.start("--no-open", "--data-dir", str(launcher.data)))

    linked = launcher.data / "home/data/msb"
    assert linked.is_symlink() == device_has_one
    if device_has_one:
        assert linked.readlink() == runtime


def test_a_link_left_dangling_is_kept(launcher: Launcher) -> None:
    (launcher.home / ".guildbotics/data/msb").mkdir(parents=True)
    linked = launcher.data / "home/data/msb"
    linked.parent.mkdir(parents=True)
    linked.symlink_to(launcher.root / "gone")

    launcher.finish(launcher.start("--no-open", "--data-dir", str(launcher.data)))

    assert linked.readlink() == launcher.root / "gone"


def test_a_relative_workspace_is_the_callers(launcher: Launcher) -> None:
    launcher.finish(launcher.start("--no-open", GUILDBOTICS_WORKSPACE_ROOT="ws"))
    launcher.finish(
        launcher.start("--no-open", GUILDBOTICS_WORKSPACE_ROOT="/elsewhere/ws")
    )

    relative, absolute = (e["env"] for e in launcher.calls("server", "start"))
    assert relative["GUILDBOTICS_WORKSPACE_ROOT"] == str(launcher.root / "ws")
    assert absolute["GUILDBOTICS_WORKSPACE_ROOT"] == "/elsewhere/ws"


def test_branvil_is_set_up_once(launcher: Launcher) -> None:
    launcher.finish(launcher.start("--no-open", "--data-dir", str(launcher.data)))
    launcher.finish(
        launcher.start("--no-open", "--data-dir", str(launcher.data), "--owner", "x")
    )

    assert [e["args"] for e in launcher.calls("branvil", "init")] == [
        [
            "branvil",
            "init",
            "--data-dir",
            str(launcher.data / "branvil"),
            "--owner",
            "owner",
            "--listen",
            "127.0.0.1:8866",
        ]
    ]


def test_both_screens_open_with_this_data_directorys_credentials(
    launcher: Launcher,
) -> None:
    # What the latest start said is not what this one says.
    launcher.data.mkdir()
    (launcher.data / "server.out").write_text(
        "\nGUILDBOTICS_APP_API_PORT=8866\n", "utf-8"
    )
    (launcher.data / "server.log").write_text("listening on http://old\n", "utf-8")
    run = launcher.start("--data-dir", str(launcher.data), DEV_TEST_BRANVIL_DELAY="1")
    wait_until(lambda: len(launcher.calls("branvil", "open")) == 1, run[0])
    launcher.finish(run)

    [guildbotics] = launcher.calls("open")
    assert guildbotics["env"]["GUILDBOTICS_HOME"] == str(launcher.data / "home")
    [branvil] = launcher.calls("branvil", "open")
    assert branvil["args"] == [
        "branvil",
        "open",
        "--data-dir",
        str(launcher.data / "branvil"),
    ]
    assert branvil["env"]["BRANVIL_TOKEN"] is None
    assert branvil["env"]["BRANVIL_URL"] is None
    # Branvil starts listening after GuildBotics does: its screen waits for it.
    assert branvil["branvil_listened"]


def test_a_browser_failure_prints_a_branvil_link_and_keeps_serving(
    launcher: Launcher,
) -> None:
    run = launcher.start(DEV_TEST_BROWSER_EXIT="7")
    wait_until(lambda: len(launcher.calls("branvil", "open")) == 2, run[0])
    launcher.finish(run)

    first, fallback = launcher.calls("branvil", "open")
    assert "--print" not in first["args"]
    assert "--print" in fallback["args"]
    for call in (first, fallback):
        assert call["env"]["BRANVIL_TOKEN"] is None
        assert call["env"]["BRANVIL_URL"] is None
    output = run[1].read_text(encoding="utf-8")
    assert "http://example.invalid/-/enter#test-only-link" in output
    assert "60 seconds" in output


def test_a_failed_print_fallback_keeps_the_server_until_stopped(
    launcher: Launcher,
) -> None:
    run = launcher.start(DEV_TEST_BROWSER_EXIT="7", DEV_TEST_PRINT_EXIT="8")
    wait_until(lambda: len(launcher.calls("branvil", "open")) == 2, run[0])
    assert run[0].poll() is None
    launcher.finish(run)

    with pytest.raises(ProcessLookupError):
        os.kill(launcher.server()["pid"], 0)


def test_termination_after_a_browser_failure_stops_the_server(
    launcher: Launcher,
) -> None:
    proc, _, _ = launcher.start(DEV_TEST_BROWSER_EXIT="7", DEV_TEST_SERVER_EXIT="5")
    wait_until(lambda: len(launcher.calls("branvil", "open")) == 2, proc)
    proc.terminate()

    assert proc.wait(timeout=5) == 5
    assert launcher.signals() == ["SIGTERM"]


def test_a_server_that_no_longer_answers_opens_no_branvil_screen(
    launcher: Launcher,
) -> None:
    run = launcher.start(DEV_TEST_OPEN_EXIT="1")
    wait_until(lambda: launcher.calls("open") != [], run[0])
    time.sleep(0.5)
    launcher.finish(run)

    assert launcher.calls("branvil", "open") == []


def test_no_open_skips_both_screens(launcher: Launcher) -> None:
    launcher.finish(launcher.start("--no-open"))

    assert launcher.calls("open") == []
    assert launcher.calls("branvil", "open") == []


def test_a_ctrl_c_reaches_the_server_once_and_ends_with_it(
    launcher: Launcher,
) -> None:
    proc, output, _ = launcher.start(
        "--no-open", "--data-dir", str(launcher.data), DEV_TEST_SERVER_EXIT="3"
    )
    os.killpg(proc.pid, signal.SIGINT)

    assert proc.wait(timeout=5) == 3
    # A second one would tell the server to cancel the commands it runs.
    assert launcher.signals() == ["SIGINT"]
    # What the server says as it stops still reaches the terminal and the log.
    wait_until(lambda: "stopping on SIGINT" in output.read_text(encoding="utf-8"))
    assert "stopping on SIGINT" in (launcher.data / "server.log").read_text("utf-8")


def test_the_log_keeps_what_the_server_says_as_any_signal_stops_it(
    launcher: Launcher,
) -> None:
    proc, _, _ = launcher.start("--no-open", "--data-dir", str(launcher.data))
    os.killpg(proc.pid, signal.SIGTERM)

    proc.wait(timeout=5)
    wait_until(
        lambda: (
            "stopping on SIGTERM" in (launcher.data / "server.log").read_text("utf-8")
        )
    )


def test_a_termination_after_a_ctrl_c_reaches_the_server_as_it_is(
    launcher: Launcher,
) -> None:
    proc, _, _ = launcher.start("--no-open")
    os.killpg(proc.pid, signal.SIGINT)
    wait_until(lambda: launcher.signals() == ["SIGINT"], proc)
    proc.terminate()

    proc.wait(timeout=5)
    assert launcher.signals() == ["SIGINT", "SIGTERM"]


@pytest.mark.parametrize(
    ("while_", "extra"),
    [
        ("waiting for branvil", {"DEV_TEST_BRANVIL_DELAY": "30"}),
        ("opening branvil", {"DEV_TEST_OPEN_SLEEP": "30"}),
    ],
)
def test_a_ctrl_c_during_startup_reaches_the_server_once_too(
    launcher: Launcher, while_: str, extra: dict[str, str]
) -> None:
    proc, output, _ = launcher.spawn(DEV_TEST_SERVER_EXIT="3", **extra)
    if while_ == "waiting for branvil":
        wait_until(lambda: launcher.calls("server", "start") != [], proc)
    else:
        wait_until(lambda: launcher.calls("branvil", "open") != [], proc)
    time.sleep(0.3)
    os.killpg(proc.pid, signal.SIGINT)

    assert proc.wait(timeout=5) == 3
    assert launcher.signals() == ["SIGINT"]


def test_termination_stops_the_server(launcher: Launcher) -> None:
    proc, _, _ = launcher.start("--no-open", DEV_TEST_SERVER_EXIT="5")
    proc.terminate()

    assert proc.wait(timeout=5) == 5
    assert launcher.signals() == ["SIGTERM"]
    with pytest.raises(ProcessLookupError):
        os.kill(launcher.server()["pid"], 0)


def test_a_server_that_cannot_start_ends_the_launcher_with_its_status(
    launcher: Launcher,
) -> None:
    result = launcher.run(DEV_TEST_SERVER_FAILS="4")

    assert result.returncode == 4
    assert "Press Ctrl-C" not in result.stdout
    assert launcher.calls("open") == []
