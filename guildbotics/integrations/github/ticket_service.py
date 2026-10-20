"""GitHub as a member's tickets: the issues of the configured owner's
repositories, and the Projects V2 board they are taken from."""

import re
from collections.abc import Sequence
from contextlib import suppress
from datetime import datetime
from functools import cached_property
from logging import Logger
from typing import Any, ClassVar, cast
from urllib.parse import quote

from guildbotics.entities import Person, Task, Team
from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.integrations.github.api import GitHubApi, GitHubItem
from guildbotics.integrations.github.async_client import GitHubNotFound
from guildbotics.integrations.github.github_utils import (
    get_agent_token,
    get_author_type,
    get_github_username,
    get_person_name,
    normalize_login,
    parse_timestamp,
)
from guildbotics.integrations.github.reads import (
    read_collection,
    reading,
    request_of,
)
from guildbotics.integrations.github.repository_scope import (
    ADD_PROJECT_ITEM,
    CREATE_PROJECT_FIELD,
    UPDATE_PROJECT_FIELD_OPTIONS,
    UPDATE_PROJECT_ITEM_STATUS,
)
from guildbotics.integrations.github.workflow_status_comment import (
    parse_workflow_status_comment,
)
from guildbotics.integrations.repository_scope import check_path
from guildbotics.integrations.ticket_selection import (
    linked_pull_state,
    mention_pending,
    select,
    since_assignment,
)
from guildbotics.runtime.port import (
    SemanticReaction,
    ServiceError,
    WorkflowStatus,
)
from guildbotics.runtime.resources import (
    TICKET_RESOURCES,
    CommentWritten,
    Lane,
    ReactionAdded,
    ReadPage,
    Ticket,
    TicketRelative,
    read_conditions,
)
from guildbotics.runtime.ticket_service import (
    ClosedItem,
    MoveLane,
    TicketCreated,
    TicketService,
    TicketUpdated,
)
from guildbotics.utils.i18n_tool import t

#: The project items of an issue, with their Status, and the issues it is
#: related to. GitHub holds at most 100 sub-issues of an issue; more of any
#: kind than one page fails the read rather than leave some out.
_ISSUE_PLACE = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      projectItems(first: 100) {
        nodes {
          project { id }
          fieldValueByName(name: "Status") {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
        }
        pageInfo { hasNextPage }
      }
      parent { ...Relative }
      subIssues(first: 100) { nodes { ...Relative } pageInfo { hasNextPage } }
      blockedBy(first: 100) { nodes { ...Relative } pageInfo { hasNextPage } }
      blocking(first: 100) { nodes { ...Relative } pageInfo { hasNextPage } }
    }
  }
}
fragment Relative on Issue { url title number state }
"""

#: The board's single-select fields: Status, and the Agent field when set up.
_PROJECT_FIELDS = """
query($proj: ID!, $cursor: String) {
  node(id: $proj) {
    ... on ProjectV2 {
      fields(first: 100, after: $cursor) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id
            name
            options { id name description color }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

#: The board's items with their single-select values (Status and Agent, read
#: by the field's name) and the issue each one holds.
_BOARD_ITEMS = """
query($proj: ID!, $cursor: String) {
  node(id: $proj) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        nodes {
          fieldValues(first: 20) {
            nodes {
              ... on ProjectV2ItemFieldSingleSelectValue {
                field {
                  __typename
                  ... on ProjectV2SingleSelectField { name }
                }
                name
                updatedAt
              }
            }
          }
          content {
            ... on Issue {
              id
              number
              url
              title
              body
              createdAt
              assignees(first: 10) { nodes { id login name } }
              timelineItems(last: 10, itemTypes: [ASSIGNED_EVENT]) {
                nodes {
                  ... on AssignedEvent {
                    createdAt
                    assignee { ... on User { login } }
                  }
                }
              }
              labels(first: 10) { nodes { name } }
              repository { name owner { login } }
              issueDependenciesSummary { blockedBy }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

#: The board's issues and pull requests, with when they closed.
_PROJECT_CONTENTS = """
query($proj: ID!, $cursor: String) {
  node(id: $proj) {
    ... on ProjectV2 {
      items(first: 100, after: $cursor) {
        nodes { content {
          __typename
          ... on Issue { number title url state closedAt repository { name owner { login } } }
          ... on PullRequest { number title url state closedAt mergedAt repository { name owner { login } } }
        } }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


class GitHubTicketService(GitHubApi, TicketService):
    """GitHub Projects V2 ticket service using GraphQL and REST APIs."""

    FIELD_AGENT: ClassVar[str] = "Agent"
    LANE_READY: ClassVar[str] = "ready"
    LANE_DONE: ClassVar[str] = "done"
    LANE_WORKING: ClassVar[str] = "working"
    DEFAULT_LANE_MAP: ClassVar[dict[str, str]] = {
        LANE_READY: "Todo",
        LANE_DONE: "Done",
        LANE_WORKING: "In Progress",
    }

    def __init__(self, logger: Logger, person: Person, team: Team):
        TicketService.__init__(self, logger, person, team)
        GitHubApi.__init__(self, person, team, Service.TICKET_SERVICE)
        config = team.project.get_service_config(Service.TICKET_SERVICE)
        self.project_id = str(config["project_id"])
        self.project_url = str(config["url"])
        self._mention_token = get_agent_token(person)

        self.lane_map = self._load_lane_map(cast(dict | None, config.get("lane_map")))

        # What the board's reads fill in.
        self._project_node_id: str | None = None
        self._status_field_id = ""
        self.columns: dict[str, str] = {}  # Task status -> option_id
        self._status_positions: dict[str, int] = {}  # Status option name -> position

    # --------------------------------------------------------------------- #
    #   Tickets                                                             #
    # --------------------------------------------------------------------- #

    async def read(
        self,
        resource: str,
        subject: str,
        *,
        parameters: dict[str, Any] | None = None,
        continuation: str = "",
    ) -> ReadPage:
        conditions = read_conditions(resource, parameters, continuation)
        if resource not in TICKET_RESOURCES:
            raise ServiceError(t("integrations.resources.resource"))
        item = self.item(subject, "ticket")
        with reading(resource):
            if resource == "tickets":
                data = await self.fetch(item, bounded=True)
                place = await self._place(item)
                parent = place["parent"]
                ticket = Ticket(
                    **self.target(item, data).model_dump(),
                    key=str(item.number),
                    tracker=item.repository,
                    repository=item.repository,
                    body=data.get("body") or "",
                    state=data["state"],
                    assignees=[user["login"] for user in data.get("assignees") or []],
                    labels=[label["name"] for label in data.get("labels") or []],
                    lane=await self._lane(place),
                    parent=_relative(parent) if parent else None,
                    children=_relatives(place["subIssues"]),
                    blocked_by=_relatives(place["blockedBy"]),
                    blocks=_relatives(place["blocking"]),
                )
                return ReadPage.of(resource, [ticket], target=self.target(item, data))
            request = request_of(self, resource, subject, conditions)
            return await read_collection(self, resource, item, request, continuation)

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        return await self.post_comment(self.item(url, "ticket"), body, status)

    async def create_ticket(
        self,
        tracker: str,
        title: str,
        body: str,
        labels: Sequence[str],
        *,
        add_to_board: bool = True,
    ) -> TicketCreated:
        """Open an issue in ``tracker`` and, if ``add_to_board``, add it to the
        configured Project.

        Raises:
            ServiceError: Saying the issue was created, if it could not be
                added to the Project: the member does not open it again.
        """
        repository = self.repository(tracker)
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = await self._defined_labels(repository, labels)
        client = await self.client()
        data = (await client.post(f"/repos/{repository}/issues", json=payload)).json()
        item = self.item(str(data["html_url"]), "ticket")
        created = TicketCreated(
            labels=_label_names(data), target=self.target(item, data)
        )
        if add_to_board:
            try:
                await self._get_project_item_id(str(data["node_id"]))
            except ServiceError as exc:
                raise ServiceError(
                    t(
                        "integrations.github.response.created_off_board",
                        url=created.target.url,
                        message=str(exc),
                    )
                ) from exc
        return created

    async def update_ticket(
        self,
        url: str,
        *,
        body: str | None,
        title: str | None,
        add_labels: Sequence[str],
        remove_labels: Sequence[str],
        state: str | None,
        state_reason: str | None,
        parent: str | None = None,
        add_blocked_by: Sequence[str] = (),
        remove_blocked_by: Sequence[str] = (),
    ) -> TicketUpdated:
        item = self.item(url, "ticket")
        new_parent = self.item(parent, "ticket") if parent else None
        adding = [self.item(blocker, "ticket") for blocker in add_blocked_by]
        removing = [self.item(blocker, "ticket") for blocker in remove_blocked_by]

        # Every issue whose relations change must be the member's to write,
        # checked before anything is written: a relation is written on this
        # issue or on its old or new parent, and the gate sees only the
        # repository a request is sent to. The old parent is known once the
        # issue's relations are read.
        def check(*changed: GitHubItem | None) -> None:
            for issue in changed:
                if issue is not None:
                    check_path(self.owner, issue.repository, provider="github")

        check(item, new_parent, *adding, *removing)
        place = await self._place(item) if parent is not None or adding else None
        current = place["parent"] if place and parent is not None else None
        old_parent = self.item(current["url"], "ticket") if current else None
        check(old_parent)
        payload: dict[str, Any] = {}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if state is not None:
            payload["state"] = state
            if state_reason is not None:
                payload["state_reason"] = state_reason
        # Label additions are validated up front so an undefined label aborts
        # before any write; the label endpoints are called only after the field
        # PATCH succeeded so a failed PATCH leaves the labels untouched.
        additions = await self._defined_labels(item.repository, add_labels)
        data = await self.fetch(item)
        if place and adding:
            # GitHub refuses a blocker the issue already has; adding it changes
            # nothing, as adding a label it carries does.
            had = {issue["url"].casefold() for issue in _nodes(place["blockedBy"])}
            adding = [b for b in adding if self.url(b).casefold() not in had]
        if new_parent is not None:
            await self.fetch(new_parent)
        # The dependency endpoints name an issue by its id, not its number.
        blockers = [(await self.fetch(b))["id"] for b in adding]
        unblockers = [(await self.fetch(b))["id"] for b in removing]
        state_changed = state is not None and data.get("state") != state
        client = await self.client()
        if payload:
            data = (await client.patch(item.api, json=payload)).json()
        # The label endpoints change only the labels they name, so labels a
        # human sets meanwhile survive; a PATCH of the whole set would not.
        labels = f"{item.api}/labels"
        for name in remove_labels:
            # One the issue does not carry is already removed.
            with suppress(GitHubNotFound):
                await client.delete(f"{labels}/{quote(name, safe='')}")
        if additions:
            await client.post(labels, json={"labels": list(additions)})
        if parent is not None:
            await self._reparent(data["id"], old_parent, new_parent)
        dependencies = f"{item.conversation}/dependencies/blocked_by"
        for blocker in blockers:
            await client.post(dependencies, json={"issue_id": blocker})
        for blocker in unblockers:
            # One the issue is not blocked by is already removed.
            with suppress(GitHubNotFound):
                await client.delete(f"{dependencies}/{blocker}")
        if additions or remove_labels:
            data = await self.fetch(item)
        return TicketUpdated(
            body=data.get("body") or "",
            state=str(data.get("state") or ""),
            state_changed=state_changed,
            labels=_label_names(data),
            target=self.target(item, data),
        )

    async def add_reaction(
        self, url: str, target_id: str, reaction: SemanticReaction
    ) -> ReactionAdded:
        return await self.react(self.item(url, "ticket"), target_id, reaction)

    async def _reparent(
        self,
        issue_id: int,
        current: GitHubItem | None,
        parent: GitHubItem | None,
    ) -> None:
        """Move the issue ``issue_id`` from its ``current`` parent to
        ``parent``, or to none.

        A sub-issue is added to and removed from its parent, by its id; GitHub
        moves one that has a parent only when told to replace it.
        """
        client = await self.client()
        if parent is None:
            if current is not None:
                await client.request(
                    "DELETE",
                    f"{current.conversation}/sub_issue",
                    json={"sub_issue_id": issue_id},
                )
        elif (
            current is None
            or self.url(current).casefold() != self.url(parent).casefold()
        ):
            await client.post(
                f"{parent.conversation}/sub_issues",
                json={"sub_issue_id": issue_id, "replace_parent": current is not None},
            )

    async def _defined_labels(
        self, repository: str, labels: Sequence[str]
    ) -> list[str]:
        """Map requested labels onto the labels the repository already defines.

        Labels are a shared vocabulary, so a member picks from the repository's
        own set and proposes a missing label to a human rather than creating
        one as a side effect of an issue write.
        """
        if not labels:
            return []
        defined = [
            name
            async for found in self.paginated(f"/repos/{repository}/labels")
            if (name := str(found.get("name") or ""))
        ]
        by_name = {name.casefold(): name for name in defined}
        resolved: list[str] = []
        for label in labels:
            canonical = by_name.get(label.strip().casefold())
            if canonical is None:
                raise ServiceError(
                    f"Label '{label}' is not defined in {repository}. "
                    f"Defined labels: {', '.join(defined) or '(none)'}. "
                    "Ask a human to add the label instead of introducing a new one."
                )
            if canonical not in resolved:
                resolved.append(canonical)
        return resolved

    async def _place(self, item: GitHubItem) -> dict[str, Any]:
        """The issue's project items and the issues it is related to."""
        owner, name = item.repository.split("/")
        data = await self.graphql(
            _ISSUE_PLACE, {"owner": owner, "repo": name, "number": item.number}
        )
        return data["repository"]["issue"]

    async def _lane(self, place: dict[str, Any]) -> Lane | None:
        """The issue's lane on the configured board; ``None`` off it."""
        project = await self._project_node()
        on_board = [
            node
            for node in _nodes(place["projectItems"])
            if (node.get("project") or {}).get("id") == project
        ]
        if not on_board:
            return None
        await self._sync_status_columns()
        option = (on_board[0].get("fieldValueByName") or {}).get("name")
        return cast(Lane, self._status_from_option(option) or Task.NEW)

    # --------------------------------------------------------------------- #
    #   The board                                                           #
    # --------------------------------------------------------------------- #

    def _load_lane_map(self, raw: dict | None) -> dict[str, str | None]:
        if raw is None:
            return dict(self.DEFAULT_LANE_MAP)

        ready = str(raw.get(self.LANE_READY) or self.DEFAULT_LANE_MAP[self.LANE_READY])
        done = str(raw.get(self.LANE_DONE) or self.DEFAULT_LANE_MAP[self.LANE_DONE])
        working_value = raw.get(
            self.LANE_WORKING, self.DEFAULT_LANE_MAP[self.LANE_WORKING]
        )
        working = str(working_value).strip() if working_value is not None else ""
        return {
            self.LANE_READY: ready.strip(),
            self.LANE_DONE: done.strip(),
            self.LANE_WORKING: working or None,
        }

    async def _project_node(self) -> str:
        """Return the node-ID of the configured Project V2."""
        if self._project_node_id:
            return self._project_node_id

        # Whether the owner is an organization or a user, as the board's URL says.
        is_org = "/orgs/" in self.project_url
        query_type = "organization" if is_org else "user"

        query = f"""
        query($owner:String!, $num:Int!){{
            {query_type}(login:$owner){{
                projectV2(number:$num){{ id }}
            }}
        }}
        """
        data = await self.graphql(
            query,
            {"owner": self.owner, "num": int(self.project_id)},
        )
        proj = data[query_type]["projectV2"]
        if not proj:
            raise ServiceError(
                f"ProjectV2 number={self.project_id} not found for {self.owner}"
            )
        self._project_node_id = proj["id"]
        assert self._project_node_id, "Project node ID must not be empty"
        return self._project_node_id

    # --------------------------------------------------------------------- #
    #   The board's fields                                                  #
    # --------------------------------------------------------------------- #

    async def _project_connection(
        self, query: str, connection: str
    ) -> list[dict[str, Any]]:
        """Every node of one of the board's connections, page by page."""
        project = await self._project_node()
        nodes: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = await self.graphql(query, {"proj": project, "cursor": cursor})
            page = data["node"][connection]
            nodes.extend(node for node in page["nodes"] if node)
            if not page["pageInfo"]["hasNextPage"]:
                return nodes
            cursor = page["pageInfo"]["endCursor"]

    async def _fields(self) -> dict[str, dict[str, Any]]:
        """The board's single-select fields by name, read afresh."""
        fields = await self._project_connection(_PROJECT_FIELDS, "fields")
        return {field["name"]: field for field in fields}

    async def _status_options(self) -> list[dict[str, Any]]:
        """The Status field's options in board order; its id is kept for moves."""
        status = (await self._fields()).get("Status")
        if status is None:
            raise ServiceError("Status field not found (after paginating all fields)")
        self._status_field_id = status["id"]
        return status["options"]

    def _cache_is_complete(self) -> bool:
        required = {Task.READY, Task.DONE}
        working = self.lane_map.get(self.LANE_WORKING)
        if working:
            required.add(Task.IN_PROGRESS)
        return required.issubset(self.columns)

    async def _sync_status_columns(self) -> None:
        if self._cache_is_complete():
            return
        options = await self._status_options()
        self._status_positions = {
            option["name"]: position for position, option in enumerate(options)
        }
        ids = {option["name"]: option["id"] for option in options}
        status_by_lane = {
            self.LANE_READY: Task.READY,
            self.LANE_DONE: Task.DONE,
            self.LANE_WORKING: Task.IN_PROGRESS,
        }
        self.columns = {
            status_by_lane[lane]: ids[name]
            for lane, name in self.lane_map.items()
            if name in ids
        }

    async def get_statuses(self) -> list[str]:
        """Return the board's Status option names, read afresh."""
        return [option["name"] for option in await self._status_options()]

    async def is_assignable_user(self, username: str) -> bool:
        """
        Return True if *username* resolves to an existing GitHub user account.

        Tickets are managed on the GitHub Project board (issues are promoted from
        drafts that humans triage), so assignability is not gated by a single
        repository's collaborator list. The only account-type-neutral check that
        works for both organization and user projects is resolving the login to a
        ``User`` node via GraphQL. No data is written to GitHub.
        """
        if not username:
            return False
        query = """
        query($login: String!) {
          user(login: $login) {
            id
          }
        }
        """
        try:
            resp = await self.graphql(query, {"login": username})
        except Exception:
            return False
        user = resp.get("user") if isinstance(resp, dict) else None
        return bool(user and user.get("id"))

    def _agent_options(self) -> list[dict[str, str]]:
        """The Agent options of the configured non-human members."""
        return [
            {"name": get_agent_token(member), "description": member.name}
            for member in self.team.members
            if member.person_type not in ["", "human"]
        ]

    async def get_agent_field_options(self) -> list[str]:
        """Return the option names of the board's ``Agent`` field, read afresh;
        none when the field is missing, which only :meth:`sync_agent_field`
        creates."""
        state = await self.get_agent_field_state()
        return [option["name"] for option in state["options"]]

    async def get_agent_field_state(self) -> dict[str, Any]:
        """Return the current state of the board's ``Agent`` field, read afresh.

        ``options`` are the members currently registered as field options;
        ``missing`` are configured non-human members not yet registered (what
        :meth:`sync_agent_field` would add). Each entry is
        ``{"name": <agent token>, "description": <member name>}``.
        """
        agent = (await self._fields()).get(self.FIELD_AGENT)
        current = [option["name"] for option in agent["options"]] if agent else []
        desired = self._agent_options()
        described = {option["name"]: option["description"] for option in desired}
        return {
            "exists": agent is not None,
            "options": [
                {"name": name, "description": described.get(name, "")}
                for name in current
            ],
            "missing": [option for option in desired if option["name"] not in current],
        }

    async def sync_agent_field(self) -> dict[str, Any]:
        """Create the ``Agent`` field (with options) or add missing options.

        When the field already exists, existing options are resubmitted *with
        their ids* so GitHub preserves them and does not clear ticket
        assignments; only the missing configured members are appended. Returns
        the refreshed state (same shape as :meth:`get_agent_field_state`).
        """
        agent = (await self._fields()).get(self.FIELD_AGENT)
        current = agent["options"] if agent else []
        names = {option["name"] for option in current}
        additions = [
            {**option, "color": "GRAY"}
            for option in self._agent_options()
            if option["name"] not in names
        ]
        if agent is None:
            await self.graphql(
                CREATE_PROJECT_FIELD,
                {
                    "proj": await self._project_node(),
                    "name": self.FIELD_AGENT,
                    "options": additions or None,
                },
            )
        elif additions:
            await self.graphql(
                UPDATE_PROJECT_FIELD_OPTIONS,
                {"field": agent["id"], "options": [*current, *additions]},
            )
        return await self.get_agent_field_state()

    def _status_from_option(self, option_name: str | None) -> str | None:
        """Map a board Status option to an internal lane by its board position.

        The ready and done lanes act as the boundaries of the work window:
        options positioned strictly between them are treated as working lanes,
        while options before ready (e.g. ``Backlog``) or at/after done (e.g.
        ``Icebox``) are ignored. This keeps configuration minimal—custom
        intermediate lanes such as ``In Review`` become actionable without any
        ``lane_map`` change, and pre/post lanes are skipped without one either.
        """
        if not option_name:
            return None
        ready_name = self.lane_map[self.LANE_READY]
        done_name = self.lane_map[self.LANE_DONE]
        if option_name == ready_name:
            return Task.READY
        if option_name == done_name:
            return Task.DONE

        ready_pos = self._status_positions.get(ready_name) if ready_name else None
        done_pos = self._status_positions.get(done_name) if done_name else None
        option_pos = self._status_positions.get(option_name)
        if ready_pos is None or done_pos is None or option_pos is None:
            return None
        if ready_pos < option_pos < done_pos:
            return Task.IN_PROGRESS
        return None

    async def get_all_tickets(self) -> list[dict]:
        """Retrieve all items of the GitHub Projects V2 board."""
        await self._sync_status_columns()
        return await self._project_connection(_BOARD_ITEMS, "items")

    @cached_property
    def _login(self) -> str:
        """The member's login, read only by what decides the member's work:
        reading the board's closed work needs no one's identity."""
        return normalize_login(get_github_username(self.person, strict=True))

    def _is_me(self, login: str) -> bool:
        """Whether a login from either GitHub API names this member."""
        return bool(self._login) and normalize_login(login) == self._login

    def _text_mentions_me(self, text: str | None) -> bool:
        """
        Return True when the given text contains a mention of the current user.
        Args:
            text (str | None): The text to check for mentions.
        Returns:
            bool: True if the text mentions the user, False otherwise.
        """
        if not text:
            return False

        if self._mention_token and self._mention_token in text:
            return True

        if self._login:
            mention_re = (
                r"(^|[^A-Za-z0-9_])@" + re.escape(self._login) + r"(?=$|[^A-Za-z0-9-])"
            )
            if re.search(mention_re, text, flags=re.IGNORECASE):
                return True

        return False

    async def _comments(self, item: GitHubItem) -> list[Message]:
        comments = []
        async for found in self.paginated(f"{item.conversation}/comments"):
            login = found["user"]["login"]
            author_type = get_author_type(self.person, login)
            comments.append(
                Message(
                    content=found["body"],
                    author=get_person_name(self.team.members, login) or author_type,
                    author_type=author_type,
                    timestamp=found["created_at"],
                )
            )
        return sorted(comments, key=lambda comment: comment.timestamp)

    async def _related_pull_request_state(self, item: GitHubItem) -> str | None:
        """The state of the pull request that refers to the issue -- ``merged``
        for a merged one -- choosing an open one, and the latest updated; ``None``
        when none does."""
        pulls = []
        async for event in self.paginated(f"{item.conversation}/timeline"):
            source = (event.get("source") or {}).get("issue") or {}
            if event.get("event") == "cross-referenced" and "pull_request" in source:
                merged = (source.get("pull_request") or {}).get("merged_at")
                pulls.append(
                    (
                        "merged" if merged else str(source.get("state") or ""),
                        str(source.get("updated_at") or ""),
                    )
                )
        return linked_pull_state(pulls)

    def _latest_assigned_event_time(self, issue: dict[str, Any]) -> str:
        """Return when this member was last added to the issue's assignees.

        Reads the ``ASSIGNED_EVENT`` timeline items already fetched by
        :meth:`get_all_tickets`, so no extra request is made. Returns an empty
        string when GitHub reports no such event (for example an assignment
        older than the fetched window), which leaves the assignment time
        unknown.
        """
        events = (issue.get("timelineItems") or {}).get("nodes") or []
        times = [
            str(event.get("createdAt") or "")
            for event in events
            if self._is_me(str((event.get("assignee") or {}).get("login") or ""))
        ]
        return max(times, default="")

    def _build_project_tasks(
        self, all_items: list[dict]
    ) -> tuple[list[Task], dict[str, dict[str, Any]]]:
        tasks: list[Task] = []
        task_metadata: dict[str, dict[str, Any]] = {}
        for it in all_items:
            # Single-select values by their field's name; other values are empty.
            values = {
                (value.get("field") or {}).get("name"): value
                for value in it["fieldValues"]["nodes"]
            }
            status = self._status_from_option((values.get("Status") or {}).get("name"))
            issue = it["content"]
            if status is None or status == Task.DONE or not issue:
                continue
            assignees = issue.get("assignees", {}).get("nodes", [])
            # Assignees and the Agent field are independent ways to assign this
            # member, and both can be set at once. Each active one carries its
            # own request time, so the most recent of them is the live request.
            by_assignee = any(self._is_me(str(a.get("login") or "")) for a in assignees)
            agent = values.get(self.FIELD_AGENT) or {}
            by_agent_field = agent.get("name") == self._mention_token
            is_assigned = by_assignee or by_agent_field

            assignment_times = []
            if by_assignee:
                assignment_times.append(self._latest_assigned_event_time(issue))
            if by_agent_field:
                assignment_times.append(agent.get("updatedAt") or "")
            assigned_at = max((time for time in assignment_times if time), default="")

            tasks.append(
                Task(
                    provider="github",
                    label=self.item(issue["url"], "ticket").label,
                    id=issue["id"],
                    url=issue.get("url"),
                    title=issue["title"],
                    description=issue.get("body", "") or "",
                    status=status,
                    created_at=issue["createdAt"],
                    assignee=self.person.person_id if is_assigned else None,
                )
            )
            task_metadata[issue["id"]] = {
                "assigned": is_assigned,
                "assigned_at": assigned_at,
                # The open issues it is blocked by.
                "blocked": issue["issueDependenciesSummary"]["blockedBy"] > 0,
            }
        return sorted(tasks), task_metadata

    async def _select_actionable_task(
        self, task: Task, metadata: dict[str, Any]
    ) -> Task | None:
        # A ticket is not started while an issue it is blocked by is open.
        if not metadata["assigned"] or (
            task.status == Task.READY and metadata["blocked"]
        ):
            return None
        assert task.url
        item = self.item(task.url, "ticket")
        task.comments = await self._comments(item)
        # The agent still receives the whole history on the task; only the
        # decision is limited to what was said after the assignment.
        since = since_assignment(task.comments, str(metadata.get("assigned_at") or ""))
        return await select(
            task,
            since,
            latest_status=parse_workflow_status_comment(since[-1].content)
            if since
            else None,
            pending=mention_pending(task.description, since, self._text_mentions_me),
            linked_pull=lambda: self._related_pull_request_state(item),
            done=lambda: self.move(task, "done"),
        )

    async def candidates(self) -> list[Task]:
        """Return every currently actionable issue in patrol order."""
        candidates: list[Task] = []
        all_items = await self.get_all_tickets()
        tasks, task_metadata = self._build_project_tasks(all_items)
        ready_tasks = [task for task in tasks if task.status == Task.READY]
        working_tasks = [task for task in tasks if task.status != Task.READY]
        for task in [*ready_tasks, *working_tasks]:
            assert task.id, "Task ID must be set"
            selected = await self._select_actionable_task(task, task_metadata[task.id])
            if selected:
                candidates.append(selected)
        return candidates

    async def refresh(self, candidate: Task) -> Task | None:
        """Re-read one patrol candidate and apply the canonical work test."""
        all_items = await self.get_all_tickets()
        tasks, task_metadata = self._build_project_tasks(all_items)
        for task in tasks:
            if task.id != candidate.id:
                continue
            assert task.id, "Task ID must be set"
            return await self._select_actionable_task(task, task_metadata[task.id])
        return None

    async def _get_project_item_id(self, issue_node_id: str) -> str:
        project_id = await self._project_node()
        data = await self.graphql(
            ADD_PROJECT_ITEM, {"proj": project_id, "content": issue_node_id}
        )
        return data["addProjectV2ItemById"]["item"]["id"]

    async def move(self, task: Task, lane: MoveLane) -> bool:
        """Set the ticket's Status to the lane's option; ``False`` when the
        lane has none (the working lane is not configured, or is not on the
        board)."""
        await self._sync_status_columns()
        option_id = self.columns.get(lane)
        if not option_id:
            return False
        assert task.id, "Task ID must be set before moving"
        await self.graphql(
            UPDATE_PROJECT_ITEM_STATUS,
            {
                "proj": await self._project_node(),
                "item": await self._get_project_item_id(task.id),
                "field": self._status_field_id,
                "opt": option_id,
            },
        )
        return True

    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        """The Project's issues and pull requests closed in ``[start, end]``,
        with the closed pull requests of every repository the Project holds
        items of: those reach the board only through their issue."""
        items = [
            item.get("content") or {}
            for item in await self._project_connection(_PROJECT_CONTENTS, "items")
        ]
        closed: list[ClosedItem] = []
        for repository in sorted({_repository_of(item) for item in items} - {""}):
            closed.extend(await self._closed_pull_requests(repository, start))
        closed.extend(
            item
            for item in (_closed_item(content) for content in items)
            if item is not None
        )
        return [
            item
            for item in closed
            if (occurred := parse_timestamp(item.closed_at)) is not None
            and start <= occurred <= end
        ]

    async def _closed_pull_requests(
        self, repository: str, start: datetime
    ) -> list[ClosedItem]:
        closed: list[ClosedItem] = []
        async for pull_request in self.paginated(
            f"/repos/{repository}/pulls",
            params={"state": "closed", "sort": "updated", "direction": "desc"},
        ):
            updated_at = parse_timestamp(str(pull_request.get("updated_at") or ""))
            if updated_at is not None and updated_at < start:
                break
            owner, _, name = repository.partition("/")
            item = _closed_item(
                {
                    "__typename": "PullRequest",
                    "number": pull_request.get("number"),
                    "title": pull_request.get("title"),
                    "url": pull_request.get("html_url"),
                    "state": "CLOSED",
                    "closedAt": pull_request.get("closed_at"),
                    "mergedAt": pull_request.get("merged_at"),
                    "repository": {"name": name, "owner": {"login": owner}},
                }
            )
            if item is not None:
                closed.append(item)
        return closed


def _closed_item(content: dict[str, Any]) -> ClosedItem | None:
    """The closed issue or pull request ``content`` names, if it is one."""
    number = content.get("number")
    url = str(content.get("url") or "")
    if (
        content.get("state") != "CLOSED"
        or not content.get("closedAt")
        or not _repository_of(content)
        or not isinstance(number, int)
        or number <= 0
        or not url
    ):
        return None
    merged_at = content.get("mergedAt")
    return ClosedItem(
        kind="pull_request" if content.get("__typename") == "PullRequest" else "ticket",
        url=url,
        title=str(content.get("title") or ""),
        label=f"#{number}",
        closed_at=str(merged_at or content["closedAt"]),
        merged=bool(merged_at),
    )


def _repository_of(content: dict[str, Any]) -> str:
    repository = content.get("repository") or {}
    owner = repository.get("owner") or {}
    return "/".join(
        part
        for part in (str(owner.get("login") or ""), str(repository.get("name") or ""))
        if part
    )


def _nodes(connection: dict[str, Any]) -> list[dict[str, Any]]:
    """The nodes of ``connection``, which must all be on its one page."""
    if connection["pageInfo"]["hasNextPage"]:
        raise ServiceError(t("integrations.github.response.response"))
    return connection["nodes"]


def _relative(issue: dict[str, Any]) -> TicketRelative:
    return TicketRelative(
        kind="ticket",
        url=issue["url"],
        title=issue["title"],
        label=f"#{issue['number']}",
        state=str(issue["state"]).lower(),
    )


def _relatives(connection: dict[str, Any]) -> list[TicketRelative]:
    return [_relative(issue) for issue in _nodes(connection)]


def _label_names(issue: dict[str, Any]) -> list[str]:
    return [str(label.get("name", "")) for label in issue.get("labels") or []]
