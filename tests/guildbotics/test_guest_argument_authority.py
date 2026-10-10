"""Who decides each value a command's isolated environment passes the host.

Everything a member command and a call of the command's window carry was
written inside the microVM, where the agent's code runs beside the
command's. So every argument is classified by what keeps it from reaching
beyond the run it belongs to:

- ``GRANT``: the host's grant settles it. The call names no such value (the
  host takes it from the grant), or the host refuses any other; the basis is
  the function that refuses.
- ``MEMBER``: the member's own authority bounds it (its memory, its clone,
  what the host does not read for it); the basis is the function that checks.
- ``SERVICE``: the external service's permission for the member bounds it (the
  GitHub token and the configured owner, the Slack bot's channels); the basis
  is the function that checks, or why the service does.
- ``CONTENT``: what the value says, its format, or a step of the procedure
  (``--human-approved`` among them); it chooses nothing to act on.

A new argument or call fails here until it is classified.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Iterator

import click
import pytest

from guildbotics.cli.member import member
from guildbotics.environment.host_window import _CALLS, HostWindow

GRANT, MEMBER, SERVICE, CONTENT = "grant", "member", "service", "content"

_BROKER = "guildbotics.environment.member_broker._rejection_reason"
_HOST_PATH = "guildbotics.cli.member._HostPath"
_MEMORY = "guildbotics.capabilities.member_memory.MemberMemoryService._scope_dir"
_GITHUB = "guildbotics.integrations.github.repository_scope.check_request"
_GITHUB_READ = (
    "Reads go through the member's GitHub token, which bounds what they reach."
)
_SLACK = "The member's Slack bot reaches only the channels it was invited to."
#: Why a service bounds what no function of GuildBotics checks.
_SERVICE_RATIONALES = frozenset({_GITHUB_READ, _SLACK})
_CONVERSATION = "guildbotics.environment.host_window.HostWindow._check_conversation"

_CHAT_TARGETS = frozenset(
    {
        "chat inspect channel",
        "chat inspect thread",
        "chat post",
        "chat reply",
        "chat reaction add",
    }
)
_CONTENT_COMMANDS = frozenset(
    {
        "memory record",
        "memory update",
        "chat post",
        "chat reply",
        "chat noop",
        "chat complete",
        "git commit",
        "git publish",
        "github issue comment",
        "github issue create",
        "github issue update",
        "github pr create",
        "github pr update",
        "github pr comment",
        "github pr review",
        "github pr review-comment",
        "github pr reply",
        "task complete",
    }
)
_MEMORY_DOCUMENTS = frozenset(
    {"memory get", "memory update", "memory touch", "memory archive"}
)
_MEMORY_LINKS = frozenset({"memory record", "memory update"})
_GIT_WRITES = frozenset({"git commit", "git push", "git publish"})

#: Every argument of a member command: its kind, the commands that take it
#: (``None`` for every command but ``help``; ``""`` for the group), and its
#: basis (none for ``CONTENT``).
MEMBER_ARGUMENTS: dict[str, tuple[str, frozenset[str] | None, str]] = {
    "workspace_dir": (GRANT, frozenset({""}), _BROKER),
    "person": (GRANT, None, _BROKER),
    "output_format": (CONTENT, None, ""),
    "content_stdin": (CONTENT, _CONTENT_COMMANDS, ""),
    "content_file": (MEMBER, _CONTENT_COMMANDS, _HOST_PATH),
    "check_credentials": (CONTENT, frozenset({"context"}), ""),
    # Memory: the member's own documents and the team's.
    "doc_id": (MEMBER, _MEMORY_DOCUMENTS | {"memory promote"}, _MEMORY),
    "team_scope": (MEMBER, _MEMORY_DOCUMENTS, _MEMORY),
    "scope": (MEMBER, frozenset({"memory record"}), _MEMORY),
    "title": (
        CONTENT,
        _MEMORY_LINKS
        | {
            "github issue create",
            "github issue update",
            "github pr create",
            "github pr update",
        },
        "",
    ),
    "summary": (CONTENT, _MEMORY_LINKS, ""),
    "keywords": (CONTENT, _MEMORY_LINKS, ""),
    "add_keywords": (CONTENT, frozenset({"memory update"}), ""),
    "remove_keywords": (CONTENT, frozenset({"memory update"}), ""),
    "kind": (CONTENT, _MEMORY_LINKS, ""),
    "pinned": (CONTENT, frozenset({"memory record"}), ""),
    "pin_value": (CONTENT, frozenset({"memory update"}), ""),
    "policy_approved": (
        CONTENT,
        frozenset({"memory record", "memory update", "memory archive"}),
        "",
    ),
    "set_values": (CONTENT, _MEMORY_LINKS, ""),
    "tickets": (CONTENT, _MEMORY_LINKS, ""),
    "prs": (CONTENT, _MEMORY_LINKS, ""),
    "channels": (CONTENT, _MEMORY_LINKS, ""),
    "threads": (CONTENT, _MEMORY_LINKS, ""),
    "queries": (CONTENT, frozenset({"memory recall"}), ""),
    "meta_only": (CONTENT, frozenset({"memory recall"}), ""),
    "limit": (
        CONTENT,
        frozenset({"memory recall", "chat inspect channel", "chat inspect thread"}),
        "",
    ),
    # Chat: where the member reads and posts. The chat run's own event is
    # not among them: it is the grant's work.
    "service_name": (
        SERVICE,
        _CHAT_TARGETS | {"chat identity", "chat resolve-channel"},
        _SLACK,
    ),
    "channel_id": (SERVICE, _CHAT_TARGETS, _SLACK),
    "channel_name": (SERVICE, _CHAT_TARGETS | {"chat resolve-channel"}, _SLACK),
    "thread_ts": (SERVICE, frozenset({"chat inspect thread", "chat reply"}), _SLACK),
    "message_ts": (SERVICE, frozenset({"chat reaction add"}), _SLACK),
    "message_url": (
        SERVICE,
        frozenset({"chat inspect thread", "chat reply", "chat reaction add"}),
        _SLACK,
    ),
    "oldest_ts": (SERVICE, frozenset({"chat inspect channel"}), _SLACK),
    "latest_ts": (SERVICE, frozenset({"chat inspect channel"}), _SLACK),
    "reaction": (CONTENT, frozenset({"chat reaction add"}), ""),
    "status": (CONTENT, frozenset({"chat complete", "task complete"}), ""),
    # Git: the member's clone, worked on in the command's environment.
    "repo_path": (MEMBER, _GIT_WRITES, _HOST_PATH),
    "workspace_mode": (
        MEMBER,
        _GIT_WRITES,
        "guildbotics.cli.member._reject_current_workspace_mode_in_task_run",
    ),
    "branch": (
        SERVICE,
        frozenset({"git prepare"}),
        "guildbotics.integrations.repository_scope.check_repository",
    ),
    # GitHub: what the member writes to, within the configured owner.
    "repo": (
        SERVICE,
        frozenset(
            {
                "git prepare",
                "github issue create",
                "github pr create",
                "github reaction add",
                "repository read",
            }
        ),
        _GITHUB,
    ),
    "issue_url": (
        SERVICE,
        frozenset(
            {
                "git prepare",
                "github issue comment",
                "github issue update",
                "github pr create",
            }
        ),
        _GITHUB,
    ),
    "pr_url": (
        SERVICE,
        frozenset(
            {
                "git prepare",
                "github pr update",
                "github pr comment",
                "github pr review",
                "github pr review-comment",
                "github pr reply",
            }
        ),
        _GITHUB,
    ),
    "base": (SERVICE, frozenset({"github pr create"}), _GITHUB),
    "head": (SERVICE, frozenset({"github pr create"}), _GITHUB),
    "reply_target_id": (SERVICE, frozenset({"github pr reply"}), _GITHUB),
    "comment_id": (SERVICE, frozenset({"github reaction add"}), _GITHUB),
    "pr_number": (SERVICE, frozenset({"github reaction add"}), _GITHUB),
    "target": (SERVICE, frozenset({"github reaction add"}), _GITHUB),
    "reaction_content": (CONTENT, frozenset({"github reaction add"}), ""),
    "target_url": (SERVICE, frozenset({"github run artifact download"}), _GITHUB_READ),
    "name": (SERVICE, frozenset({"github run artifact download"}), _GITHUB_READ),
    "dest": (MEMBER, frozenset({"github run artifact download"}), _HOST_PATH),
    "resource": (CONTENT, frozenset({"repository read"}), ""),
    "identifier": (SERVICE, frozenset({"repository read"}), _GITHUB_READ),
    "parameters": (CONTENT, frozenset({"repository read"}), ""),
    "continuation": (SERVICE, frozenset({"repository read"}), _GITHUB_READ),
    "human_approved": (
        CONTENT,
        frozenset({"github issue create", "github issue update"}),
        "",
    ),
    "labels": (CONTENT, frozenset({"github issue create"}), ""),
    "add_labels": (CONTENT, frozenset({"github issue update"}), ""),
    "remove_labels": (CONTENT, frozenset({"github issue update"}), ""),
    "add_to_project": (CONTENT, frozenset({"github issue create"}), ""),
    "state": (CONTENT, frozenset({"github issue update"}), ""),
    "state_reason": (CONTENT, frozenset({"github issue update"}), ""),
    "closes_issue": (CONTENT, frozenset({"github pr create"}), ""),
    "draft": (CONTENT, frozenset({"github pr create"}), ""),
    "drop_issue_links": (CONTENT, frozenset({"github pr update"}), ""),
    "event": (CONTENT, frozenset({"github pr review"}), ""),
    "file_path": (CONTENT, frozenset({"github pr review-comment"}), ""),
    "line": (CONTENT, frozenset({"github pr review-comment"}), ""),
    "side": (CONTENT, frozenset({"github pr review-comment"}), ""),
    "start_line": (CONTENT, frozenset({"github pr review-comment"}), ""),
    "start_side": (CONTENT, frozenset({"github pr review-comment"}), ""),
}

#: Every argument of a call of the command's window: its kind and basis.
WINDOW_ARGUMENTS: dict[tuple[str, str], tuple[str, str]] = {
    ("begin_turn", "tool"): (
        GRANT,
        "guildbotics.environment.command_environment.start_turn_environment",
    ),
    ("begin_turn", "cwd"): (
        GRANT,
        "guildbotics.environment.command_environment._SharedEnvironment._admit",
    ),
    ("begin_turn", "participant_labels"): (CONTENT, ""),
    ("end_turn", "turn_grant"): (
        GRANT,
        "guildbotics.environment.host_window.HostWindow.end_turn",
    ),
    ("resolve", "key"): (GRANT, _CONVERSATION),
    ("resolve", "policy"): (CONTENT, ""),
    ("resolve", "model"): (CONTENT, ""),
    ("save", "record"): (GRANT, _CONVERSATION),
    ("mark_unhealthy", "record"): (GRANT, _CONVERSATION),
    ("mark_unhealthy", "reason"): (CONTENT, ""),
    ("record", "entries"): (GRANT, _CONVERSATION),
    ("record_completed", "attempt"): (CONTENT, ""),
    ("record_completion_missing", "attempt"): (CONTENT, ""),
    ("record_completion_missing", "max_attempts"): (CONTENT, ""),
    ("record_completion_missing", "error"): (CONTENT, ""),
    ("member", "arguments"): (GRANT, _BROKER),
    ("member", "stdin"): (CONTENT, ""),
    ("agno", "person_id"): (
        GRANT,
        "guildbotics.environment.host_window.HostWindow.agno",
    ),
    ("agno", "call"): (CONTENT, ""),
    ("jev", "call"): (CONTENT, ""),
}

#: What the grant settles, which no argument names: the run, its work, and the
#: subject a run's completion and checks act on.
GRANT_SETTLED = frozenset(
    {
        "run_id",
        "task_run_id",
        "work",
        "work_kind",
        "work_identity",
        "ticket_url",
        "event_id",
        "adapter",
    }
)


def _leaves(
    group: click.Group, path: tuple[str, ...] = ()
) -> Iterator[tuple[str, click.Command]]:
    for name, command in group.commands.items():
        if isinstance(command, click.Group):
            yield from _leaves(command, (*path, name))
        else:
            yield " ".join((*path, name)), command


def _member_arguments() -> dict[str, frozenset[str]]:
    """Each argument the member CLI takes, and the commands that take it
    (``""`` for the group's own)."""
    taken: dict[str, set[str]] = {param.name or "": {""} for param in member.params}
    for path, command in _leaves(member):
        for param in command.params:
            taken.setdefault(param.name or "", set()).add(path)
    return {name: frozenset(paths) for name, paths in taken.items()}


def _window_arguments() -> set[tuple[str, str]]:
    return {
        (call, name)
        for call in _CALLS
        for name in inspect.signature(getattr(HostWindow, call)).parameters
        if name != "self"
    }


def _resolves(basis: str) -> bool:
    """Whether ``basis`` names a module's attribute, as ``module.attribute``."""
    parts = basis.split(".")
    for split in range(len(parts) - 1, 0, -1):
        try:
            target = importlib.import_module(".".join(parts[:split]))
        except ImportError:
            continue
        for part in parts[split:]:
            if not hasattr(target, part):
                return False
            target = getattr(target, part)
        return True
    return False


def test_every_member_argument_is_classified() -> None:
    commands = frozenset(path for path, _ in _leaves(member)) - {"help"}
    assert _member_arguments() == {
        name: commands if taken is None else taken
        for name, (_kind, taken, _basis) in MEMBER_ARGUMENTS.items()
    }


def test_every_window_argument_is_classified() -> None:
    assert _window_arguments() == set(WINDOW_ARGUMENTS)


@pytest.mark.parametrize(
    ("kind", "basis"),
    [
        *((kind, basis) for kind, _commands, basis in MEMBER_ARGUMENTS.values()),
        *WINDOW_ARGUMENTS.values(),
    ],
)
def test_an_argument_bounded_by_authority_names_its_basis(kind, basis) -> None:
    """A value the grant or the member's authority bounds names the function
    that checks it, and that function exists; one the service bounds names
    its check or why; content names none."""
    if kind in {GRANT, MEMBER}:
        assert _resolves(basis), basis
    elif kind == SERVICE:
        assert _resolves(basis) or basis in _SERVICE_RATIONALES, basis
    else:
        assert kind == CONTENT and basis == ""


def test_no_argument_names_what_the_grant_settles() -> None:
    """The run, its work and the subject a completion acts on are the grant's,
    so no member command and no call of the window takes one."""
    assert set(_member_arguments()) & GRANT_SETTLED == set()
    assert {name for _call, name in _window_arguments()} & GRANT_SETTLED == set()
