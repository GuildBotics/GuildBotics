"""The local code host reads Git paths exactly and patches as display text."""

import subprocess
from pathlib import Path

import pytest
from git import Repo

from guildbotics.integrations.local import store
from guildbotics.runtime.port import ServiceError
from tests.guildbotics.local_code_host import REPOSITORY, local_member, pull_request
from tests.subprocess_output import process_output


@pytest.mark.asyncio
@pytest.mark.parametrize("encoding", ["utf-8", "cp932"])
async def test_files_preserve_japanese_paths_and_decode_patch_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, encoding: str
) -> None:
    with Repo.init(tmp_path / "seed", initial_branch="main") as seed:
        root = Path(seed.working_dir)
        (root / "README.md").write_text("seed\n", encoding="utf-8")
        seed.index.add(["README.md"])
        seed.index.commit("seed")
        seed.create_head("改善").checkout()
        body = "日本語のメモリ検索\n".encode(encoding)
        (root / "設計.md").write_bytes(body)
        seed.index.add(["設計.md"])
        seed.index.commit("feature")
        remote = store.bare(REPOSITORY)
        remote.parent.mkdir(parents=True)
        with Repo.clone_from(seed.working_dir, remote, bare=True):
            pass
    url = pull_request(1, "改善")
    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp932")
    member = local_member()
    try:
        [changed] = (await member.code.read("pull_request_files", url)).items
    finally:
        await member.aclose()

    assert changed["path"] == "設計.md"
    assert changed["status"] == "added"
    assert changed["additions"] == 1
    assert "+" + body.decode("utf-8", "replace").strip() in changed["patch"]


@pytest.mark.parametrize(
    ("stdout", "stderr", "returncode", "error", "match"),
    [
        (b"\xff", b"", 0, UnicodeDecodeError, "utf-8"),
        (b"", b"fatal: invalid \x81", 128, ServiceError, "fatal: invalid "),
    ],
)
def test_git_requires_exact_paths_and_reports_undecodable_diagnostics(
    monkeypatch: pytest.MonkeyPatch, stdout, stderr, returncode, error, match
) -> None:
    remote = store.bare(REPOSITORY)
    remote.mkdir(parents=True)
    run = subprocess.run

    def failed(_command, **kwargs):
        return run(process_output(stdout, stderr, returncode), **kwargs)

    monkeypatch.setattr(subprocess, "_text_encoding", lambda: "cp932")
    monkeypatch.setattr(subprocess, "run", failed)

    with pytest.raises(error, match=match):
        store.git(REPOSITORY, "diff")
