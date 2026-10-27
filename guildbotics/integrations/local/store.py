"""The files the local code host and tickets keep, under the workspace's
device-local ``.guildbotics/local/services``.

A repository is named by its path of any depth (``acme/team/demo``) and kept
in ``code/<path, quoted>/``: its bare git repository ``repository.git`` (the
remote members fetch from and push to), its pull requests ``pulls/<n>.json``,
the CI artifacts of a head commit ``artifacts/<sha>/<name>.zip``, and the
conclusion its CI finished with, when it was recorded, ``checks/<sha>``.

A ticket is kept in a tracker, a path too, in ``tickets/<path, quoted>/``:
the tracker's ``tracker.json`` (the labels it defines, and the repository its
tickets belong to, if any), the tickets ``<key>.json``, keyed ``T-<n>``, and
the files attached to them, ``attachments/<id>`` with ``attachments/<id>.json``
(its name and media type). A ticket's body or comment attaches one by writing
its URL, ``local://tickets/<tracker>/attachments/<id>``.
Pull requests and tickets are numbered each in their own place (``number``).

An item is addressed by ``local://code/<repository>/pulls/<n>`` or
``local://tickets/<tracker>/<key>``. The comments, reviews, and inline
comments of an item have ids of its own sequence (``comment-3``), so a URL and
an id name one thing only together.

Every write is a :func:`save`, which refuses a path outside the scope the
writing service is configured with.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, unquote, urlparse

from guildbotics.integrations.repository_scope import check_path, valid_path
from guildbotics.runtime.port import ServiceError, ServiceNotFound
from guildbotics.runtime.resources import WorkKind
from guildbotics.utils.fileio import get_workspace_local_path

SCHEME = "local"
Kind = Literal["code", "tickets"]
_KINDS: dict[WorkKind, Kind] = {"pull_request": "code", "ticket": "tickets"}
#: The keys of an item that hold things with an id of the item's sequence.
ENTRIES = ("comments", "reviews", "review_comments")
_KEY = re.compile(r"T-[1-9][0-9]*")
_NUMBER = re.compile(r"[1-9][0-9]*")


@dataclass(frozen=True)
class Ref:
    """Where a local item is: its repository or tracker, and its name there."""

    kind: WorkKind
    #: The repository of a pull request, the tracker of a ticket.
    place: str
    #: A pull request's number, a ticket's key.
    name: str

    @property
    def url(self) -> str:
        if self.kind == "pull_request":
            return f"{SCHEME}://code/{self.place}/pulls/{self.name}"
        return f"{SCHEME}://tickets/{self.place}/{self.name}"

    @property
    def label(self) -> str:
        return f"!{self.name}" if self.kind == "pull_request" else self.name

    @property
    def path(self) -> Path:
        if self.kind == "pull_request":
            return directory("code", self.place) / "pulls" / f"{self.name}.json"
        return directory("tickets", self.place) / f"{self.name}.json"


def now() -> str:
    return datetime.now(UTC).isoformat()


def directory(kind: Kind, place: str) -> Path:
    return get_workspace_local_path("services", kind) / quote(place, safe="")


def places(kind: Kind) -> Iterator[str]:
    """Every repository or tracker there is."""
    root = get_workspace_local_path("services", kind)
    if root.is_dir():
        yield from sorted(unquote(found.name) for found in root.iterdir())


def locate(url: str, kind: WorkKind) -> Ref:
    """The item of ``kind`` the local URL ``url`` names.

    Raises:
        ServiceNotFound: If it names none.
    """
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if kind == "pull_request":
        place, name = "/".join(parts[:-2]), parts[-1]
        fits = parts[-2:-1] == ["pulls"] and bool(_NUMBER.fullmatch(name))
    else:
        place, name = "/".join(parts[:-1]), parts[-1]
        fits = bool(_KEY.fullmatch(name))
    if (
        parsed.scheme != SCHEME
        or parsed.netloc != _KINDS[kind]
        or parsed.query
        or parsed.fragment
        or not fits
        or not valid_path(place)
    ):
        raise ServiceNotFound(f"{url} is not a local {kind} URL.")
    return Ref(kind, place, name)


def load(ref: Ref) -> dict[str, Any]:
    """Raises:
    ServiceNotFound: If there is no such item.
    """
    try:
        return json.loads(ref.path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ServiceNotFound(f"{ref.url} was not found.") from None
    except ValueError:
        raise ServiceError(f"{ref.url} cannot be read.") from None


def items(kind: WorkKind, place: str | None = None) -> list[tuple[Ref, dict[str, Any]]]:
    """Every item of ``kind`` of the repository or tracker ``place`` -- of every
    one without it -- each with where it is, in each place's order."""
    found: list[tuple[Ref, dict[str, Any]]] = []
    for each in [place] if place else places(_KINDS[kind]):
        folder = directory(_KINDS[kind], each)
        if kind == "pull_request":
            folder /= "pulls"
        paths = folder.glob("*.json") if folder.is_dir() else ()
        loaded = sorted(
            (
                json.loads(path.read_text(encoding="utf-8"))
                for path in paths
                if path.name != "tracker.json"
            ),
            key=lambda item: item["number"],
        )
        found.extend((ref_of(kind, each, item["number"]), item) for item in loaded)
    return found


def save(scope: str, ref: Ref, item: dict[str, Any]) -> None:
    """Write ``item`` for a member writing in ``scope``.

    Raises:
        ServiceError: If its repository or tracker is outside ``scope``.
    """
    check_path(scope, ref.place, provider="local")
    ref.path.parent.mkdir(parents=True, exist_ok=True)
    ref.path.write_text(json.dumps(item, ensure_ascii=False, indent=2), "utf-8")


def new_ref(kind: WorkKind, place: str) -> tuple[Ref, int]:
    """Where the next item of the repository or tracker goes, and its number."""
    number = max((item["number"] for _, item in items(kind, place)), default=0) + 1
    return ref_of(kind, place, number), number


def ref_of(kind: WorkKind, place: str, number: int) -> Ref:
    """The item numbered ``number`` in the repository or tracker: a pull
    request is named by its number, a ticket by its key ``T-<number>``."""
    return Ref(kind, place, str(number) if kind == "pull_request" else f"T-{number}")


def next_id(item: dict[str, Any], kind: str) -> str:
    """The next id of the item's sequence, for an entry of ``kind``."""
    taken = [
        int(entry["id"].rpartition("-")[2])
        for key in ENTRIES
        for entry in item.get(key, [])
    ]
    return f"{kind}-{max(taken, default=0) + 1}"


def tracker(place: str) -> dict[str, Any]:
    """The tracker's settings: the labels it defines, its repository.

    Raises:
        ServiceNotFound: If there is no such tracker.
    """
    path = directory("tickets", place) / "tracker.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ServiceNotFound(f"There is no tracker '{place}'.") from None


def attachments(place: str) -> Path:
    """Where the tracker keeps the files attached to its tickets."""
    return directory("tickets", place) / "attachments"


def attachment_url(place: str, attachment_id: str) -> str:
    """What a ticket of the tracker writes to refer to an attachment."""
    return f"{SCHEME}://tickets/{place}/attachments/{attachment_id}"


def bare(repository: str) -> Path:
    return directory("code", repository) / "repository.git"


def artifact(repository: str, sha: str, name: str) -> Path:
    """Where the artifact ``name`` of the commit ``sha`` is kept.

    Raises:
        ServiceNotFound: If ``name`` names a file outside the commit's artifacts.
    """
    artifacts = directory("code", repository) / "artifacts" / sha
    path = artifacts / f"{name}.zip"
    if path.parent != artifacts:
        raise ServiceNotFound(f"'{name}' is no artifact name.")
    return path


def artifacts(repository: str, sha: str) -> list[str]:
    """The names of the artifacts of the commit ``sha``."""
    return sorted(
        path.stem
        for path in (directory("code", repository) / "artifacts" / sha).glob("*.zip")
    )


def checks(repository: str, sha: str) -> str:
    """The conclusion recorded for the CI of the commit ``sha``; empty for none."""
    path = directory("code", repository) / "checks" / sha
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def finish_checks(repository: str, sha: str, conclusion: str) -> None:
    """Record the conclusion the CI of the commit ``sha`` finished with, as
    whoever runs it does."""
    path = directory("code", repository) / "checks" / sha
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(conclusion, encoding="utf-8")


def git(repository: str, *args: str, errors: str = "strict") -> str:
    """What git prints in the repository's bare repository.

    Paths and refs are UTF-8; only display patches may replace non-UTF-8
    file content. Keep bytes until decoding so paths retain their newlines.

    Raises:
        ServiceNotFound: If there is none.
        ServiceError: If git fails.
    """
    if not bare(repository).is_dir():
        raise ServiceNotFound(f"{repository} has no remote repository.")
    ran = subprocess.run(
        ["git", *args],
        cwd=bare(repository),
        # The caller's GIT_* variables name its own repository, index, and
        # configuration, none of which is the service's.
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        capture_output=True,
        check=False,
    )
    if ran.returncode != 0:
        raise ServiceError(
            f"git {args[0]} failed in {repository}: "
            f"{ran.stderr.decode('utf-8', 'replace').strip()}"
        )
    return ran.stdout.decode("utf-8", errors).strip()


def branch_tip(repository: str, branch: str) -> str:
    """The commit ``branch`` of the remote is at; empty if it has no such
    branch, as a remote that is not there (a deleted fork) has none.

    Raises:
        ServiceError: If the remote cannot be read.
    """
    if not bare(repository).is_dir():
        return ""
    ref = f"refs/heads/{branch}"
    # The pattern also matches the refs below it (``refs/heads/<branch>/x``).
    listed = git(repository, "for-each-ref", "--format=%(objectname) %(refname)", ref)
    return next(
        (
            sha
            for sha, _, name in (line.partition(" ") for line in listed.splitlines())
            if name == ref
        ),
        "",
    )
