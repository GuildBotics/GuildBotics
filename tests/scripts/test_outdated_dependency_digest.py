"""Tests for the outdated dependency digest parsers and rendering.

The parser fixtures are verbatim output captured from uv 0.9.28, npm 11.16.0,
and cargo 1.96.0 against this repository.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import outdated_dependency_digest as digest
import pytest

GENERATED_AT = datetime(2026, 8, 8, 22, 0, 0, tzinfo=UTC)

UV_TREE_OUTPUT = """\
Resolved 126 packages in 22ms
guildbotics
├── agno v2.8.7
├── langcodes[data] v3.5.1
├── websockets v16.1.1 (latest: v17.0.1)
├── mypy v2.3.0 (extra: dev)
├── ruff v0.16.2 (extra: dev) (latest: v0.17.0)
└── pytest v9.1.1 (extra: test)
"""

CARGO_OUTPUT = """\
    Updating crates.io index
     Locking 2 packages to latest compatible versions
   Unchanged generic-array v0.14.7 (available: v0.14.9)
    Updating thiserror v2.0.19 -> v2.0.20
    Updating thiserror-impl v2.0.19 -> v2.0.20
warning: not updating lockfile due to dry run
"""

NPM_OUTPUT = json.dumps(
    {
        "typescript": {
            "current": "5.9.3",
            "wanted": "5.9.3",
            "latest": "7.0.2",
            "dependent": "desktop",
        },
        "vite": {"current": "8.1.0", "wanted": "8.1.0", "latest": "8.1.0"},
        "react": {"wanted": "19.2.0", "latest": "19.3.0"},
    }
)


def test_parse_uv_tree_keeps_only_lines_annotated_with_a_latest_version() -> None:
    found = digest.parse_uv_tree(UV_TREE_OUTPUT)

    assert found == [
        digest.Outdated("pip", "websockets", "16.1.1", "17.0.1"),
        digest.Outdated("pip", "ruff", "0.16.2", "0.17.0"),
    ]


def test_parse_uv_tree_ignores_the_resolution_header_and_root() -> None:
    assert digest.parse_uv_tree("Resolved 126 packages in 22ms\nguildbotics\n") == []


def test_parse_npm_outdated_skips_current_and_uninstalled_packages() -> None:
    found = digest.parse_npm_outdated(NPM_OUTPUT)

    assert found == [digest.Outdated("npm", "typescript", "5.9.3", "7.0.2")]


def test_parse_npm_outdated_accepts_the_empty_output_of_an_up_to_date_tree() -> None:
    assert digest.parse_npm_outdated("") == []
    assert digest.parse_npm_outdated("{}") == []


def test_parse_cargo_update_reads_both_moved_and_held_back_crates() -> None:
    found = digest.parse_cargo_update(CARGO_OUTPUT)

    assert found == [
        digest.Outdated("cargo", "generic-array", "0.14.7", "0.14.9"),
        digest.Outdated("cargo", "thiserror", "2.0.19", "2.0.20"),
        digest.Outdated("cargo", "thiserror-impl", "2.0.19", "2.0.20"),
    ]


def test_parse_cargo_update_ignores_the_index_and_locking_notices() -> None:
    noise = "    Updating crates.io index\n     Locking 2 packages to latest\n"

    assert digest.parse_cargo_update(noise) == []


def test_item_id_combines_ecosystem_and_package_name() -> None:
    assert digest.Outdated("npm", "vite", "8.0.0", "8.1.0").item_id == "npm:vite"


def test_collect_reads_each_tool_from_the_stream_it_reports_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cargo prints its version report on stderr, uv and npm on stdout."""
    streams = {
        "uv": (UV_TREE_OUTPUT, "Resolved 126 packages in 22ms\n"),
        "npm": (NPM_OUTPUT, "npm notice New major version of npm available!\n"),
        "cargo": ("", CARGO_OUTPUT),
    }
    commands: list[list[str]] = []

    def fake_run(args, **kwargs):
        commands.append(list(args))
        stdout, stderr = streams[args[0]]
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=stderr)

    monkeypatch.setattr(digest.subprocess, "run", fake_run)

    found = digest.collect(Path("/repo"))

    assert [command[0] for command in commands] == ["uv", "npm", "cargo"]
    assert [item.item_id for item in found] == [
        "pip:websockets",
        "pip:ruff",
        "npm:typescript",
        "cargo:generic-array",
        "cargo:thiserror",
        "cargo:thiserror-impl",
    ]


def test_collect_tolerates_the_non_zero_exit_of_npm_outdated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[bool] = []

    def fake_run(args, **kwargs):
        checked.append(kwargs["check"])
        if kwargs["check"] and args[0] == "npm":
            raise AssertionError("npm outdated exits non-zero when it finds packages")
        return subprocess.CompletedProcess(args, 1, stdout="{}", stderr="")

    monkeypatch.setattr(digest.subprocess, "run", fake_run)

    assert digest.collect(Path("/repo")) == []
    assert checked == [True, False, True]


def test_render_body_groups_packages_under_their_ecosystem() -> None:
    items = [
        digest.Outdated("pip", "websockets", "16.1.1", "17.0.1"),
        digest.Outdated("npm", "typescript", "5.9.3", "7.0.2"),
        digest.Outdated("cargo", "thiserror", "2.0.19", "2.0.20"),
    ]

    body = digest.render_body(items, GENERATED_AT)

    assert "**更新可能 3 件** — Python 1 / desktop 1 / Tauri 1" in body
    assert "### Python (`uv.lock`)" in body
    assert "### desktop (`desktop/package-lock.json`)" in body
    assert "### Tauri (`desktop/src-tauri/Cargo.lock`)" in body
    assert "| `websockets` | `16.1.1` | `17.0.1` |" in body
    assert (
        body.index("### Python") < body.index("### desktop") < body.index("### Tauri")
    )


def test_render_body_omits_sections_for_ecosystems_that_are_up_to_date() -> None:
    items = [digest.Outdated("npm", "typescript", "5.9.3", "7.0.2")]

    body = digest.render_body(items, GENERATED_AT)

    assert "**更新可能 1 件** — Python 0 / desktop 1 / Tauri 1" not in body
    assert "**更新可能 1 件** — Python 0 / desktop 1 / Tauri 0" in body
    assert "### desktop (`desktop/package-lock.json`)" in body
    assert "### Python" not in body
    assert "### Tauri" not in body


def test_render_body_sorts_packages_case_insensitively_within_a_section() -> None:
    items = [
        digest.Outdated("pip", "Zope", "1.0", "2.0"),
        digest.Outdated("pip", "agno", "2.8.7", "2.9.0"),
    ]

    body = digest.render_body(items, GENERATED_AT)

    assert body.index("`agno`") < body.index("`Zope`")


def test_render_body_states_the_cargo_major_version_limitation() -> None:
    body = digest.render_body([], GENERATED_AT)

    assert "semver 互換の範囲のみ" in body
    assert "2026-08-08 22:00:00 UTC" in body
    assert digest.WORKFLOW_PATH in body


def test_render_new_outdated_comment_lists_only_the_newly_behind_packages() -> None:
    items = [
        digest.Outdated("pip", "websockets", "16.1.1", "17.0.1"),
        digest.Outdated("npm", "typescript", "5.9.3", "7.0.2"),
        digest.Outdated("cargo", "thiserror", "2.0.19", "2.0.20"),
    ]

    comment = digest.render_new_outdated_comment(items, {"npm:typescript"})

    assert comment.startswith("新たに更新可能となった依存が 1 件あります。")
    assert "`npm` / `typescript` — `5.9.3` → `7.0.2`" in comment
    assert "websockets" not in comment
    assert "thiserror" not in comment
