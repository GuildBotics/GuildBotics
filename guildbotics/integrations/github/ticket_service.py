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

#: The project items of an issue, with their Status.
_ISSUE_PROJECT_ITEMS = """
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
        self.columns: dict[str, str] = {}  # Task status -> option_id
        self._status_positions: dict[str, int] = {}  # Status option name -> position
        self.custom_fields: dict[str, dict[str, Any]] = {}  # field_name -> field_info

        #: Custom field definitions
        agents = []
        for member in team.members:
            if member.person_type not in ["", "human"]:
                agents.append(
                    {
                        "name": get_agent_token(member),
                        "description": member.name,
                    }
                )

        self._custom_field_definitions: dict[str, dict] = {
            GitHubTicketService.FIELD_AGENT: {
                "dataType": "SINGLE_SELECT",
                "options": agents,
            },
        }

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
                ticket = Ticket(
                    **self.target(item, data).model_dump(),
                    key=str(item.number),
                    tracker=item.repository,
                    repository=item.repository,
                    body=data.get("body") or "",
                    state=data["state"],
                    assignees=[user["login"] for user in data.get("assignees") or []],
                    labels=[label["name"] for label in data.get("labels") or []],
                    lane=await self._lane(item),
                )
                return ReadPage.of(resource, [ticket], target=self.target(item, data))
            request = request_of(self, resource, subject, conditions)
            return await read_collection(self, resource, item, request, continuation)

    async def comment(
        self, url: str, body: str, *, status: WorkflowStatus | None = None
    ) -> CommentWritten:
        return await self.post_comment(self.item(url, "ticket"), body, status)

    async def create_ticket(
        self, tracker: str, title: str, body: str, labels: Sequence[str]
    ) -> TicketCreated:
        repository = self.repository(tracker)
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = await self._defined_labels(repository, labels)
        client = await self.client()
        data = (await client.post(f"/repos/{repository}/issues", json=payload)).json()
        item = self.item(str(data["html_url"]), "ticket")
        return TicketCreated(labels=_label_names(data), target=self.target(item, data))

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
    ) -> TicketUpdated:
        item = self.item(url, "ticket")
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

    async def _lane(self, item: GitHubItem) -> Lane | None:
        """The issue's lane on the configured board; ``None`` off it."""
        owner, name = item.repository.split("/")
        data = await self.graphql(
            _ISSUE_PROJECT_ITEMS, {"owner": owner, "repo": name, "number": item.number}
        )
        items = data["repository"]["issue"]["projectItems"]
        if items["pageInfo"]["hasNextPage"]:
            raise ServiceError(t("integrations.github.response.response"))
        project = await self._project_node()
        on_board = [
            node
            for node in items["nodes"]
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
    #   Status field & option utilities                                     #
    # --------------------------------------------------------------------- #

    async def _get_status_field(self) -> tuple[str, dict[str, dict]]:
        proj_id = await self._project_node()

        query = """
        query($proj:ID!,$first:Int!,$after:String){
        node(id:$proj){
            ... on ProjectV2{
            fields(first:$first, after:$after){
                nodes{
                ... on ProjectV2SingleSelectField{
                    id
                    name
                    options{ id name }
                }
                }
                pageInfo{ endCursor hasNextPage }
            }
            }
        }
        }
        """

        after: str | None = None
        while True:
            data = await self.graphql(
                query, {"proj": proj_id, "first": 100, "after": after}
            )
            fields = data["node"]["fields"]
            for fld in fields["nodes"]:
                if fld and fld["name"] == "Status":
                    opts = {
                        o["name"]: {"id": o["id"], "name": o["name"], "position": idx}
                        for idx, o in enumerate(fld["options"])
                    }
                    return fld["id"], opts

            if not fields["pageInfo"]["hasNextPage"]:
                break
            after = fields["pageInfo"]["endCursor"]

        raise ServiceError("Status field not found (after paginating all fields)")

    def _cache_is_complete(self) -> bool:
        required = {Task.READY, Task.DONE}
        working = self.lane_map.get(self.LANE_WORKING)
        if working:
            required.add(Task.IN_PROGRESS)
        return required.issubset(self.columns)

    async def _sync_status_columns(self) -> None:
        if self._cache_is_complete():
            return

        _, columns_local = await self._get_status_field()

        self._status_positions = {
            name: info["position"] for name, info in columns_local.items()
        }

        current_set = set(columns_local)
        self.columns = {}
        status_by_lane = {
            self.LANE_READY: Task.READY,
            self.LANE_DONE: Task.DONE,
            self.LANE_WORKING: Task.IN_PROGRESS,
        }
        for lane, option_name in self.lane_map.items():
            if option_name in current_set:
                self.columns[status_by_lane[lane]] = columns_local[option_name]["id"]

    async def get_statuses(self) -> list[str]:
        """
        Return the list of Status column names.

        Ensures the Status column set is synced before fetching.
        """
        _, columns_local = await self._get_status_field()
        return list(columns_local)

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

    async def get_agent_field_options(self) -> list[str]:
        """Return the option names of the project's ``Agent`` custom field.

        Read-only: unlike :meth:`ensure_custom_fields`, this does not create the
        field if it is missing (an empty list is returned in that case).
        """
        fields = await self._get_custom_fields()
        agent_field = fields.get(GitHubTicketService.FIELD_AGENT, {})
        options = agent_field.get("options", {})
        return list(options) if isinstance(options, dict) else []

    def _desired_agent_options(self) -> list[dict[str, str]]:
        """The Agent options expected from configured non-human members."""
        config = self._custom_field_definitions[GitHubTicketService.FIELD_AGENT]
        return list(config.get("options", []))

    async def get_agent_field_state(self) -> dict[str, Any]:
        """Return the current state of the project's ``Agent`` custom field.

        Read-only. ``options`` are the members currently registered as field
        options; ``missing`` are configured non-human members not yet registered
        (what :meth:`sync_agent_field` would add). Each entry is
        ``{"name": <agent token>, "description": <member name>}``.
        """
        self.custom_fields = {}  # force a fresh read
        fields = await self._get_custom_fields()
        agent_field = fields.get(GitHubTicketService.FIELD_AGENT)
        desired = self._desired_agent_options()
        desired_by_name = {opt["name"]: opt.get("description", "") for opt in desired}
        if agent_field is None:
            return {
                "exists": False,
                "options": [],
                "missing": [
                    {"name": opt["name"], "description": opt.get("description", "")}
                    for opt in desired
                ],
            }
        current_names = list(agent_field.get("options", {}).keys())
        options = [
            {"name": name, "description": desired_by_name.get(name, "")}
            for name in current_names
        ]
        missing = [
            {"name": opt["name"], "description": opt.get("description", "")}
            for opt in desired
            if opt["name"] not in current_names
        ]
        return {"exists": True, "options": options, "missing": missing}

    async def sync_agent_field(self) -> dict[str, Any]:
        """Create the ``Agent`` field (with options) or add missing options.

        When the field already exists, existing options are resubmitted *with
        their ids* so GitHub preserves them and does not clear ticket
        assignments; only the missing configured members are appended. Returns
        the refreshed state (same shape as :meth:`get_agent_field_state`).
        """
        self.custom_fields = {}  # force a fresh read
        fields = await self._get_custom_fields()
        agent_field = fields.get(GitHubTicketService.FIELD_AGENT)
        desired = self._desired_agent_options()

        if agent_field is None:
            await self._create_custom_field(
                GitHubTicketService.FIELD_AGENT,
                self._custom_field_definitions[GitHubTicketService.FIELD_AGENT],
            )
        else:
            current = await self._fetch_single_select_options(agent_field["id"])
            current_names = {opt["name"] for opt in current}
            additions = [opt for opt in desired if opt["name"] not in current_names]
            if additions:
                merged: list[dict[str, str]] = [
                    {
                        "id": opt["id"],
                        "name": opt["name"],
                        "description": opt.get("description", ""),
                        "color": opt.get("color", "GRAY"),
                    }
                    for opt in current
                ] + [
                    {
                        "name": opt["name"],
                        "description": opt.get("description", ""),
                        "color": "GRAY",
                    }
                    for opt in additions
                ]
                await self._update_single_select_options(agent_field["id"], merged)

        return await self.get_agent_field_state()

    async def _fetch_single_select_options(self, field_id: str) -> list[dict[str, str]]:
        """Return the full option records (id/name/description/color) of a field."""
        query = """
        query($id: ID!) {
          node(id: $id) {
            ... on ProjectV2SingleSelectField {
              options { id name description color }
            }
          }
        }
        """
        data = await self.graphql(query, {"id": field_id})
        node = data.get("node") or {}
        return [opt for opt in (node.get("options") or []) if opt]

    async def _update_single_select_options(
        self, field_id: str, options: list[dict[str, str]]
    ) -> None:
        """Overwrite a single-select field's option set (must include existing ids)."""
        await self.graphql(
            UPDATE_PROJECT_FIELD_OPTIONS, {"field": field_id, "options": options}
        )

    async def get_column_id(self, column_name: str) -> str | None:
        """
        Return the option-ID corresponding to *column_name*.

        Ensures the Status column set is synced before fetching.
        """
        await self._sync_status_columns()
        return self.columns.get(column_name, None)

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

    def _issue_to_task(
        self,
        issue: dict,
        status: str,
        assignee: str | None,
    ) -> Task:
        """
        Convert a GitHub Issue JSON to a Task instance.

        Args:
            issue (dict): The issue JSON payload.
            status (str): The current status.
            assignee (str | None): The assignee of the task.

        Returns:
            Task: The converted Task object.
        """
        return Task(
            provider="github",
            label=self.item(issue["url"], "ticket").label,
            id=issue["id"],
            url=issue.get("url"),
            title=issue["title"],
            description=issue.get("body", "") or "",
            status=status,
            created_at=issue["createdAt"],
            assignee=assignee,
        )

    async def get_all_tickets(self) -> list[dict]:
        """Retrieve all tickets from the GitHub Projects V2 board."""
        await self._sync_status_columns()
        await self.ensure_custom_fields()
        proj_node = await self._project_node()

        # Build GraphQL fragments for custom fields
        custom_field_fragments = []
        for _field_name, field_info in self.custom_fields.items():
            data_type = field_info["dataType"]
            if data_type == "SINGLE_SELECT":
                # updatedAt is requested for every single-select field; only
                # the Agent field's value is read, to date the assignment.
                custom_field_fragments.append(
                    """
                ... on ProjectV2ItemFieldSingleSelectValue {
                    field {
                        __typename
                        ... on ProjectV2SingleSelectField { id name }
                    }
                    name
                    updatedAt
                }
                """
                )
            elif data_type == "NUMBER":
                custom_field_fragments.append(
                    """
                ... on ProjectV2ItemFieldNumberValue {
                    field {
                        __typename
                        ... on ProjectV2Field { id }
                    }
                    number
                }
                """
                )
            elif data_type == "DATE":
                custom_field_fragments.append(
                    """
                ... on ProjectV2ItemFieldDateValue {
                    field {
                        __typename
                        ... on ProjectV2Field { id }
                    }
                    date
                }
                """
                )
            elif data_type == "TEXT":
                custom_field_fragments.append(
                    """
                ... on ProjectV2ItemFieldTextValue {
                    field {
                        __typename
                        ... on ProjectV2Field { id }
                    }
                    text
                }
                """
                )

        # Fetch all items via cursor-based pagination
        all_items: list[dict] = []
        cursor: str | None = None
        while True:
            query = f"""
            query($proj: ID!, $cursor: String) {{
              node(id: $proj) {{
                ... on ProjectV2 {{
                  items(first: 100, after: $cursor) {{
                    nodes {{
                      fieldValues(first: 20) {{
                        nodes {{
                          ... on ProjectV2ItemFieldSingleSelectValue {{
                            field {{
                              __typename
                              ... on ProjectV2SingleSelectField {{ name }}
                            }}
                            name
                          }}
                          {"".join(custom_field_fragments)}
                        }}
                      }}
                      content {{
                        ... on Issue {{
                          id
                          number
                          url
                          title
                          body
                          createdAt
                          assignees(first:10) {{ nodes {{ id login name }} }}
                          timelineItems(last: 10, itemTypes: [ASSIGNED_EVENT]) {{
                            nodes {{
                              ... on AssignedEvent {{
                                createdAt
                                assignee {{ ... on User {{ login }} }}
                              }}
                            }}
                          }}
                          labels(first:10)    {{ nodes {{ name }} }}
                          repository {{
                            name
                            owner {{ login }}
                          }}
                        }}
                      }}
                    }}
                    pageInfo {{
                      hasNextPage
                      endCursor
                    }}
                  }}
                }}
              }}
            }}
            """
            variables = {"proj": proj_node, "cursor": cursor}
            resp = await self.graphql(query, variables)
            payload = resp["node"]["items"]
            all_items.extend(payload["nodes"])
            if not payload["pageInfo"]["hasNextPage"]:
                break
            cursor = payload["pageInfo"]["endCursor"]
        return all_items

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

    def _pending_mention(self, description: str, comments: Sequence[Message]) -> bool:
        """Return True when a mention of this member is still unanswered.

        A mention in the description or in any comment marks the member as
        summoned; the member's own next comment answers it.
        """
        pending = self._text_mentions_me(description)
        for comment in comments:
            if self._text_mentions_me(comment.content):
                pending = True
            if comment.author_type == Message.ASSISTANT:
                pending = False
        return pending

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
        candidates = [pull for pull in pulls if pull[0] == "open"] or pulls
        return max(candidates, key=lambda pull: pull[1])[0] if candidates else None

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

    def _comments_since_assignment(
        self, comments: Sequence[Message], assigned_at: str
    ) -> list[Message]:
        """Return the comments that count as evidence about the assigned work.

        Being assigned is how a human asks this member to act, so only what was
        said from that point on can show whether the member has already acted.
        Comments the member wrote earlier were written in another capacity —
        as the issue's author or a reviewer — and must not suppress the run.

        The whole history is kept on the task for the agent to read; this
        narrower view only drives the selection decision. When no assignment
        path reports a time, every comment is kept: an unknown request time
        must never be a reason to discard evidence and redo finished work.
        """
        since = parse_timestamp(assigned_at)
        if since is None:
            return list(comments)
        return [
            comment
            for comment in comments
            if (timestamp := parse_timestamp(comment.timestamp)) is None
            or timestamp >= since
        ]

    def _build_project_tasks(
        self, all_items: list[dict]
    ) -> tuple[list[Task], dict[str, dict[str, Any]]]:
        tasks: list[Task] = []
        task_metadata: dict[str, dict[str, Any]] = {}
        for it in all_items:
            status: str | None = None
            field_values = {}
            field_updated_at: dict[str, str] = {}

            for fv in it["fieldValues"]["nodes"]:
                field = fv.get("field", {})
                field_name = field.get("name")
                field_id = field.get("id")

                if field_name == "Status":
                    status = self._status_from_option(fv["name"])
                elif field_id in [info["id"] for info in self.custom_fields.values()]:
                    custom_field_name = next(
                        (
                            name
                            for name, info in self.custom_fields.items()
                            if info["id"] == field_id
                        ),
                        None,
                    )
                    if custom_field_name:
                        if "name" in fv:  # SINGLE_SELECT
                            field_values[custom_field_name] = fv["name"]
                            field_updated_at[custom_field_name] = (
                                fv.get("updatedAt") or ""
                            )
                        elif "number" in fv:  # NUMBER
                            field_values[custom_field_name] = fv["number"]
                        elif "date" in fv:  # DATE
                            field_values[custom_field_name] = fv["date"]
                        elif "text" in fv:  # TEXT
                            field_values[custom_field_name] = fv["text"]

            if status is None or status == Task.DONE:
                continue

            issue = it["content"]
            if not issue:
                continue
            assignees = issue.get("assignees", {}).get("nodes", [])
            # Assignees and the Agent field are independent ways to assign this
            # member, and both can be set at once. Each active one carries its
            # own request time, so the most recent of them is the live request.
            by_assignee = any(self._is_me(str(a.get("login") or "")) for a in assignees)
            agent_field = GitHubTicketService.FIELD_AGENT
            by_agent_field = field_values.get(agent_field) == self._mention_token
            is_assigned = by_assignee or by_agent_field
            assignee = self.person.person_id if is_assigned else None

            assignment_times = []
            if by_assignee:
                assignment_times.append(self._latest_assigned_event_time(issue))
            if by_agent_field:
                assignment_times.append(field_updated_at.get(agent_field, ""))
            assigned_at = max((time for time in assignment_times if time), default="")

            task = self._issue_to_task(issue, status, assignee)
            tasks.append(task)
            assert task.id, "Task ID must be set"
            task_metadata[task.id] = {
                "assigned": is_assigned,
                "assigned_at": assigned_at,
            }

        tasks = sorted(tasks)
        return tasks, task_metadata

    async def _select_actionable_task(
        self, task: Task, metadata: dict[str, Any]
    ) -> Task | None:
        if not metadata["assigned"]:
            return None
        assert task.url
        item = self.item(task.url, "ticket")
        task.comments = await self._comments(item)
        # The agent still receives the whole history on the task; only the
        # decision below is limited to what was said after the assignment.
        since_assignment = self._comments_since_assignment(
            task.comments, str(metadata.get("assigned_at") or "")
        )
        mention_pending = self._pending_mention(task.description, since_assignment)
        last_comment_is_mine = (
            bool(since_assignment)
            and since_assignment[-1].author_type == Message.ASSISTANT
        )

        if task.status == Task.READY:
            if _latest_workflow_status_suppresses_selection(since_assignment):
                return None
            if last_comment_is_mine and not mention_pending:
                return None
            task.trigger_reason = "ready_lane"
            return task

        pull = await self._related_pull_request_state(item)
        if pull is not None:
            # A merged PR finishes the ticket; an open one is followed on the
            # PR itself by the pull request patrol, never through the issue.
            if pull == "merged":
                await self.move(task, "done")
            return None

        if _latest_workflow_status_suppresses_selection(since_assignment):
            return None

        if since_assignment and not last_comment_is_mine:
            task.trigger_reason = "issue_comment"
            return task
        if not since_assignment:
            task.trigger_reason = "issue_mention" if mention_pending else "working_lane"
            return task
        return None

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
        proj_node = await self._project_node()
        # update ProjectV2 item field value
        assert task.id, "Task ID must be set before moving"
        item_id = await self._get_project_item_id(task.id)
        status_field_id, _ = await self._get_status_field()
        option_id = await self.get_column_id(lane)
        if not option_id:
            return False

        await self.graphql(
            UPDATE_PROJECT_ITEM_STATUS,
            {
                "proj": proj_node,
                "item": item_id,
                "field": status_field_id,
                "opt": option_id,
            },
        )
        return True

    async def add(self, ticket_url: str) -> bool:
        """Add the issue to the configured Project."""
        data = await self.fetch(self.item(ticket_url, "ticket"))
        await self._get_project_item_id(str(data["node_id"]))
        return True

    async def closed_since(self, start: datetime, end: datetime) -> list[ClosedItem]:
        """The Project's issues and pull requests closed in ``[start, end]``,
        with the closed pull requests of every repository the Project holds
        items of: those reach the board only through their issue."""
        items = await self._project_contents()
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

    async def _project_contents(self) -> list[dict[str, Any]]:
        query = """
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
        project = await self._project_node()
        contents: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = await self.graphql(query, {"proj": project, "cursor": cursor})
            connection = data["node"]["items"]
            contents.extend(
                item.get("content") or {} for item in connection.get("nodes") or []
            )
            if not connection["pageInfo"]["hasNextPage"]:
                return contents
            cursor = str(connection["pageInfo"]["endCursor"])

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

    async def _get_custom_fields(self) -> dict[str, dict[str, Any]]:
        """
        Get all custom fields for the project.

        Returns:
            dict: Custom field information keyed by field name.
        """
        if self.custom_fields:
            return self.custom_fields

        proj_id = await self._project_node()

        query = """
        query($proj:ID!,$first:Int!,$after:String){
            node(id:$proj){
                ... on ProjectV2{
                    fields(first:$first, after:$after){
                        nodes{
                            ... on ProjectV2Field{
                                id
                                name
                                dataType
                            }
                            ... on ProjectV2SingleSelectField{
                                id
                                name
                                dataType
                                options{ id name description color }
                            }
                        }
                        pageInfo{ endCursor hasNextPage }
                    }
                }
            }
        }
        """

        after: str | None = None
        all_fields = {}

        while True:
            data = await self.graphql(
                query, {"proj": proj_id, "first": 100, "after": after}
            )
            fields = data["node"]["fields"]

            for field in fields["nodes"]:
                if field and field["name"] in self._custom_field_definitions:
                    field_info: dict[str, Any] = {
                        "id": field["id"],
                        "name": field["name"],
                        "dataType": field["dataType"],
                    }
                    # Normalize SINGLE_SELECT options to a mapping: name -> optionId
                    if field.get("dataType") == "SINGLE_SELECT":
                        opts = field.get("options", []) or []
                        field_info["options"] = {o["name"]: o["id"] for o in opts if o}
                    all_fields[field["name"]] = field_info

            if not fields["pageInfo"]["hasNextPage"]:
                break
            after = fields["pageInfo"]["endCursor"]

        self.custom_fields = all_fields
        return self.custom_fields

    async def _create_custom_field(self, field_name: str, field_config: dict) -> dict:
        """
        Create a custom field in the project.

        Args:
            field_name: Name of the field to create.
            field_config: Field configuration.

        Returns:
            Created field information.
        """
        proj_id = await self._project_node()

        # Prepare options for SINGLE_SELECT fields
        options: list[dict[str, str]] = field_config.get("options", [])

        for opt in options:
            if "color" not in opt:
                opt["color"] = "GRAY"

        variables = {
            "proj": proj_id,
            "name": field_name,
            "dataType": field_config["dataType"],
            "options": options if options else None,
        }

        data = await self.graphql(CREATE_PROJECT_FIELD, variables)
        field = data["createProjectV2Field"]["projectV2Field"]

        # Format field info
        field_info = {
            "id": field["id"],
            "name": field["name"],
            "dataType": field["dataType"],
            "options": field.get("options", []),
        }

        return field_info

    async def ensure_custom_fields(self) -> None:
        """
        Ensure all required custom fields exist, creating them if necessary.
        Also, for existing SINGLE_SELECT fields, ensure options are up to date.
        """
        existing_fields = await self._get_custom_fields()
        created_any = False

        for field_name, field_config in self._custom_field_definitions.items():
            if field_name not in existing_fields:
                # Create field then refresh cache to get option IDs
                await self._create_custom_field(field_name, field_config)
                created_any = True
            elif (
                field_config.get("dataType") == "SINGLE_SELECT"
                and "options" in field_config
            ):
                # For existing fields, check and update options if necessary
                desired_options = field_config["options"]
                current_options = existing_fields[field_name].get("options", {})
                current_option_names = list(current_options.keys())
                missing_options = {}
                for opt in desired_options:
                    if opt["name"] not in current_option_names:
                        missing_options[opt["name"]] = opt.get("description", "")

                if missing_options:
                    message = t(
                        "integrations.github.ticket_service.add_custom_field_options",
                        field=field_name,
                        options="\n"
                        + "\n".join(
                            f"  - {label}: {description}"
                            for label, description in missing_options.items()
                        ),
                    )
                    self.logger.warning(message)

        # If any fields were created, refresh the local cache to include option IDs
        if created_any:
            # Clear cache and re-fetch
            self.custom_fields = {}
            await self._get_custom_fields()


def _latest_workflow_status_suppresses_selection(
    comments: Sequence[Message],
) -> bool:
    """Whether the latest comment is the member's workflow status that keeps
    the ticket out of selection (a failure, or a rate limit not yet reset)."""
    if not comments or comments[-1].author_type != Message.ASSISTANT:
        return False
    status = parse_workflow_status_comment(comments[-1].content)
    return status is not None and status.holds_selection()


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


def _label_names(issue: dict[str, Any]) -> list[str]:
    return [str(label.get("name", "")) for label in issue.get("labels") or []]
