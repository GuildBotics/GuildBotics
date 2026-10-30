"""Local Git operations for a client exchanging history with the host.

Only Git and the standard library are needed here. The client supplies history
and reads its push URLs; repository selection, credentials and publication
belong to the host. Bundle paths refer to transferred files, never to the other
side's repository.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Protocol


class GitError(RuntimeError):
    """A local Git operation failed."""


class GitResult(Protocol):
    @property
    def returncode(self) -> int: ...

    @property
    def stdout(self) -> bytes: ...

    @property
    def stderr(self) -> bytes: ...


class LocalGit:
    """Run Git under the local repository's configuration and hooks."""

    def __init__(
        self,
        cwd: Path,
        timeout: Callable[[], float] | None = None,
        config: tuple[str, ...] = (),
    ) -> None:
        self.cwd = cwd
        self._timeout = timeout
        self._config = [option for item in config for option in ("-c", item)]

    def __call__(
        self,
        *args: str,
        env: Mapping[str, str] | None = None,
        stdin: bytes | Path = b"",
        stdout: Path | None = None,
        ok: Iterable[int] = (0,),
    ) -> GitResult:
        assert stdout is None, "local git writes its own files with git itself"
        try:
            ran = subprocess.run(
                ["git", *self._config, *args],
                cwd=self.cwd,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})},
                input=stdin.read_bytes() if isinstance(stdin, Path) else stdin,
                capture_output=True,
                timeout=self._timeout() if self._timeout else None,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {args[0]} ran out of time.") from exc
        if ran.returncode not in ok:
            reason = (ran.stderr or ran.stdout).decode(errors="replace").strip()
            raise GitError(f"git {args[0]} failed: {reason}")
        return ran

    def push_urls(self) -> list[str]:
        """Read origin's push destinations as Git resolves them."""
        return (
            self("remote", "get-url", "--push", "--all", "origin")
            .stdout.decode(errors="replace")
            .splitlines()
        )

    def branch_bundle(self, branch: str, known: Iterable[str], bundle: Path) -> bool:
        """Write branch history excluding commits the host already holds.

        Args:
            branch: A branch name already validated by the host.
            known: Object names advertised by the host; empty requests a full
                bundle, including any previously omitted prerequisites.
            bundle: The file to transfer to the host.

        Returns:
            Whether history needs to be transferred.
        """
        ref = f"refs/heads/{branch}"
        head = self("rev-parse", "-q", "--verify", f"{ref}^{{commit}}", ok=(0, 1))
        if head.returncode:
            raise GitError(f"Branch '{branch}' has no commit to push.")
        names = set(known)
        present = self(
            "cat-file",
            "--batch-check=%(objectname)",
            stdin="".join(f"{name}\n" for name in sorted(names)).encode(),
        )
        held = names & set(present.stdout.decode().splitlines())
        if head.stdout.decode().strip() in held:
            return False
        self.write_bundle(bundle, (ref, *(f"^{name}" for name in sorted(held))))
        return True

    def write_bundle(self, bundle: Path, revisions: Iterable[str]) -> None:
        """Write named revisions and optional exclusions as a transferable file."""
        self(
            "bundle",
            "create",
            "-q",
            str(bundle),
            "--stdin",
            stdin="".join(f"{line}\n" for line in revisions).encode(),
        )

    def update_tracking(self, bundle: Path, refs: Mapping[str, str]) -> None:
        """Import returned history and update only origin's tracking refs.

        As with a fetch without prune, unrelated and deleted remote refs in
        the user's repository remain untouched.
        """
        if any(not ref.startswith("refs/remotes/origin/") for ref in refs):
            raise GitError("Only origin tracking references may be updated.")
        self("bundle", "unbundle", str(bundle))
        self(
            "update-ref",
            "--no-deref",
            "--stdin",
            stdin="".join(
                f"update {ref} {name}\n" for ref, name in refs.items()
            ).encode(),
        )
