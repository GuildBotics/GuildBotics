"""Host ticket selection and settlement survive connection refusals, without
the deadline that brokered member commands carry."""

from types import SimpleNamespace

import pytest

from guildbotics.drivers.ticket_selector import TicketSelector
from guildbotics.entities.task import AwaitedRun, Task
from guildbotics.integrations import pull_request_patrol
from guildbotics.runtime.member_invocation import current_member_invocation
from guildbotics.utils.correlation import trace_scope
from tests.guildbotics.integrations.branvil_double import (
    MEMBER,
    REPO,
    refuse,
    services,
)


class _Context:
    def __init__(self, code, tickets):
        self.code, self.tickets = code, tickets
        self.person, self.team = code.person, code.team

    def clone_for(self, person):
        return self

    def get_code_hosting_service(self):
        return self.code

    def get_ticket_service(self):
        return self.tickets

    async def aclose(self):
        await self.code.aclose()
        await self.tickets.aclose()


# All eight provider branches in TicketSelector's host calls. Selection can
# itself write (done lanes, review limits); every request uses the same client.
_HOST_CALLS = [
    ("candidates", "pull", "GET", "/api/v1/pulls"),
    ("candidates", "ticket", "GET", "/api/v1/workspaces/acme/board/cards"),
    ("refresh", "pull", "GET", "/api/v1/repos/acme/demo/pulls/1"),
    ("refresh", "ticket", "GET", "/api/v1/repos/acme/demo/tickets/1"),
    ("move", "ticket", "PUT", ""),
    ("failure", "ticket", "POST", ""),
    ("failure", "pull", "POST", ""),
    ("checks", "pull", "POST", ""),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("phase", "kind", "method", "path"), _HOST_CALLS)
async def test_host_ticket_calls_wait_for_recovery_without_a_member_deadline(
    monkeypatch, phase, kind, method, path
):
    double, code, tickets = services(monkeypatch)
    ticket_url = double._ticket(REPO, "Work", "", [])["url"]
    double.assign(ticket_url)
    double.branch("feature")
    pull = await code.create_pull_request(
        REPO, "feature", "", "Work", "", draft=False, tickets=[]
    )
    pull_url = pull.target.url
    double.comment(pull_url, "reviewer", "Please fix")
    context = _Context(code, tickets)
    selector = TicketSelector(context)
    task = Task(
        id=ticket_url if kind == "ticket" else pull_url,
        url=ticket_url if kind == "ticket" else pull_url,
        title="Work",
        description="",
        status=Task.READY if kind == "ticket" else Task.IN_PROGRESS,
        pull_request_url=pull_url if kind == "pull" else None,
        trigger_reason=pull_request_patrol.CHECKS if phase == "checks" else None,
        resumes=(
            AwaitedRun(run_id="run-0", work_kind="ticket", work_identity=ticket_url)
            if phase == "checks"
            else None
        ),
    )
    with monkeypatch.context() as awaited:
        # The awaiting run's completion has reached this device.
        awaited.setattr(
            "guildbotics.drivers.ticket_selector.RunStore.status",
            lambda *_args: SimpleNamespace(summary="Awaiting CI."),
        )
        invocation = selector._invocation(context.person, task)
    assert invocation is not None
    clock = refuse(double, monkeypatch, times=200, method=method, path=path)
    assert current_member_invocation().deadline is None
    turns = []

    async def run_workflow(invocation):
        turns.append(invocation)
        if phase == "failure":
            raise RuntimeError("workflow failed")
        return "done"

    async def echo(context, text, *_args):
        return text

    monkeypatch.setattr("guildbotics.intelligences.functions.talk_as", echo)
    monkeypatch.setattr(
        "guildbotics.drivers.ticket_selector.RunStore.awaited_pull_requests",
        lambda *_args: [],
    )
    with trace_scope("routine", trace_id="host-run", person_id=MEMBER):
        if phase == "candidates":
            assert len(await selector.candidates(context.person)) == 2
        elif phase == "refresh":
            assert await selector.refresh(context.person, task) is not None
        elif phase == "failure":
            with pytest.raises(RuntimeError, match="workflow failed"):
                await selector.run(context.person, invocation, run_workflow)
        else:
            assert (
                await selector.run(context.person, invocation, run_workflow) == "done"
            )

    assert clock.now == 200.0
    if phase == "move":
        assert len(turns) == 1
        assert double.tickets[ticket_url]["lane"] == "in_progress"
        assert double.entries[ticket_url]["comments"] == []
    if phase in {"failure", "checks"}:
        url = ticket_url if kind == "ticket" else pull_url
        [notice] = [entry for entry in double.entries[url]["comments"] if entry["meta"]]
        assert notice["meta"]["reason"] == (
            "failed" if phase == "failure" else "checks_settled"
        )
