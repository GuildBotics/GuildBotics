#!/usr/bin/env python3
"""Track when the desktop dependency tree can leave the unmaintained glib 0.18 line.

`gtk` 0.18 (the GTK3 bindings Tauri uses on Linux) is archived upstream and
requires `glib` ^0.18, which carries the unsoundness reported as
GHSA-wrw7-89jp-8q8g. Neither a `gtk` nor a `glib` 0.18 release will ever fix
this, so the pin only lifts once Tauri itself stops depending on GTK3. That is
expected to arrive as a breaking release, which the outdated dependency digest
cannot see: it reads `cargo update`, and cargo never crosses a major boundary.

This digest answers the two questions that decide whether the work can start:

- does the committed lockfile still resolve `glib` below the patched line?
- would raising every declared dependency to its newest published version
  lift the pin?

The second question is answered by asking cargo, not by watching a list of
upstream crates. Each declared dependency is resolved on its own in a throwaway
manifest that requires `"*"`, and the resulting lockfile is read back. That
keeps the check correct no matter how Tauri gets off GTK3 — a v3 major, a switch
from tao to winit, or a plugin that lags behind — and it needs no hand-kept list
of crates to watch. Resolving one dependency at a time also means a plugin that
has not yet caught up with a new Tauri major shows up as a blocker rather than
failing the whole resolution.

Issue lifecycle and marker handling live in tracking_issue.py, which this script
shares with the other two digests.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import NamedTuple

import tracking_issue

MARKER_PREFIX = "<!-- gtk3-migration-digest"
ISSUE_TITLE = "glib 0.18 系固定を解消するため Tauri の GTK3 脱却を追跡する"
WORKFLOW_PATH = ".github/workflows/gtk3-migration-digest.yml"

MANIFEST = Path("desktop/src-tauri/Cargo.toml")
LOCKFILE = Path("desktop/src-tauri/Cargo.lock")
WATCHED_CRATE = "glib"
ADVISORY = "GHSA-wrw7-89jp-8q8g"
ADVISORY_URL = "https://github.com/advisories/GHSA-wrw7-89jp-8q8g"
# First release of glib carrying the VariantStrIter soundness fix.
PATCHED = (0, 20)

PROBE_MANIFEST = """\
[package]
name = "glib-probe"
version = "0.0.0"
edition = "2021"

[dependencies]
{name} = "*"
"""

NUMERIC_PREFIX = re.compile(r"\d+")


def version_key(version: str) -> tuple[int, ...]:
    """Return the leading numeric components of a version, for ordering.

    Pre-release and build metadata are dropped: the comparison only has to place
    a version relative to the patched line, and cargo never selects a
    pre-release for a `"*"` requirement anyway.
    """
    return tuple(int(part) for part in NUMERIC_PREFIX.findall(version.split("-")[0]))


def is_patched(version: str) -> bool:
    """Report whether a glib version is at or past the patched release."""
    return version_key(version)[: len(PATCHED)] >= PATCHED


class Probe(NamedTuple):
    """One declared dependency resolved at its newest published version."""

    name: str
    version: str
    glib: str | None

    @property
    def blocked(self) -> bool:
        """Report whether this dependency still pulls an unpatched glib."""
        return self.glib is not None and not is_patched(self.glib)

    @property
    def item_id(self) -> str:
        """Return the id whose change is worth a notification.

        Only the major version is carried, so the routine monthly run stays
        silent through patch releases and comments exactly once when a
        dependency crosses a major boundary while still holding glib back.
        """
        return f"blocked:{self.name}:{version_key(self.version)[0]}"


class Status(NamedTuple):
    """The committed glib version and what the newest dependencies would give."""

    current: str | None
    probes: tuple[Probe, ...]

    @property
    def done(self) -> bool:
        """Report whether the committed lockfile already satisfies the advisory."""
        return self.current is None or is_patched(self.current)

    @property
    def blockers(self) -> tuple[Probe, ...]:
        """Return the dependencies that would still hold glib back."""
        return tuple(probe for probe in self.probes if probe.blocked)

    @property
    def actionable(self) -> bool:
        """Report whether an update would now lift the pin."""
        return not self.done and not self.blockers

    @property
    def item_ids(self) -> list[str]:
        """Return the reported items, which are empty once the work is done.

        The three states each carry distinct ids, so every transition between
        them produces exactly one comment: a dependency crossing a major while
        still blocked, the moment the update becomes possible, and finally the
        close when the committed lockfile is updated.
        """
        if self.done:
            return []
        if self.actionable:
            return ["actionable"]
        return [probe.item_id for probe in self.blockers]


def lowest_version(lock: str, crate: str) -> str | None:
    """Return the lowest version of a crate in a lockfile, or None if absent.

    A graph may hold several majors of the same crate side by side. The pin is
    lifted only when no unpatched copy is left, so the lowest one decides.
    """
    packages = tomllib.loads(lock).get("package", [])
    versions = [package["version"] for package in packages if package["name"] == crate]
    return min(versions, key=version_key) if versions else None


def declared_dependencies(manifest: str) -> list[str]:
    """Return the names of the runtime dependencies declared for the desktop crate.

    Target-specific and build dependencies are left out: the former are the
    macOS-only crates, and neither reaches the Linux windowing stack.
    """
    return sorted(tomllib.loads(manifest).get("dependencies", {}))


def probe(name: str, workdir: Path) -> Probe:
    """Resolve one dependency at its newest published version and read back glib."""
    (workdir / "src").mkdir(parents=True, exist_ok=True)
    (workdir / "src" / "main.rs").write_text("fn main() {}\n")
    (workdir / "Cargo.toml").write_text(PROBE_MANIFEST.format(name=name))
    subprocess.run(
        ["cargo", "generate-lockfile"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=True,
    )
    lock = (workdir / "Cargo.lock").read_text()
    version = lowest_version(lock, name)
    assert version is not None, f"{name} is missing from its own lockfile"
    return Probe(name, version, lowest_version(lock, WATCHED_CRATE))


def collect(repo_root: Path) -> Status:
    """Read the committed glib version and probe every declared dependency."""
    current = lowest_version((repo_root / LOCKFILE).read_text(), WATCHED_CRATE)
    names = declared_dependencies((repo_root / MANIFEST).read_text())
    with TemporaryDirectory() as workdir:
        probes = tuple(probe(name, Path(workdir)) for name in names)
    return Status(current, probes)


def _cell(version: str | None) -> str:
    """Render a resolved version as inline code, or as an em dash when absent."""
    return f"`{version}`" if version else "—"


def render_body(status: Status, generated_at: datetime) -> str:
    """Render the tracking issue body for the current state of the pin."""
    patched = ".".join(str(part) for part in PATCHED)
    if status.done:
        intro = f"`{LOCKFILE}` の `glib` は {_cell(status.current)} で、完了条件を満たしています。"
        headline = "**解消済み** — この issue は次回実行時に自動でクローズされます。"
    else:
        intro = (
            f"`{LOCKFILE}` の `glib` は `{status.current}` で、"
            f"[{ADVISORY}]({ADVISORY_URL}) の修正版 {patched} に届いていません。"
        )
        if status.actionable:
            headline = (
                f"**追従可能** — 依存を最新へ上げると `glib` は {patched} 以上に"
                f"解決されます。`{LOCKFILE}` を更新してください。"
            )
        else:
            lowest = min(
                (probe.glib for probe in status.blockers if probe.glib), key=version_key
            )
            headline = (
                f"**待ち** — 依存を最新へ上げても `glib` は `{lowest}` のままです"
                f"（blocker {len(status.blockers)} 件）。"
            )

    lines = [
        intro,
        "",
        "`gtk` 0.18 (Tauri が Linux で使う GTK3 バインディング) は上流でアーカイブ済みで、",
        "`glib` ^0.18 を要求します。`gtk` 側にも `glib` 0.18 側にも修正版は出ないため、",
        "Tauri 自体が GTK3 から離れるまでこの固定は解けません。破壊的リリースとして",
        "到達する見込みで、`cargo update` はメジャー境界を越えないため、更新可能な依存の",
        "digest では検出できません。",
        "",
        headline,
        "",
        "| 依存 | 最新版 | 解決される `glib` |",
        "| --- | --- | --- |",
    ]
    lines += [
        f"| `{probe.name}` | `{probe.version}` | {_cell(probe.glib)} |"
        for probe in status.probes
    ]

    lines += [
        "",
        "### 完了条件",
        "",
        f"- `{LOCKFILE}` の `glib` が {patched} 以上になっている",
        "- Linux 向け desktop ビルドが通る",
        "",
        "<details><summary>検出方法と既知の制限</summary>",
        "",
        f"- `{MANIFEST}` の `[dependencies]` から名前だけを取り出し、各依存を単独で "
        '`"*"` として要求する使い捨て manifest で `cargo generate-lockfile` を実行し、'
        "生成された lock の `glib` を読む",
        "- 監視対象の上流 crate を列挙しないため、Tauri が v3 で GTK3 を捨てても、"
        "tao から winit へ移っても、plugin 側が遅れても判定は追従する",
        "- 依存を1つずつ解決するため、新しい Tauri メジャーに plugin が未追随でも、"
        "解決失敗ではなくその plugin が blocker として現れる",
        '- `"*"` は prerelease を選ばない。beta の間は「待ち」のままになる',
        "- feature 指定を落とすため、feature 経由でしか入らない依存は経路から外れる",
        "",
        "</details>",
        "",
        "---",
        f"最終更新: {generated_at.strftime('%Y-%m-%d %H:%M:%S UTC')} ・ "
        f"生成元: `{WORKFLOW_PATH}`",
        f"`{LOCKFILE}` の `glib` が {patched} 以上になると、"
        "この issue は次回実行時に自動でクローズされます。",
    ]
    return "\n".join(lines) + "\n"


def render_new_state_comment(status: Status) -> str:
    """Render the comment posted when the state changes between runs."""
    patched = ".".join(str(part) for part in PATCHED)
    if status.actionable:
        lines = [
            f"依存を最新へ上げると `glib` が {patched} 以上に解決されるようになりました。",
            "追従作業を開始できます。",
            "",
        ]
        lines += [
            f"- `{probe.name}` — 最新版 `{probe.version}` / `glib` {_cell(probe.glib)}"
            for probe in status.probes
            if probe.glib
        ]
    else:
        lines = [
            "blocker のメジャーバージョンが変わりましたが、"
            f"`glib` は依然として {patched} 未満に固定されています。",
            "",
        ]
        lines += [
            f"- `{probe.name}` — 最新版 `{probe.version}` / `glib` `{probe.glib}`"
            for probe in status.blockers
        ]
    lines += ["", "詳細は issue 本文の一覧を参照してください。"]
    return "\n".join(lines) + "\n"


def render_close_comment(status: Status) -> str:
    """Render the comment posted when the committed lockfile satisfies the advisory."""
    patched = ".".join(str(part) for part in PATCHED)
    return (
        f"`{LOCKFILE}` の `glib` が `{status.current}` になり、"
        f"{patched} 以上という完了条件を満たしたため、この issue をクローズします。\n"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Reconcile the tracking issue with the current state of the glib pin."""
    parser = tracking_issue.common_parser(__doc__ or "")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="Repository root holding the desktop manifest (default: this checkout).",
    )
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when $GITHUB_REPOSITORY is unset.")

    status = collect(args.repo_root)
    return tracking_issue.reconcile(
        args.repo,
        prefix=MARKER_PREFIX,
        title=ISSUE_TITLE,
        body=render_body(status, datetime.now(UTC)),
        item_ids=status.item_ids,
        close_comment=render_close_comment(status),
        on_new_items=lambda _: render_new_state_comment(status),
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    sys.exit(main())
