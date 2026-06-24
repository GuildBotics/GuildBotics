import re
from logging import Logger
from typing import Any, ClassVar, cast

from httpx import AsyncClient

from guildbotics.entities import Person, Task, Team
from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.integrations.github.github_utils import (
    create_github_client,
    get_author_type,
    get_github_username,
    get_person_name,
    get_proxy_agent_signature,
    is_proxy_agent,
)
from guildbotics.integrations.ticket_manager import TicketManager
from guildbotics.intelligences.common import Labels
from guildbotics.utils.i18n_tool import t

HTTP_BAD_REQUEST = 400


class GitHubTicketManager(TicketManager):
    """GitHub Projects V2 ticket manager using GraphQL and REST APIs."""

    FIELD_AGENT: ClassVar[str] = "Agent"
    FIELD_DUE_DATE: ClassVar[str] = "Due Date"
    FIELD_PRIORITY: ClassVar[str] = "Priority"
    LANE_READY: ClassVar[str] = "ready"
    LANE_DONE: ClassVar[str] = "done"
    LANE_WORKING: ClassVar[str] = "working"
    DEFAULT_LANE_MAP: ClassVar[dict[str, str]] = {
        LANE_READY: "Todo",
        LANE_DONE: "Done",
        LANE_WORKING: "In Progress",
    }

    def __init__(self, logger: Logger, person: Person, team: Team):
        """
        Initialize GitHubTicketManager with authentication and project settings.

        Args:
            person (Person): The user performing operations.
            team (Team): The team whose GitHub project will be used.
        """
        super().__init__(logger, person, team)
        config = team.project.get_service_config(Service.TICKET_MANAGER)
        self.base_url = str(config.get("base_url", "https://api.github.com"))
        self.owner = config["owner"]
        self.project_id = str(config["project_id"])
        self.url = str(config["url"])
        self.client: AsyncClient | None = None
        self.username = get_github_username(person, strict=True)
        self._username_lower = self.username.lower() if self.username else ""
        self._mention_token = f"⚙{self.person.person_id}"

        self.lane_map = self._load_lane_map(cast(dict | None, config.get("lane_map")))

        # caches populated in get_board()
        self._project_node_id: str | None = None
        self._status_field_id: str | None = None
        self.columns: dict[str, str] = {}  # Task status -> option_id
        self._status_positions: dict[str, int] = {}  # Status option name -> position
        self.custom_fields: dict[str, dict[str, Any]] = {}  # field_name -> field_info
        self.role_usernames: dict[
            str, list[str]
        ] = {}  # role_name -> list of user_node_id

        #: Custom field definitions
        agents = []
        for member in team.members:
            if member.person_type not in ["", "human"]:
                agents.append(
                    {
                        "name": get_proxy_agent_signature(member),
                        "description": member.name,
                    }
                )

        self._custom_field_definitions: dict[str, dict] = {
            GitHubTicketManager.FIELD_AGENT: {
                "dataType": "SINGLE_SELECT",
                "options": agents,
            },
        }

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

    async def login(self) -> AsyncClient:
        """Authenticate and create an HTTPX AsyncClient."""
        if not self.client:
            self.client = await create_github_client(self.person, self.base_url)
        return self.client

    # --------------------------------------------------------------------- #
    #   GraphQL helpers                                                     #
    # --------------------------------------------------------------------- #

    async def _graphql(self, query: str, variables: dict) -> dict:
        """Send a GraphQL request and return JSON `data`."""
        client = await self.login()
        resp = await client.post(
            "/graphql",
            json={"query": query, "variables": variables},
        )
        resp.raise_for_status()
        payload = resp.json()
        if "errors" in payload:
            raise RuntimeError(payload["errors"])
        return payload["data"]

    async def _project_node(self) -> str:
        """Return the node-ID of the configured Project V2."""
        if self._project_node_id:
            return self._project_node_id

        # Determine if owner is an organization or user based on self.url
        is_org = "/orgs/" in self.url
        query_type = "organization" if is_org else "user"

        query = f"""
        query($owner:String!, $num:Int!){{
            {query_type}(login:$owner){{
                projectV2(number:$num){{ id }}
            }}
        }}
        """
        data = await self._graphql(
            query,
            {"owner": self.owner, "num": int(self.project_id)},
        )
        proj = data[query_type]["projectV2"]
        if not proj:
            raise RuntimeError(
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
            data = await self._graphql(
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

        raise RuntimeError("Status field not found (after paginating all fields)")

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
            resp = await self._graphql(query, {"login": username})
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
        agent_field = fields.get(GitHubTicketManager.FIELD_AGENT, {})
        options = agent_field.get("options", {})
        return list(options) if isinstance(options, dict) else []

    def _desired_agent_options(self) -> list[dict[str, str]]:
        """The Agent options expected from configured non-human members."""
        config = self._custom_field_definitions[GitHubTicketManager.FIELD_AGENT]
        return list(config.get("options", []))

    async def get_agent_field_state(self) -> dict[str, Any]:
        """Return the current state of the project's ``Agent`` custom field.

        Read-only. ``options`` are the members currently registered as field
        options; ``missing`` are configured non-human members not yet registered
        (what :meth:`sync_agent_field` would add). Each entry is
        ``{"name": <signature>, "description": <member name>}``.
        """
        self.custom_fields = {}  # force a fresh read
        fields = await self._get_custom_fields()
        agent_field = fields.get(GitHubTicketManager.FIELD_AGENT)
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
        agent_field = fields.get(GitHubTicketManager.FIELD_AGENT)
        desired = self._desired_agent_options()

        if agent_field is None:
            await self._create_custom_field(
                GitHubTicketManager.FIELD_AGENT,
                self._custom_field_definitions[GitHubTicketManager.FIELD_AGENT],
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
        data = await self._graphql(query, {"id": field_id})
        node = data.get("node") or {}
        return [opt for opt in (node.get("options") or []) if opt]

    async def _update_single_select_options(
        self, field_id: str, options: list[dict[str, str]]
    ) -> None:
        """Overwrite a single-select field's option set (must include existing ids)."""
        mutation = """
        mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
          updateProjectV2Field(
            input: {fieldId: $field, singleSelectOptions: $options}
          ) {
            projectV2Field {
              ... on ProjectV2SingleSelectField { id }
            }
          }
        }
        """
        await self._graphql(mutation, {"field": field_id, "options": options})

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

    async def _get_issue_number(self, issue_node_id: str) -> int:
        """Convert a GraphQL issue node_id to its numeric REST issue number.

        Args:
            issue_node_id (str): The GraphQL global node ID of the issue.

        Returns:
            int: The numeric issue number in the repository.
        """
        query = """
        query($id: ID!) {
        node(id: $id) {
            ... on Issue {
            number
            }
        }
        }
        """
        resp = await self._graphql(query, {"id": issue_node_id})
        return resp["node"]["number"]

    def _get_issue_path(self, repo: str | None) -> str:
        """Return the REST API path for issues in the task's repository."""
        if not repo:
            raise ValueError("Task repository is required for issue operations.")
        return f"/repos/{self.owner}/{repo}/issues"

    def _to_task_field(self, name: str) -> str:
        return name.lower().replace(" ", "_")  # e.g., "Due Date" -> "due_date"

    def _issue_to_task(
        self,
        issue: dict,
        status: str,
        field_values: dict,
        assignee: str | None,
    ) -> Task:
        """
        Convert a GitHub Issue JSON to a Task instance.

        Args:
            issue (dict): The issue JSON payload.
            status (str): The current status.
            field_values (dict): Custom field values.
            assignee (str | None): The assignee of the task.

        Returns:
            Task: The converted Task object.
        """
        data = {
            "id": issue["id"],
            "number": issue.get("number"),
            "url": issue.get("url"),
            "title": issue["title"],
            "description": issue.get("body", "") or "",
            "status": status,
            "created_at": issue["createdAt"],
            "repository": f"{issue['repository']['name']}",
            "assignee": assignee,
        }

        # Extract custom field values
        for field_name, value in field_values.items():
            if field_name == GitHubTicketManager.FIELD_DUE_DATE and value:
                data[self._to_task_field(GitHubTicketManager.FIELD_DUE_DATE)] = value
            elif field_name == GitHubTicketManager.FIELD_PRIORITY and value is not None:
                data[self._to_task_field(GitHubTicketManager.FIELD_PRIORITY)] = int(
                    value
                )

        return Task(**data)

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
                custom_field_fragments.append(
                    """
                ... on ProjectV2ItemFieldSingleSelectValue {
                    field {
                        __typename
                        ... on ProjectV2SingleSelectField { id name }
                    }
                    name
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
            resp = await self._graphql(query, variables)
            payload = resp["node"]["items"]
            all_items.extend(payload["nodes"])
            if not payload["pageInfo"]["hasNextPage"]:
                break
            cursor = payload["pageInfo"]["endCursor"]
        return all_items

    def _is_my_response(self, username: str, content: str) -> bool:
        return get_author_type(self.person, username, content) == Message.ASSISTANT

    def _is_my_reaction(self, reaction: dict[str, Any]) -> bool:
        user = reaction.get("user") or {}
        login = str(user.get("login") or "").lower()
        return bool(login and login == self._username_lower)

    def _has_my_reaction(self, comment: dict[str, Any]) -> bool:
        reactions = comment.get("reactions") or {}
        for reaction in reactions.get("nodes") or []:
            if self._is_my_reaction(reaction):
                return True
        return False

    def _strip_signature_line(self, text: str, signature: str) -> str:
        """
        Remove the trailing signature line if it matches the provided signature.
        Args:
            text (str): The original text.
            signature (str): The signature line to remove.
        Returns:
            str: The text without the signature line.
        """
        stripped = text.rstrip()
        if not stripped:
            return ""
        lines = stripped.splitlines()
        if lines and lines[-1].strip() == signature:
            return "\n".join(lines[:-1])
        return text

    def _text_mentions_me(
        self, text: str | None, *, ignore_signature: bool = False
    ) -> bool:
        """
        Return True when the given text contains a mention of the current user.
        Args:
            text (str | None): The text to check for mentions.
            ignore_signature (bool): Whether to ignore the signature line.
        Returns:
            bool: True if the text mentions the user, False otherwise.
        """
        if not text:
            return False

        content = text
        if ignore_signature:
            content = self._strip_signature_line(content, self._mention_token)

        if self._mention_token and self._mention_token in content:
            return True

        if not is_proxy_agent(self.person) and self._username_lower:
            mention_re = (
                r"(^|[^A-Za-z0-9_])@"
                + re.escape(self._username_lower)
                + r"(?=$|[^A-Za-z0-9-])"
            )
            if re.search(mention_re, text, flags=re.IGNORECASE):
                return True

        return False

    async def _load_issue_comments(
        self, client: AsyncClient, task: Task, issue_number: int
    ) -> tuple[list[Message], bool]:
        comments_resp = await client.get(
            f"{self._get_issue_path(task.repository)}/{issue_number}/comments"
        )
        comments_data = comments_resp.json()
        comments_data.sort(key=lambda c: c.get("created_at") or "")

        comments = []
        mention_pending = self._text_mentions_me(task.description)
        for c in comments_data:
            author_type = get_author_type(self.person, c["user"]["login"], c["body"])
            author = (
                get_person_name(self.team.members, c["user"]["login"], c["body"])
                or author_type
            )
            comments.append(
                Message(
                    content=c["body"],
                    author=author,
                    author_type=author_type,
                    timestamp=c["created_at"],
                )
            )
            if self._text_mentions_me(c.get("body"), ignore_signature=True):
                mention_pending = True
            if author_type == Message.ASSISTANT:
                mention_pending = False

        return comments, mention_pending

    def _parse_pull_request_url(self, url: str) -> tuple[str, str, int] | None:
        match = re.search(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
        if not match:
            return None
        owner, repo, number = match.groups()
        return owner, repo, int(number)

    async def _get_pull_request_from_url(self, url: str) -> dict[str, Any] | None:
        parsed = self._parse_pull_request_url(url)
        if parsed is None:
            return None
        owner, repo, number = parsed
        client = await self.login()
        resp = await client.get(f"/repos/{owner}/{repo}/pulls/{number}")
        if resp.status_code >= HTTP_BAD_REQUEST:
            return None
        pull = resp.json()
        state = "merged" if pull.get("merged_at") else pull.get("state")
        return {
            "url": pull.get("html_url", url),
            "owner": owner,
            "repo": repo,
            "number": number,
            "state": state,
            "updated_at": pull.get("updated_at") or "",
        }

    async def _get_related_pull_requests(
        self, task: Task, issue_number: int
    ) -> list[dict[str, Any]]:
        client = await self.login()
        repo = task.repository
        resp = await client.get(
            f"/repos/{self.owner}/{repo}/issues/{issue_number}/timeline",
            headers={"Accept": "application/vnd.github+json"},
        )
        if resp.status_code >= HTTP_BAD_REQUEST:
            return []

        urls: list[str] = []
        for event in resp.json():
            source = event.get("source", {})
            issue = source.get("issue", {}) if isinstance(source, dict) else {}
            if issue.get("pull_request") and issue.get("html_url"):
                urls.append(issue["html_url"])

        pulls = []
        for url in dict.fromkeys(urls):
            pull = await self._get_pull_request_from_url(url)
            if pull:
                pulls.append(pull)
        return pulls

    async def _select_related_pull_request(
        self, task: Task, issue_number: int
    ) -> dict[str, Any] | None:
        pulls = await self._get_related_pull_requests(task, issue_number)
        if not pulls:
            return None

        open_pulls = [pull for pull in pulls if pull.get("state") == "open"]
        candidates = open_pulls or pulls
        return max(candidates, key=lambda pull: str(pull.get("updated_at") or ""))

    async def _has_unhandled_pull_request_review(self, pull: dict[str, Any]) -> bool:
        review_threads = await self._get_pull_request_review_threads(pull)
        for thread in review_threads:
            if await self._is_unhandled_review_thread(thread):
                return True
        return False

    async def _get_pull_request_review_threads(
        self, pull: dict[str, Any]
    ) -> list[dict[str, Any]]:
        owner = pull["owner"]
        repo = pull["repo"]
        number = pull["number"]
        query = """
        query($owner: String!, $repo: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 50, after: $after) {
                nodes {
                  isResolved
                  comments(last: 1) {
                    nodes {
                      id
                      body
                      createdAt
                      author { login }
                    }
                  }
                }
                pageInfo {
                  endCursor
                  hasNextPage
                }
              }
            }
          }
        }
        """

        threads: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = await self._graphql(
                query,
                {
                    "owner": owner,
                    "repo": repo,
                    "number": number,
                    "after": after,
                },
            )

            pull_request = (data.get("repository") or {}).get("pullRequest")
            if not pull_request:
                raise RuntimeError(
                    f"Pull request review threads unavailable for {owner}/{repo}#{number}"
                )
            review_threads = pull_request["reviewThreads"]
            threads.extend(review_threads.get("nodes") or [])
            page_info = review_threads["pageInfo"]
            if not page_info["hasNextPage"]:
                return threads
            after = page_info["endCursor"]

    async def _get_comment_reactions(self, comment_id: str) -> list[dict[str, Any]]:
        query = """
        query($id: ID!, $after: String) {
          node(id: $id) {
            ... on PullRequestReviewComment {
              reactions(first: 100, after: $after) {
                nodes {
                  content
                  user { login }
                }
                pageInfo {
                  endCursor
                  hasNextPage
                }
              }
            }
          }
        }
        """

        reactions: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = await self._graphql(query, {"id": comment_id, "after": after})
            node = data.get("node")
            if not node:
                raise RuntimeError(
                    f"Pull request review comment reactions unavailable for {comment_id}"
                )
            reaction_connection = node["reactions"]
            reactions.extend(reaction_connection.get("nodes") or [])
            page_info = reaction_connection["pageInfo"]
            if not page_info["hasNextPage"]:
                return reactions
            after = page_info["endCursor"]

    async def _comment_has_my_reaction(self, comment: dict[str, Any]) -> bool:
        reactions = comment.get("reactions")
        if reactions is not None:
            return self._has_my_reaction(comment)
        comment_id = str(comment.get("id") or "")
        if not comment_id:
            return False
        return any(
            self._is_my_reaction(r)
            for r in await self._get_comment_reactions(comment_id)
        )

    async def _is_unhandled_review_thread(self, thread: dict[str, Any]) -> bool:
        if thread.get("isResolved"):
            return False

        comments = (thread.get("comments") or {}).get("nodes") or []
        if not comments:
            return False

        comments = sorted(comments, key=lambda c: c.get("createdAt") or "")
        last_comment = comments[-1]
        author = last_comment.get("author") or {}
        login = str(author.get("login") or "")
        body = str(last_comment.get("body") or "")
        if login and self._is_my_response(login, body):
            return False
        return not await self._comment_has_my_reaction(last_comment)

    def _build_project_tasks(
        self, all_items: list[dict]
    ) -> tuple[list[Task], dict[str, dict[str, Any]]]:
        tasks: list[Task] = []
        task_metadata: dict[str, dict[str, Any]] = {}
        for it in all_items:
            status: str | None = None
            field_values = {}

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
            is_assigned = False
            assignee: str | None = None

            # Check assignees
            if not is_proxy_agent(self.person) and assignees:
                for a in assignees:
                    if a.get("login") == self._username_lower:
                        is_assigned = True
                        assignee = self.person.person_id
                        break

            # Check custom fields if not assigned via assignees
            if not is_assigned:
                for field_name, value in field_values.items():
                    if (
                        field_name == GitHubTicketManager.FIELD_AGENT
                        and value == self._mention_token
                    ):
                        is_assigned = True
                        assignee = self.person.person_id
                        break

            task = self._issue_to_task(issue, status, field_values, assignee)
            tasks.append(task)
            assert task.id, "Task ID must be set"
            task_metadata[task.id] = {
                "issue_number": issue["number"],
                "assigned": is_assigned,
            }

        tasks = sorted(tasks)
        return tasks, task_metadata

    async def get_ticket(self, column_name: str, all_items: list[dict]) -> Task | None:
        """
        Retrieve a ticket from a specific internal lane.

        get_task_to_work_on() is the primary entrypoint for the simplified workflow;
        this method remains as a narrow compatibility helper for direct callers.
        """
        tasks, task_metadata = self._build_project_tasks(all_items)

        for task in tasks:
            if task.status != column_name:
                continue
            assert task.id, "Task ID must be set"
            selected = await self._select_actionable_task(task, task_metadata[task.id])
            if selected:
                return selected
        return None

    async def _select_actionable_task(
        self, task: Task, metadata: dict[str, Any]
    ) -> Task | None:
        issue_number = int(metadata["issue_number"])
        assigned = bool(metadata["assigned"])
        if not assigned:
            return None

        client = await self.login()
        comments, mention_pending = await self._load_issue_comments(
            client, task, issue_number
        )
        task.comments = sorted(comments, key=lambda m: m.timestamp)
        last_comment_is_mine = (
            bool(task.comments) and task.comments[-1].author_type == Message.ASSISTANT
        )

        if task.status == Task.READY:
            if last_comment_is_mine and not mention_pending:
                return None
            task.trigger_reason = "ready_lane"
            return task

        pull = await self._select_related_pull_request(task, issue_number)
        if pull:
            if pull.get("state") == "merged":
                await self.move_ticket(task, Task.DONE)
                return None
            if pull.get("state") != "open":
                return None
            if await self._has_unhandled_pull_request_review(pull):
                task.pull_request_url = str(pull["url"])
                task.trigger_reason = "pull_request_review"
                return task
            return None

        if task.comments and task.comments[-1].author_type != Message.ASSISTANT:
            task.trigger_reason = "issue_comment"
            return task
        if not task.comments:
            task.trigger_reason = "issue_mention" if mention_pending else "working_lane"
            return task
        return None

    async def get_task_to_work_on(self) -> Task | None:
        """
        Retrieve a ticket that the person can work on.

        Returns:
            Task | None: The next available Task or None.
        """

        all_items = await self.get_all_tickets()
        tasks, task_metadata = self._build_project_tasks(all_items)

        ready_tasks = [task for task in tasks if task.status == Task.READY]
        working_tasks = [task for task in tasks if task.status != Task.READY]
        for task in [*ready_tasks, *working_tasks]:
            assert task.id, "Task ID must be set"
            selected = await self._select_actionable_task(task, task_metadata[task.id])
            if selected:
                return selected
        return None

    async def _get_project_item_id(self, issue_node_id: str) -> str:

        project_id = await self._project_node()

        mutation = """
        mutation($proj: ID!, $content: ID!) {
        addProjectV2ItemById(
            input:{ projectId: $proj, contentId: $content }
        ) {
            item { id }
        }
        }

        """

        data = await self._graphql(
            mutation, {"proj": project_id, "content": issue_node_id}
        )
        return data["addProjectV2ItemById"]["item"]["id"]

    async def move_ticket(self, task: Task, new_status: str) -> bool:
        """
        Move an existing ticket to a new Status column.

        Args:
            task (Task): The Task to move.
            new_status (str): The target column name.

        Returns:
            bool: True if the Status column was updated, False when the target
                lane has no resolvable option (e.g. the working lane is not
                configured or absent from the board).
        """
        proj_node = await self._project_node()
        # update ProjectV2 item field value
        assert task.id, "Task ID must be set before moving"
        item_id = await self._get_project_item_id(task.id)
        status_field_id, _ = await self._get_status_field()
        option_id = await self.get_column_id(new_status)
        if not option_id:
            return False

        mutation = """
        mutation($proj:ID!,$item:ID!,$field:ID!,$opt:String!){
        updateProjectV2ItemFieldValue(
            input:{
            projectId:$proj,
            itemId:$item,
            fieldId:$field,
            value:{ singleSelectOptionId:$opt }
            }
        ){ projectV2Item { id } }
        }
        """
        await self._graphql(
            mutation,
            {
                "proj": proj_node,
                "item": item_id,
                "field": status_field_id,
                "opt": option_id,
            },
        )
        return True

    async def add_comment_to_ticket(self, task: Task, comment: str) -> None:
        """
        Add a comment to an existing ticket using REST.

        Args:
            task (Task): The task to comment on.
            comment (str): The comment content.
        """
        client = await self.login()
        assert task.id, "Task ID must be set before commenting"
        issue_number = await self._get_issue_number(task.id)
        # Fetch issue to determine the author for mention
        issue_resp = await client.get(
            f"{self._get_issue_path(task.repository)}/{issue_number}"
        )
        issue_data = issue_resp.json()
        author_login = (issue_data.get("user") or {}).get("login")

        # Prepend mention to the issue author unless we are the author.
        # Avoid adding only when the body already mentions the issue author
        # (case-insensitive) to prevent duplicates.
        if author_login and author_login != self._username_lower:
            # Explicitly check if the issue author is already mentioned.
            # GitHub usernames are case-insensitive, so we use re.IGNORECASE.
            author_mention_re = (
                r"(^|[^A-Za-z0-9_])@" + re.escape(author_login) + r"(?=$|[^A-Za-z0-9-])"
            )
            has_author_mention = bool(
                re.search(author_mention_re, comment, flags=re.IGNORECASE)
            )

            if not has_author_mention:
                comment = f"@{author_login}\n\n{comment}"
        if is_proxy_agent(self.person):
            comment = f"{comment}\n\n{self._mention_token}"
        await client.post(
            f"{self._get_issue_path(task.repository)}/{issue_number}/comments",
            json={"body": comment},
        )

    async def get_ticket_url(self, task: Task, markdown: bool = True) -> str:
        """
        Get the URL for a specific ticket.

        Promoted issues link to their issue page; draft tickets are not bound to a
        repository and have no standalone issue URL, so they link to the Project
        board instead.

        Args:
            task (Task): The Task instance.
            markdown (bool): Wrap in Markdown link if True.

        Returns:
            str: The ticket URL.
        """
        assert task.id, "Task ID must be set before getting URL"
        if not task.repository:
            url = self.url
        else:
            issue_id = await self._get_issue_number(task.id)
            url = f"https://github.com/{self.owner}/{task.repository}/issues/{issue_id}"
        return f"[{task.title}]({url})" if markdown else url

    async def update_ticket(self, task: Task) -> None:
        """
        Update an existing ticket's custom fields.

        Args:
            task (Task): The Task to update.
        """
        await self.ensure_custom_fields()

        # Get project item ID
        assert task.id, "Task ID must be set before updating"
        item_id = await self._get_project_item_id(task.id)

        # Update custom field values
        field_values = {}
        for field_name in self._custom_field_definitions:
            value = await self._get_field_value_for_task(task, field_name)
            if value:
                field_values[field_name] = value

        if field_values:
            await self._set_multiple_custom_field_values(item_id, field_values)

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
            data = await self._graphql(
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

        mutation = """
        mutation($proj:ID!, $name:String!, $dataType:ProjectV2CustomFieldType!, $options:[ProjectV2SingleSelectFieldOptionInput!]) {
            createProjectV2Field(input: {
                projectId: $proj,
                name: $name,
                dataType: $dataType,
                singleSelectOptions: $options
            }) {
                projectV2Field {
                    ... on ProjectV2Field {
                        id
                        name
                        dataType
                    }
                    ... on ProjectV2SingleSelectField {
                        id
                        name
                        dataType
                        options { name description color }
                    }
                }
            }
        }
        """

        for opt in options:
            if "color" not in opt:
                opt["color"] = "GRAY"

        variables = {
            "proj": proj_id,
            "name": field_name,
            "dataType": field_config["dataType"],
            "options": options if options else None,
        }

        data = await self._graphql(mutation, variables)
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
                        "integrations.github.github_ticket_manager.add_custom_field_options",
                        field=field_name,
                        options=Labels(missing_options),
                    )
                    self.logger.warning(message)

        # If any fields were created, refresh the local cache to include option IDs
        if created_any:
            # Clear cache and re-fetch
            self.custom_fields = {}
            await self._get_custom_fields()

    async def _get_field_value_for_task(self, task: Task, field_name: str) -> Any:
        """
        Get the appropriate field value for a task based on field type.

        Args:
            task: The task object.
            field_name: The field name.

        Returns:
            The field value formatted for GraphQL.
        """
        if field_name == GitHubTicketManager.FIELD_DUE_DATE:
            if task.due_date:
                return {"date": task.due_date.isoformat().split("T")[0]}

        elif (
            field_name == GitHubTicketManager.FIELD_PRIORITY
            and task.priority is not None
        ):
            return {"number": float(task.priority)}

        return None

    async def _set_multiple_custom_field_values(
        self, item_id: str, field_values: dict[str, Any]
    ) -> None:
        """
        Set multiple custom field values for a project item in a single GraphQL request.

        Args:
            item_id: The project item ID.
            field_values: Dictionary mapping field names to their values.
        """
        if not field_values:
            return

        proj_id = await self._project_node()

        # Build multiple mutations in a single request
        mutations = []
        variables = {"proj": proj_id, "item": item_id}

        for i, (field_name, value) in enumerate(field_values.items()):
            if value is None:
                continue

            field_id = self.custom_fields[field_name]["id"]
            mutation_name = f"update{i}"
            variables[f"field{i}"] = field_id
            variables[f"value{i}"] = value

            mutations.append(
                f"""
            {mutation_name}: updateProjectV2ItemFieldValue(input: {{
                projectId: $proj,
                itemId: $item,
                fieldId: $field{i},
                value: $value{i}
            }}) {{
                projectV2Item {{ id }}
            }}
            """
            )

        if not mutations:
            return

        query = f"""
        mutation($proj:ID!, $item:ID!, {", ".join(f"$field{i}:ID!, $value{i}:ProjectV2FieldValue!" for i in range(len(mutations)))}) {{
            {"".join(mutations)}
        }}
        """

        await self._graphql(query, variables)
