"""The member's services inside a command's isolated environment: each is the
member's own command, run by the host through the command's window, and only
a member command's reported failures belong to the port's contract."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from guildbotics.entities import Task
from guildbotics.entities.team import Person, Project, Team
from guildbotics.guest.window import (
    MemberCommandError,
    WindowChatService,
    WindowCodeHostingService,
    WindowTicketService,
)
from guildbotics.intelligences.agent_runtime.wire import HostCallError
from guildbotics.runtime.port import ServiceError, WorkflowStatus

_PERSON = Person(person_id="aiko", name="Aiko")
_TICKET_URL = "local://tickets/acme/team/demo/T-1"
_PR_URL = "local://code/acme/team/demo/pulls/3"
_COMMENT = {
    "id": "comment-1",
    "body": "hello",
    "author": "otota",
    "author_type": "User",
    "created_at": None,
    "url": None,
}


def _client(**result: object) -> SimpleNamespace:
    return SimpleNamespace(
        acall=AsyncMock(
            return_value={"exit_code": 0, "stdout": "", "stderr": "", **result}
        )
    )


def _answering(page: dict[str, object]) -> SimpleNamespace:
    return _client(stdout=json.dumps(page))


def _ticket_service(client: SimpleNamespace) -> WindowTicketService:
    return WindowTicketService(
        client, _PERSON, Team(project=Project(name="demo"), members=[_PERSON])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("get_bot_identity", ()),
        ("resolve_channel_id", ("general",)),
        ("post_message", ("C1", "hello")),
        ("add_reaction", ("C1", "1.0", "ack")),
    ],
)
@pytest.mark.parametrize("failure", ["reported", "defect", "refused"])
async def test_chat_failure_contract(method, args, failure):
    refused = HostCallError("refused", "grant refused")
    client = SimpleNamespace(
        acall=AsyncMock(
            return_value={
                "exit_code": 1,
                "stdout": "",
                "stderr": "Error: denied"
                if failure == "reported"
                else "Traceback: defect",
            },
            side_effect=refused if failure == "refused" else None,
        )
    )
    expected = {
        "reported": (ServiceError, "denied"),
        "defect": (MemberCommandError, "Traceback: defect"),
        "refused": (HostCallError, "grant refused"),
    }
    error_type, message = expected[failure]
    with pytest.raises(error_type, match=message):
        await getattr(WindowChatService(client, "aiko"), method)(*args)


@pytest.mark.asyncio
async def test_a_workflow_status_is_not_posted_from_inside_a_command():
    client = _client()
    status = WorkflowStatus(reason="failed", person_id="aiko", run_id="run-1")

    with pytest.raises(NotImplementedError, match="workflow status"):
        await WindowChatService(client, "aiko").post_message(
            "C1", "notice", status=status
        )

    client.acall.assert_not_called()


@pytest.mark.asyncio
async def test_a_reply_is_the_members_chat_reply_command_with_its_text_on_stdin():
    client = _client(
        stdout=json.dumps(
            {
                "channel_id": "C1",
                "message_id": "2.0",
                "thread_id": "1.0",
                "occurred_at": "2026-10-01T00:00:02+00:00",
            }
        )
    )

    posted = await WindowChatService(client, "aiko").post_message(
        "C1", "hello", thread_id="1.0"
    )

    assert posted.message_id == "2.0"
    assert posted.occurred_at == datetime(2026, 10, 1, 0, 0, 2, tzinfo=UTC)
    client.acall.assert_awaited_once_with(
        "member",
        arguments=[
            "chat",
            "reply",
            "--thread-id",
            "1.0",
            "--channel-id",
            "C1",
            "--content-stdin",
            "--person",
            "aiko",
            "--format",
            "json",
        ],
        stdin="hello",
    )


def _read_arguments(group: str, resource: str, subject: str) -> list[str]:
    return [
        group,
        "read",
        "--person",
        "aiko",
        "--resource",
        resource,
        "--subject",
        subject,
        "--params",
        json.dumps({"page_size": 10}),
        "--continuation",
        "c1",
        "--format",
        "json",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("resource", "subject", "group"),
    [
        ("pull_request_comments", _PR_URL, "pr"),
        ("ticket_pull_requests", _TICKET_URL, "ticket"),
    ],
)
async def test_code_host_reads_are_the_members_read_command_of_the_subject(
    resource, subject, group
):
    client = _answering({"items": [], "continuation": "c2"})

    page = await WindowCodeHostingService(client, "aiko").read(
        resource, subject, parameters={"page_size": 10}, continuation="c1"
    )

    assert page.items == []
    assert page.continuation == "c2"
    client.acall.assert_awaited_once_with(
        "member", arguments=_read_arguments(group, resource, subject), stdin=""
    )


@pytest.mark.asyncio
async def test_ticket_reads_are_the_members_ticket_read_command():
    client = _answering(
        {
            "items": [_COMMENT],
            "continuation": None,
            "target": {
                "kind": "ticket",
                "url": _TICKET_URL,
                "title": "Fix it",
                "label": "T-1",
            },
        }
    )

    page = await _ticket_service(client).read(
        "ticket_comments", _TICKET_URL, parameters={"page_size": 10}, continuation="c1"
    )

    assert page.items == [_COMMENT]
    assert page.target is not None
    assert page.target.label == "T-1"
    client.acall.assert_awaited_once_with(
        "member",
        arguments=_read_arguments("ticket", "ticket_comments", _TICKET_URL),
        stdin="",
    )


@pytest.mark.asyncio
async def test_a_read_answer_out_of_the_resources_shape_is_refused():
    client = _answering({"items": [{"id": "comment-1"}]})

    with pytest.raises(ValueError, match="body"):
        await _ticket_service(client).read("ticket_comments", _TICKET_URL)


@pytest.mark.asyncio
async def test_an_unknown_resource_is_refused_before_the_host_is_asked():
    client = _client()

    with pytest.raises(ServiceError):
        await _ticket_service(client).read("issue_timeline", _TICKET_URL)

    client.acall.assert_not_called()


@pytest.mark.asyncio
async def test_a_reported_read_failure_is_the_ports_service_error():
    client = _client(exit_code=1, stderr="Error: Ticket not found.")

    with pytest.raises(ServiceError, match="Ticket not found."):
        await WindowCodeHostingService(client, "aiko").read("pull_requests", _PR_URL)


_TASK = Task(id="1", url=_TICKET_URL, title="Fix it", description="")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "call",
    [
        lambda s: s.comment(_TICKET_URL, "hello"),
        lambda s: s.create_ticket("acme/team/demo", "title", "body", []),
        lambda s: s.update_ticket(
            _TICKET_URL,
            body=None,
            title="new",
            add_labels=[],
            remove_labels=[],
            state=None,
        ),
        lambda s: s.add_reaction(_TICKET_URL, "comment-1", "ack"),
        lambda s: s.candidates(),
        lambda s: s.refresh(_TASK),
        lambda s: s.move(_TASK, "in_progress"),
        lambda s: s.add(_TICKET_URL),
        lambda s: s.closed_since(datetime.now(UTC), datetime.now(UTC)),
    ],
    ids=[
        "comment",
        "create_ticket",
        "update_ticket",
        "add_reaction",
        "candidates",
        "refresh",
        "move",
        "add",
        "closed_since",
    ],
)
async def test_ticket_writes_and_the_board_are_not_available_in_a_command(call):
    client = _client()

    with pytest.raises(NotImplementedError, match="isolated environment"):
        await call(_ticket_service(client))

    client.acall.assert_not_called()
