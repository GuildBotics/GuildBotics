"""The files the local code host and board keep, under the workspace's
device-local ``.guildbotics/local/services/code``.

Each repository is a bare git repository ``<owner>/<repo>.git`` (the remote
members fetch from and push to) beside a directory ``<owner>/<repo>/`` that
holds its issues and pull requests, one JSON file per number
(``items/<n>.json``), and optionally ``repository.json`` (the labels the
repository defines) and ``artifacts/<n>/<name>.zip`` (a pull request's CI
artifacts). Issues and pull requests share one numbering, and comments, reviews
and review comments one id sequence per repository, as on a hosted code host.

An item is addressed by ``local://<owner>/<repo>/issues/<n>`` or
``local://<owner>/<repo>/pull/<n>``.

Every write of the code host and the board is a :func:`save`, which refuses a
repository outside the project's configured owner.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from guildbotics.entities import Task
from guildbotics.entities.team import Project
from guildbotics.integrations.repository_scope import (
    NAME,
    check_repository,
    configured_owner,
)
from guildbotics.runtime.code_hosting_service import ItemKind, ItemRef
from guildbotics.runtime.integration_factory import MemberCapabilityError
from guildbotics.utils.fileio import get_workspace_local_path

SCHEME = "local"
_COLLECTIONS: dict[ItemKind, str] = {"issue": "issues", "pull_request": "pull"}
_URL_PARTS = 4
#: The keys of an item that hold things with an id of the repository's sequence.
_ENTRIES = ("comments", "reviews", "review_comments")


def root() -> Path:
    return get_workspace_local_path("services", "code")


def now() -> str:
    return datetime.now(UTC).isoformat()


def url(owner: str, repo: str, kind: ItemKind, number: int) -> str:
    return f"{SCHEME}://{owner}/{repo}/{_COLLECTIONS[kind]}/{number}"


def locate(value: str, kind: ItemKind | None = None) -> ItemRef:
    """Where the local item URL ``value`` points.

    Raises:
        MemberCapabilityError: If it is not one, or not of ``kind``.
    """
    parsed = urlparse(value)
    parts = [parsed.netloc, *parsed.path.strip("/").split("/")]
    found = next(
        (
            k
            for k, name in _COLLECTIONS.items()
            if len(parts) == _URL_PARTS and parts[2] == name
        ),
        None,
    )
    if (
        parsed.scheme != SCHEME
        or found is None
        or not parts[3].isdigit()
        or not all(_valid(name) for name in parts[:2])
    ):
        raise MemberCapabilityError(f"Unsupported local URL: {value}")
    if kind is not None and found != kind:
        raise MemberCapabilityError(f"Expected {kind} URL, got {found} URL.")
    return ItemRef(
        kind=found,
        owner=parts[0],
        repo=parts[1],
        number=int(parts[3]),
        url=url(parts[0], parts[1], found, int(parts[3])),
    )


def split(repo: str) -> tuple[str, str]:
    """``owner/repo`` as its two names.

    Raises:
        MemberCapabilityError: If it is not one.
    """
    owner, _, name = repo.partition("/")
    if not (_valid(owner) and _valid(name)):
        raise MemberCapabilityError(f"Repository must be '<owner>/<repo>': {repo}")
    return owner, name


def bare(owner: str, repo: str) -> Path:
    return root() / owner / f"{repo}.git"


def repositories() -> Iterator[tuple[str, str]]:
    """Every repository that has items."""
    for items in sorted(root().glob("*/*/items")):
        yield items.parent.parent.name, items.parent.name


def item_path(owner: str, repo: str, number: int) -> Path:
    return root() / owner / repo / "items" / f"{number}.json"


def load(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Raises:
    MemberCapabilityError: If the repository has no such item.
    """
    path = item_path(owner, repo, number)
    if not path.is_file():
        raise MemberCapabilityError(f"{owner}/{repo}#{number} was not found.")
    return json.loads(path.read_text(encoding="utf-8"))


def items(owner: str, repo: str) -> list[dict[str, Any]]:
    directory = root() / owner / repo / "items"
    return sorted(
        (
            json.loads(path.read_text(encoding="utf-8"))
            for path in directory.glob("*.json")
        ),
        key=lambda item: item["number"],
    )


def save(project: Project, owner: str, repo: str, item: dict[str, Any]) -> None:
    """Write ``item`` of ``owner/repo`` for a member of ``project``.

    Raises:
        RepositoryScopeError: If the repository is not the configured owner's.
    """
    check_repository(configured_owner(project), owner, repo)
    path = item_path(owner, repo, item["number"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(item, ensure_ascii=False, indent=2), encoding="utf-8")


def next_number(owner: str, repo: str) -> int:
    return max((item["number"] for item in items(owner, repo)), default=0) + 1


def next_id(owner: str, repo: str) -> int:
    return (
        max(
            (
                entry["id"]
                for item in items(owner, repo)
                for key in _ENTRIES
                for entry in item.get(key, [])
            ),
            default=0,
        )
        + 1
    )


def find_entry(owner: str, repo: str, key: str, entry_id: int) -> tuple[dict, dict]:
    """The item and its ``key`` entry with ``entry_id``.

    Raises:
        MemberCapabilityError: If the repository has none.
    """
    for item in items(owner, repo):
        for entry in item.get(key, []):
            if entry["id"] == entry_id:
                return item, entry
    raise MemberCapabilityError(f"{owner}/{repo} has no {key} entry {entry_id}.")


def repository(owner: str, repo: str) -> dict[str, Any]:
    path = root() / owner / repo / "repository.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def artifact(owner: str, repo: str, number: int, name: str) -> Path:
    return root() / owner / repo / "artifacts" / str(number) / f"{name}.zip"


def task(
    owner: str, repo: str, item: dict[str, Any], status: str, **fields: Any
) -> Task:
    """The work ``item`` is, in ``status``, assigned to the member it asks."""
    url_ = url(owner, repo, item["kind"], item["number"])
    return Task(
        id=url_,
        number=item["number"],
        url=url_,
        title=item["title"],
        description=item["body"],
        status=status,
        created_at=datetime.fromisoformat(item["created_at"]),
        repository=repo,
        **fields,
    )


def git(owner: str, repo: str, *args: str) -> str:
    """What git prints in the repository's bare repository.

    Raises:
        MemberCapabilityError: If there is none, or git fails.
    """
    if not bare(owner, repo).is_dir():
        raise MemberCapabilityError(f"{owner}/{repo} has no remote repository.")
    ran = subprocess.run(
        ["git", *args],
        cwd=bare(owner, repo),
        capture_output=True,
        check=False,
        text=True,
    )
    if ran.returncode != 0:
        raise MemberCapabilityError(
            f"git {args[0]} failed in {owner}/{repo}: {ran.stderr.strip()}"
        )
    return ran.stdout.strip()


def branch_tip(owner: str, repo: str, branch: str) -> str:
    """The commit ``branch`` of the remote is at; empty if it has no such
    branch, as a remote that is not there (a deleted fork) has none.

    Raises:
        MemberCapabilityError: If the remote cannot be read.
    """
    if not bare(owner, repo).is_dir():
        return ""
    ref = f"refs/heads/{branch}"
    # The pattern also matches the refs below it (``refs/heads/<branch>/x``).
    listed = git(owner, repo, "for-each-ref", "--format=%(objectname) %(refname)", ref)
    return next(
        (
            sha
            for sha, _, name in (line.partition(" ") for line in listed.splitlines())
            if name == ref
        ),
        "",
    )


def _valid(name: str) -> bool:
    return bool(NAME.fullmatch(name)) and name not in {".", ".."}
