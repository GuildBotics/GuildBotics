"""Exercise the real CLI Git client against backend data exchanges."""

import asyncio
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.cli import member_client
from guildbotics.local_git import GitError, LocalGit, known_commits
from guildbotics.capabilities.errors import MemberCapabilityError


async def current(member, path, *, message=None, cwd=None):
    def exchange(_client, _body, phase, data, output, bundle=None):
        # HTTP transfers bytes, never a path to the client's file.
        with tempfile.TemporaryDirectory() as directory:
            incoming, outgoing = (
                Path(directory) / "in.bundle",
                Path(directory) / "out.bundle",
            )
            if bundle is not None:
                shutil.copyfile(bundle, incoming)
            result = asyncio.run(
                member.service.client_git(
                    phase, data, incoming if bundle else None, outgoing
                )
            )
            output.write_bytes(outgoing.read_bytes() if outgoing.exists() else b"")
            return result

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(member_client, "_git_exchange", exchange)
        try:
            result = await asyncio.to_thread(
                member_client._current_git,
                None,
                {},
                {
                    "repo_path": str(path),
                    "operation": "push" if message is None else "publish",
                },
                message or "",
                cwd=cwd or path,
            )
        except GitError as exc:
            raise MemberCapabilityError(str(exc)) from exc
    return SimpleNamespace(**result)


def tracking(member, origin, path):
    """Return a tracking bundle, retrying a stale advertisement."""
    git = LocalGit(path)
    with tempfile.TemporaryDirectory() as directory:
        outgoing = Path(directory) / "out.bundle"
        for known in (sorted(member_client.known_commits(git)), []):
            refs = asyncio.run(
                member.service.client_git(
                    "tracking",
                    {
                        "remotes": [member.url("owner")],
                        "known": known,
                    },
                    None,
                    outgoing,
                )
            )["refs"]
            bundle = outgoing if outgoing.exists() else None
            if bundle is None or git.verify_bundle(bundle):
                git.update_tracking(bundle, refs)
                return
            outgoing.unlink()
    raise AssertionError("Tracking transfer failed")
