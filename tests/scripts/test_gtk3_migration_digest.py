"""Tests for the GTK3 migration digest probe and rendering.

The lockfile fixtures are trimmed from real `cargo generate-lockfile` output
against `tauri = "*"` on cargo 1.96.0.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import gtk3_migration_digest as digest
import pytest

GENERATED_AT = datetime(2026, 8, 8, 22, 0, 0, tzinfo=UTC)

MANIFEST = """\
[package]
name = "guildbotics-desktop"
version = "0.1.0"

[build-dependencies]
tauri-build = { version = "2.6.3", features = [] }

[dependencies]
tauri = { version = "2.11.5", features = ["tray-icon"] }
serde = "1.0.229"

[target.'cfg(target_os = "macos")'.dependencies]
objc2 = "0.6"
"""


def lockfile(*packages: tuple[str, str]) -> str:
    """Render a lockfile holding the given (name, version) packages."""
    return "version = 4\n" + "".join(
        f'\n[[package]]\nname = "{name}"\nversion = "{version}"\n'
        for name, version in packages
    )


GTK3_LOCK = lockfile(("glib", "0.18.5"), ("gtk", "0.18.2"), ("tauri", "2.11.5"))


def probe(name: str, version: str, glib: str | None) -> digest.Probe:
    """Build a Probe without going through cargo."""
    return digest.Probe(name, version, glib)


def status(current: str | None, *probes: digest.Probe) -> digest.Status:
    """Build a Status from a committed glib version and probe results."""
    return digest.Status(current, tuple(probes))


BLOCKED = status(
    "0.18.5", probe("tauri", "2.11.5", "0.18.5"), probe("serde", "1.0.229", None)
)
ACTIONABLE = status(
    "0.18.5", probe("tauri", "3.0.0", "0.22.0"), probe("serde", "1.0.229", None)
)
DONE = status("0.22.0", probe("tauri", "3.0.0", "0.22.0"))


def test_version_key_orders_by_leading_numeric_components() -> None:
    assert digest.version_key("0.18.5") < digest.version_key("0.20.0")
    assert digest.version_key("0.9.0") < digest.version_key("0.18.5")
    assert digest.version_key("2.11.5") < digest.version_key("3.0.0")


def test_version_key_ignores_pre_release_metadata() -> None:
    assert digest.version_key("3.0.0-beta.1") == (3, 0, 0)


def test_is_patched_uses_the_first_glib_release_carrying_the_fix() -> None:
    assert not digest.is_patched("0.18.5")
    assert not digest.is_patched("0.19.9")
    assert digest.is_patched("0.20.0")
    assert digest.is_patched("0.22.0")


def test_lowest_version_reads_the_requested_crate() -> None:
    assert digest.lowest_version(GTK3_LOCK, "glib") == "0.18.5"
    assert digest.lowest_version(GTK3_LOCK, "tauri") == "2.11.5"


def test_lowest_version_returns_none_when_the_crate_is_absent() -> None:
    assert digest.lowest_version(GTK3_LOCK, "gtk4") is None


def test_lowest_version_picks_the_oldest_of_several_coexisting_majors() -> None:
    """An unpatched copy still blocks even when a patched one is also present."""
    lock = lockfile(("glib", "0.22.0"), ("glib", "0.18.5"))

    assert digest.lowest_version(lock, "glib") == "0.18.5"


def test_declared_dependencies_skips_build_and_target_sections() -> None:
    assert digest.declared_dependencies(MANIFEST) == ["serde", "tauri"]


def test_probe_requires_the_newest_version_and_reads_glib_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands: list[list[str]] = []

    def fake_run(args, **kwargs):
        commands.append(list(args))
        (Path(kwargs["cwd"]) / "Cargo.lock").write_text(GTK3_LOCK)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(digest.subprocess, "run", fake_run)

    found = digest.probe("tauri", tmp_path)

    assert commands == [["cargo", "generate-lockfile"]]
    assert '\ntauri = "*"\n' in (tmp_path / "Cargo.toml").read_text()
    assert (tmp_path / "src" / "main.rs").exists()
    assert found == digest.Probe("tauri", "2.11.5", "0.18.5")


def test_probe_reports_no_glib_for_a_dependency_outside_the_gtk_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(args, **kwargs):
        (Path(kwargs["cwd"]) / "Cargo.lock").write_text(lockfile(("serde", "1.0.229")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(digest.subprocess, "run", fake_run)

    assert digest.probe("serde", tmp_path) == digest.Probe("serde", "1.0.229", None)


def test_collect_probes_every_declared_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / digest.MANIFEST).parent.mkdir(parents=True)
    (tmp_path / digest.MANIFEST).write_text(MANIFEST)
    (tmp_path / digest.LOCKFILE).write_text(GTK3_LOCK)
    probed: list[str] = []

    def fake_probe(name: str, workdir: Path) -> digest.Probe:
        probed.append(name)
        return digest.Probe(name, "1.0.0", "0.18.5" if name == "tauri" else None)

    monkeypatch.setattr(digest, "probe", fake_probe)

    found = digest.collect(tmp_path)

    assert probed == ["serde", "tauri"]
    assert found.current == "0.18.5"
    assert [p.name for p in found.probes] == ["serde", "tauri"]


def test_blocked_state_reports_one_item_per_blocking_dependency() -> None:
    assert not BLOCKED.done
    assert not BLOCKED.actionable
    assert [p.name for p in BLOCKED.blockers] == ["tauri"]
    assert BLOCKED.item_ids == ["blocked:tauri:2"]


def test_item_id_tracks_only_the_major_so_patch_releases_stay_silent() -> None:
    assert probe("tauri", "2.11.5", "0.18.5").item_id == "blocked:tauri:2"
    assert probe("tauri", "2.12.0", "0.18.5").item_id == "blocked:tauri:2"
    assert probe("tauri", "3.0.0", "0.18.5").item_id == "blocked:tauri:3"


def test_upstream_fix_becomes_a_single_actionable_item() -> None:
    assert ACTIONABLE.actionable
    assert not ACTIONABLE.done
    assert ACTIONABLE.item_ids == ["actionable"]


def test_updated_lockfile_empties_the_items_so_the_issue_closes() -> None:
    assert DONE.done
    assert DONE.item_ids == []


def test_a_lockfile_without_glib_at_all_counts_as_done() -> None:
    assert status(None, probe("tauri", "3.0.0", None)).done


def test_render_body_names_the_blocking_dependency_while_waiting() -> None:
    body = digest.render_body(BLOCKED, GENERATED_AT)

    assert "**待ち**" in body
    assert (
        "依存を最新へ上げても `glib` は `0.18.5` のままです（blocker 1 件）。" in body
    )
    assert "| `tauri` | `2.11.5` | `0.18.5` |" in body
    assert "| `serde` | `1.0.229` | — |" in body
    assert digest.ADVISORY in body


def test_render_body_tells_the_reader_to_update_once_the_pin_lifts() -> None:
    body = digest.render_body(ACTIONABLE, GENERATED_AT)

    assert "**追従可能**" in body
    assert "**待ち**" not in body
    assert "| `tauri` | `3.0.0` | `0.22.0` |" in body


def test_render_body_states_the_completion_condition_and_the_probe_limits() -> None:
    body = digest.render_body(BLOCKED, GENERATED_AT)

    assert "desktop/src-tauri/Cargo.lock` の `glib` が 0.20 以上になっている" in body
    assert "prerelease を選ばない" in body
    assert "2026-08-08 22:00:00 UTC" in body
    assert digest.WORKFLOW_PATH in body


def test_render_body_stays_coherent_once_the_work_is_done() -> None:
    body = digest.render_body(DONE, GENERATED_AT)

    assert "**解消済み**" in body
    assert "届いていません" not in body


def test_render_new_state_comment_announces_that_work_can_start() -> None:
    comment = digest.render_new_state_comment(ACTIONABLE)

    assert "追従作業を開始できます。" in comment
    assert "`tauri` — 最新版 `3.0.0` / `glib` `0.22.0`" in comment
    assert "serde" not in comment


def test_render_new_state_comment_reports_a_major_bump_that_did_not_help() -> None:
    still_blocked = status("0.18.5", probe("tauri", "3.0.0", "0.18.5"))

    comment = digest.render_new_state_comment(still_blocked)

    assert "依然として" in comment
    assert "`tauri` — 最新版 `3.0.0` / `glib` `0.18.5`" in comment


def test_render_close_comment_quotes_the_version_that_met_the_condition() -> None:
    comment = digest.render_close_comment(DONE)

    assert "`0.22.0`" in comment
    assert "0.20 以上という完了条件" in comment


def test_main_reconciles_with_the_collected_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(digest, "collect", lambda repo_root: BLOCKED)
    monkeypatch.setattr(
        digest.tracking_issue,
        "reconcile",
        lambda repo, **kwargs: captured.update(repo=repo, **kwargs) or 0,
    )

    assert digest.main(["--repo", "o/r", "--dry-run"]) == 0
    assert captured["repo"] == "o/r"
    assert captured["prefix"] == digest.MARKER_PREFIX
    assert captured["title"] == digest.ISSUE_TITLE
    assert captured["item_ids"] == ["blocked:tauri:2"]
    assert captured["dry_run"] is True
