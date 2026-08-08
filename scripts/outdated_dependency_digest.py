#!/usr/bin/env python3
"""Report dependencies that have a newer release into a single tracking issue.

The Dependabot alert digest only covers dependencies with a known vulnerability.
This script covers the rest: packages that are merely behind, across the three
lockfiles in this repository. It deliberately reports instead of opening update
PRs, for the same reason Dependabot security updates are disabled — a bare
version bump without the accompanying test and behaviour fixes is not a
reviewable change.

Issue lifecycle and marker handling live in tracking_issue.py, which this script
shares with the Dependabot alert digest.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import tracking_issue

MARKER_PREFIX = "<!-- outdated-dependency-digest"
ISSUE_TITLE = "更新可能な依存の一覧"
WORKFLOW_PATH = ".github/workflows/outdated-dependency-digest.yml"
CLOSE_COMMENT = "更新可能な依存が無くなったため、この issue をクローズします。"

# Display label and lockfile for each ecosystem, in the order they are reported.
ECOSYSTEMS = (
    ("pip", "Python", "uv.lock"),
    ("npm", "desktop", "desktop/package-lock.json"),
    ("cargo", "Tauri", "desktop/src-tauri/Cargo.lock"),
)

# A tree line names the package first; the annotations that follow it (`latest`,
# `extra`, ...) are matched separately so their order does not matter.
UV_LINE = re.compile(
    r"^[\s│├└─]*(?P<name>[A-Za-z0-9._-]+)(?:\[[^\]]*\])? v(?P<current>[^\s(]+)"
)
UV_LATEST = re.compile(r"\(latest: v(?P<latest>[^)]+)\)")
CARGO_UPDATING = re.compile(
    r"^\s*Updating (?P<name>\S+) v(?P<current>\S+) -> v(?P<latest>\S+)\s*$"
)
CARGO_UNCHANGED = re.compile(
    r"^\s*Unchanged (?P<name>\S+) v(?P<current>\S+) \(available: v(?P<latest>\S+)\)"
)


class Outdated(NamedTuple):
    """One dependency that has a newer release available."""

    ecosystem: str
    name: str
    current: str
    latest: str

    @property
    def item_id(self) -> str:
        """Return the stable id used to detect newly outdated dependencies."""
        return f"{self.ecosystem}:{self.name}"


def parse_uv_tree(text: str) -> list[Outdated]:
    """Parse `uv tree --outdated` output into the packages behind their latest."""
    found = []
    for line in text.splitlines():
        latest = UV_LATEST.search(line)
        package = UV_LINE.match(line)
        if latest is not None and package is not None:
            found.append(
                Outdated(
                    "pip",
                    package["name"],
                    package["current"],
                    latest["latest"],
                )
            )
    return found


def parse_npm_outdated(payload: str) -> list[Outdated]:
    """Parse `npm outdated --json` output, skipping entries with no installed version.

    An entry without ``current`` means the package is not installed, which makes
    the comparison meaningless; the workflow runs `npm ci` first so that the
    installed versions are real.
    """
    entries = json.loads(payload) if payload.strip() else {}
    found = []
    for name, info in sorted(entries.items()):
        current, latest = info.get("current"), info.get("latest")
        if current and latest and current != latest:
            found.append(Outdated("npm", name, current, latest))
    return found


def parse_cargo_update(text: str) -> list[Outdated]:
    """Parse `cargo update --dry-run --verbose` output.

    Both the crates cargo would move and the ones it holds back report a newer
    version. Neither covers releases outside the declared semver range, so major
    bumps do not appear here.
    """
    found = []
    for line in text.splitlines():
        match = CARGO_UPDATING.match(line) or CARGO_UNCHANGED.match(line)
        if match is not None:
            found.append(Outdated("cargo", *match.group("name", "current", "latest")))
    return found


def render_body(items: Sequence[Outdated], generated_at: datetime) -> str:
    """Render the tracking issue body for the given outdated dependencies."""
    by_ecosystem = {
        ecosystem: sorted(
            (item for item in items if item.ecosystem == ecosystem),
            key=lambda item: item.name.lower(),
        )
        for ecosystem, _, _ in ECOSYSTEMS
    }
    counts = " / ".join(
        f"{label} {len(by_ecosystem[ecosystem])}" for ecosystem, label, _ in ECOSYSTEMS
    )

    lines = [
        "更新可能な依存の一覧です。脆弱性を伴うものは Dependabot アラートの issue で",
        "別途追跡しています。ここでは更新 PR は作らず、対応するかどうかの判断材料だけを",
        "並べます。",
        "",
        f"**更新可能 {len(items)} 件** — {counts}",
    ]

    for ecosystem, label, lockfile in ECOSYSTEMS:
        found = by_ecosystem[ecosystem]
        if not found:
            continue
        lines += [
            "",
            f"### {label} (`{lockfile}`)",
            "",
            "| Package | 現在 | 最新 |",
            "| --- | --- | --- |",
        ]
        lines += [
            f"| `{item.name}` | `{item.current}` | `{item.latest}` |" for item in found
        ]

    lines += [
        "",
        "<details><summary>検出方法と既知の制限</summary>",
        "",
        "- Python: `uv tree --outdated --depth 1` — 直接依存のみ。推移的依存は対象外",
        "- desktop: `npm outdated --json` — `npm ci` 後に実行",
        "- Tauri: `cargo update --dry-run --verbose` — **semver 互換の範囲のみ**。"
        "`Cargo.toml` の変更を伴うメジャー更新は検出できない",
        "",
        "</details>",
        "",
        "---",
        f"最終更新: {generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')} ・ "
        f"生成元: `{WORKFLOW_PATH}`",
        "更新可能な依存が無くなると、この issue は次回実行時に自動でクローズされます。",
    ]
    return "\n".join(lines) + "\n"


def render_new_outdated_comment(items: Sequence[Outdated], new_ids: set[str]) -> str:
    """Render the comment posted when dependencies fall behind for the first time."""
    added = sorted(
        (item for item in items if item.item_id in new_ids),
        key=lambda item: (item.ecosystem, item.name.lower()),
    )
    lines = [f"新たに更新可能となった依存が {len(added)} 件あります。", ""]
    lines += [
        f"- `{item.ecosystem}` / `{item.name}` — `{item.current}` → `{item.latest}`"
        for item in added
    ]
    lines += ["", "詳細は issue 本文の一覧を参照してください。"]
    return "\n".join(lines) + "\n"


def _run(
    args: Sequence[str], cwd: Path, check: bool = True, from_stderr: bool = False
) -> str:
    """Run a dependency tool and return the stream carrying its report.

    Args:
        args: Command and arguments to run.
        cwd: Directory to run the command in.
        check: Fail on a non-zero exit status.
        from_stderr: Read the report from stderr instead of stdout.
    """
    result = subprocess.run(
        list(args), cwd=cwd, capture_output=True, text=True, check=check
    )
    return result.stderr if from_stderr else result.stdout


def collect(repo_root: Path) -> list[Outdated]:
    """Collect outdated dependencies from every lockfile in the repository."""
    return [
        *parse_uv_tree(_run(["uv", "tree", "--outdated", "--depth", "1"], repo_root)),
        # npm outdated exits non-zero precisely when it finds outdated packages.
        *parse_npm_outdated(
            _run(["npm", "outdated", "--json"], repo_root / "desktop", check=False)
        ),
        # cargo prints its status lines, including the version report, on stderr.
        *parse_cargo_update(
            _run(
                ["cargo", "update", "--dry-run", "--verbose"],
                repo_root / "desktop" / "src-tauri",
                from_stderr=True,
            )
        ),
    ]


def main(argv: Sequence[str] | None = None) -> int:
    """Reconcile the tracking issue with the currently outdated dependencies."""
    parser = tracking_issue.common_parser(__doc__ or "")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root holding the lockfiles (default: this checkout).",
    )
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when $GITHUB_REPOSITORY is unset.")

    items = collect(args.repo_root)
    return tracking_issue.reconcile(
        args.repo,
        prefix=MARKER_PREFIX,
        title=ISSUE_TITLE,
        body=render_body(items, datetime.now(UTC)),
        item_ids=[item.item_id for item in items],
        close_comment=CLOSE_COMMENT,
        on_new_items=lambda new_ids: render_new_outdated_comment(items, new_ids),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
