from logging import Logger
import re
from typing import Any, ClassVar, cast

from httpx import AsyncClient

from guildbotics.entities import Person, Task, Team
from guildbotics.entities.message import Message
from guildbotics.entities.team import Service
from guildbotics.integrations.github.github_code_hosting_service import (
    GitHubCodeHostingService,
)
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
from guildbotics.modes.mode_base import ModeBase
from guildbotics.utils.i18n_tool import t


class InvalidStatusColumnsError(RuntimeError):
    def __init__(self, missing: set[str], extra: set[str]) -> None:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if extra:
            details.append(f"extra={sorted(extra)}")
        super().__init__("Status columns mismatch: " + ", ".join(details))


class GitHubTicketManager(TicketManager):
    """GitHub Projects V2 ticket manager using GraphQL and REST APIs."""

    FIELD_AGENT: ClassVar[str] = "Agent"
    FIELD_DUE_DATE: ClassVar[str] = "Due Date"
    FIELD_PRIORITY: ClassVar[str] = "Priority"
    FIELD_MODE: ClassVar[str] = "Mode"
    FIELD_ROLE: ClassVar[str] = "Role"
    FIELD_OWNER: ClassVar[str] = "Owner"

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
        self.default_repo = team.project.get_default_repository().name
        self.project_id = str(config["project_id"])
        self.url = str(config["url"])
        self.client: AsyncClient | None = None
        self.username = get_github_username(person, strict=True)

        self.default_status_map = {
            Task.NEW: "New",
            Task.READY: "Ready",
            Task.IN_PROGRESS: "In Progress",
            Task.IN_REVIEW: "In Review",
            Task.RETROSPECTIVE: "Retrospective",
            Task.DONE: "Done",
        }
        status_map: dict = cast(dict, config.get("status_map", self.default_status_map))
        self.status_map = {
            status_map.get(k, v): k for k, v in self.default_status_map.items()
        }

        # caches populated in get_board()
        self._project_node_id: str | None = None
        self._status_field_id: str | None = None
        self.columns: dict[str, str] = {}  # column_name -> option_id
        self.custom_fields: dict[str, dict[str, Any]] = {}  # field_name -> field_info
        self.role_usernames: dict[str, list[str]] = (
            {}
        )  # role_name -> list of user_node_id

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
            GitHubTicketManager.FIELD_MODE: {
                "dataType": "SINGLE_SELECT",
                "options": [
                    {
                        "name": name,
                        "description": mode or "",
                    }
                    for name, mode in ModeBase.get_available_modes(self.team).items()
                ],
            },
            GitHubTicketManager.FIELD_ROLE: {
                "dataType": "SINGLE_SELECT",
                "options": [
                    {
                        "name": id,
                        "description": role.summary,
                    }
                    for id, role in Person.DEFINED_ROLES.items()
                ],
            },
            GitHubTicketManager.FIELD_AGENT: {
                "dataType": "SINGLE_SELECT",
                "options": agents,
            },
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
        """Return the node‑ID of the configured Project V2."""
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
        return len(self.columns) == len(self.status_map)

    async def _sync_status_columns(self) -> None:
        if self._cache_is_complete():
            return

        _, columns_local = await self._get_status_field()

        current_set = set(columns_local)
        self.columns = {}
        for n in current_set:
            k = self.status_map.get(n, None)
            if k:
                self.columns[k] = columns_local[n]["id"]

    async def get_statuses(self) -> list[str]:
        """
        Return the list of Status column names.

        Ensures the Status column set is synced before fetching.
        """
        _, columns_local = await self._get_status_field()
        return list(columns_local)

    async def get_column_id(self, column_name: str) -> str | None:
        """
        Return the option‑ID corresponding to *column_name*.

        Ensures the Status column set is synced before fetching.
        """
        await self._sync_status_columns()
        return self.columns.get(column_name, None)

    def _create_role_usernames(self) -> dict[str, list[str]]:
        """
        Create a mapping of roles to their corresponding GitHub user node IDs.
        """
        role_usernames: dict[str, list[str]] = {}
        for role, members in self.team.get_role_members().items():
            role_usernames[role] = []
            for member in members:
                user_name = get_github_username(member)
                if not user_name:
                    continue
                role_usernames[role].append(user_name)

        return role_usernames

    async def _get_issue_node_id(self, repo: str | None, issue_number: int) -> str:
        """Convert a REST issue number to the GraphQL global node_id.

        Args:
            repo (str): The repository name.
            issue_number (int): The numeric issue identifier in the repo.

        Returns:
            str: The GraphQL global node ID of the issue.
        """
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            issue(number: $number) {
              id
            }
          }
        }
        """
        r = repo if repo else self.default_repo
        variables = {"owner": self.owner, "repo": r, "number": issue_number}
        resp = await self._graphql(query, variables)
        return resp["repository"]["issue"]["id"]

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
        """Return the REST API path for issues in the configured repository."""
        r = repo if repo else self.default_repo
        return f"/repos/{self.owner}/{r}/issues"

    async def create_tickets(self, tasks: list[Task]) -> None:
        """
        Create GitHub issues for the given tasks and assign columns, labels, roles.

        Args:
            tasks (list[Task]): List of Task instances to create.
        """
        await self._sync_status_columns()
        await self.ensure_custom_fields()

        client = await self.login()
        proj_node = await self._project_node()

        for task in sorted(tasks):
            payload: dict = {"title": task.title, "body": task.description or ""}

            # create issue
            issue_path = self._get_issue_path(task.repository)
            resp = await client.post(issue_path, json=payload)
            issue = resp.json()
            num = issue["number"]

            # add to project
            node_id = await self._get_issue_node_id(task.repository, num)
            task.id = node_id
            mutation = """
            mutation($proj: ID!, $cid: ID!) {
                addProjectV2ItemById(input: {projectId: $proj, contentId: $cid}) {
                    item { id }
                }
            }
            """
            data = await self._graphql(
                mutation,
                {"proj": proj_node, "cid": node_id},
            )
            item_id = data["addProjectV2ItemById"]["item"]["id"]

            # Set Status
            status_field_id, _ = await self._get_status_field()
            new_status_option_id = await self.get_column_id(task.status)
            set_status_mutation = """
            mutation($proj:ID!, $item:ID!, $field:ID!, $opt:String!){
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
                set_status_mutation,
                {
                    "proj": proj_node,
                    "item": item_id,
                    "field": status_field_id,
                    "opt": new_status_option_id,
                },
            )

            # Set custom field values
            field_values = {}
            for field_name in self._custom_field_definitions.keys():
                value = await self._get_field_value_for_task(task, field_name)
                if value:
                    field_values[field_name] = value

            if field_values:
                await self._set_multiple_custom_field_values(item_id, field_values)

    def _to_task_field(self, name: str) -> str:
        return name.lower().replace(" ", "_")  # e.g., "Due Date" -> "due_date"

    def _issue_to_task(self, issue: dict, status: str, field_values: dict) -> Task:
        """
        Convert a GitHub Issue JSON to a Task instance.

        Args:
            issue (dict): The issue JSON payload.
            status (str): The current status.
            field_values (dict): Custom field values.

        Returns:
            Task: The converted Task object.
        """
        data = {
            "id": issue["id"],
            "title": issue["title"],
            "description": issue.get("body", "") or "",
            "status": status,
            "created_at": issue["createdAt"],
            "repository": f"{issue["repository"]["name"]}",
        }

        # Extract custom field values
        for field_name, value in field_values.items():
            if field_name == GitHubTicketManager.FIELD_DUE_DATE and value:
                data[self._to_task_field(GitHubTicketManager.FIELD_DUE_DATE)] = value
            elif field_name == GitHubTicketManager.FIELD_PRIORITY and value is not None:
                data[self._to_task_field(GitHubTicketManager.FIELD_PRIORITY)] = int(
                    value
                )
            elif field_name == GitHubTicketManager.FIELD_MODE and value:
                data[self._to_task_field(GitHubTicketManager.FIELD_MODE)] = value
            elif field_name == GitHubTicketManager.FIELD_ROLE and value:
                data[self._to_task_field(GitHubTicketManager.FIELD_ROLE)] = value
            elif field_name == GitHubTicketManager.FIELD_OWNER and value:
                data[self._to_task_field(GitHubTicketManager.FIELD_OWNER)] = value

        return Task(**data)

    async def get_all_tickets(self) -> list[dict]:
        """Retrieve all tickets from the GitHub Projects V2 board."""
        await self._sync_status_columns()
        await self.ensure_custom_fields()
        proj_node = await self._project_node()

        # Build GraphQL fragments for custom fields
        custom_field_fragments = []
        for field_name, field_info in self.custom_fields.items():
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
                          {''.join(custom_field_fragments)}
                        }}
                      }}
                      content {{
                        ... on Issue {{
                          id
                          number
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

    async def has_pr_review_comments(self, task: Task) -> bool:
        """
        Check if a pull request has review comments.

        Args:
            task (Task): The Task instance representing the pull request.

        Returns:
            bool: True if there are review comments, False otherwise.
        """
        if task.status != Task.IN_REVIEW:
            return False

        url = task.find_output_title_and_url_from_comments(strict=False)[1]
        if not url:
            return False

        hosting_service = GitHubCodeHostingService(
            self.person, self.team, task.repository
        )
        # Check PR URL against GitHub web base, independent of clone scheme
        if not url.startswith("https://github.com/"):
            return False  # Not a PR URL

        comments = await hosting_service.get_pull_request_comments(url)
        return not comments.is_replied

    async def get_ticket(self, column_name: str, all_items: list[dict]) -> Task | None:
        """
        Retrieve a ticket from a specific column by name, fetching all pages of items.

        Args:
            column_name (str): The column to pull tasks from.

        Returns:
            Task | None: A Task or None if no task available.
        """

        client = await self.login()

        # process each item in the specified column
        tasks: list[Task] = []
        issue_number_map = {}
        for it in all_items:
            # extract status and custom field values
            status = None
            field_values = {}

            for fv in it["fieldValues"]["nodes"]:
                field = fv.get("field", {})
                field_name = field.get("name")
                field_id = field.get("id")

                if field_name == "Status":
                    status = self.status_map.get(fv["name"])
                elif field_id in [info["id"] for info in self.custom_fields.values()]:
                    # Find field name by ID
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

            if status != column_name:
                continue

            issue = it["content"]
            if not issue:
                continue
            assignees = issue.get("assignees", {}).get("nodes", [])
            is_assigned = False

            # Check assignees
            if not is_proxy_agent(self.person) and assignees:
                for assignee in assignees:
                    if assignee.get("login") == self.username:
                        is_assigned = True
                        break

            # Check custom fields if not assigned via assignees
            if not is_assigned:
                signature = get_proxy_agent_signature(self.person)
                for field_name, value in field_values.items():
                    if (
                        field_name == GitHubTicketManager.FIELD_AGENT
                        and value == signature
                    ):
                        is_assigned = True
                        break

            if not is_assigned:
                continue

            issue_number_map[issue["id"]] = issue["number"]
            # convert issue dict to Task
            tasks.append(self._issue_to_task(issue, status, field_values))

        tasks = sorted(tasks)

        for task in tasks:
            # fetch comments via REST and attach to Task
            issue_number = issue_number_map.get(task.id)
            comments_resp = await client.get(
                f"{self._get_issue_path(task.repository)}/{issue_number}/comments"
            )
            comments_data = comments_resp.json()
            comments = []
            for c in comments_data:
                author_type = get_author_type(
                    self.person, c["user"]["login"], c["body"]
                )
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
            if comments:
                # sort comments by creation timestamp ascending
                comments.sort(key=lambda m: m.timestamp)
                task.comments = comments
                # If the last comment was made by the person, we skip this task.
                author_type = comments[-1].author_type
                if (
                    author_type == Message.ASSISTANT
                    and not await self.has_pr_review_comments(task)
                ):
                    continue

            return task
        return None

    async def get_task_to_work_on(self) -> Task | None:
        """
        Retrieve a ticket that the person can work on.

        Returns:
            Task | None: The next available Task or None.
        """

        all_items = await self.get_all_tickets()

        all_cols = [Task.RETROSPECTIVE, Task.IN_REVIEW, Task.IN_PROGRESS, Task.READY]
        available_cols = set(self.columns.keys())

        for col in all_cols:
            if col in available_cols:
                t = await self.get_ticket(col, all_items)
                if t:
                    return t
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

    async def move_ticket(self, task: Task, new_status: str) -> None:
        """
        Move an existing ticket to a new Status column.

        Args:
            task (Task): The Task to move.
            new_status (str): The target column name.
        """
        proj_node = await self._project_node()
        # update ProjectV2 item field value
        assert task.id, "Task ID must be set before moving"
        item_id = await self._get_project_item_id(task.id)
        status_field_id, _ = await self._get_status_field()
        option_id = await self.get_column_id(new_status)
        if not option_id:
            return

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
        if author_login and author_login != self.username:
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
            comment = f"{comment}\n\n{get_proxy_agent_signature(self.person)}"
        await client.post(
            f"{self._get_issue_path(task.repository)}/{issue_number}/comments",
            json={"body": comment},
        )

    async def get_ticket_url(self, task: Task, markdown: bool = True) -> str:
        """
        Get the URL for a specific issue.

        Args:
            task (Task): The Task instance.
            markdown (bool): Wrap in Markdown link if True.

        Returns:
            str: The issue URL.
        """
        assert task.id, "Task ID must be set before getting URL"
        issue_id = await self._get_issue_number(task.id)
        repo = task.repository or self.default_repo
        url = f"https://github.com/{self.owner}/{repo}/issues/{issue_id}"
        return f"[{task.title}]({url})" if markdown else url

    def get_board_url(self) -> str:
        """
        Get the URL for the ProjectV2 board.

        Returns:
            str: The board URL.
        """
        return self.url

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
        for field_name in self._custom_field_definitions.keys():
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
            if not "color" in opt:
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
            else:
                # For existing fields, check and update options if necessary
                if (
                    field_config.get("dataType") == "SINGLE_SELECT"
                    and "options" in field_config
                ):
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
        field_info = self.custom_fields[field_name]

        if field_name == GitHubTicketManager.FIELD_DUE_DATE:
            if task.due_date:
                return {"date": task.due_date.isoformat().split("T")[0]}

        elif field_name == GitHubTicketManager.FIELD_PRIORITY:
            if task.priority is not None:
                return {"number": float(task.priority)}

        elif field_name in (
            GitHubTicketManager.FIELD_MODE,
            GitHubTicketManager.FIELD_ROLE,
        ):
            # Handle SINGLE_SELECT fields dynamically
            value_attr = (
                task.mode if field_name == GitHubTicketManager.FIELD_MODE else task.role
            )
            if value_attr:
                # Ensure options dict exists
                options = field_info.setdefault("options", {})
                # If the option does not exist yet, create it via GraphQL
                if value_attr not in options:
                    return None  # Skip if option not found
                return {"singleSelectOptionId": options[value_attr]}

        elif field_name == GitHubTicketManager.FIELD_OWNER:
            if task.owner:
                return {"text": task.owner}

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
        mutation($proj:ID!, $item:ID!, {', '.join(f'$field{i}:ID!, $value{i}:ProjectV2FieldValue!' for i in range(len(mutations)))}) {{
            {''.join(mutations)}
        }}
        """

        await self._graphql(query, variables)

    async def create_label(self, label: str) -> None:
        """
        Create a new label in the default repository if it does not already exist.

        Args:
            label (str): The name of the label to create.
        """
        client = await self.login()
        path = f"/repos/{self.owner}/{self.default_repo}/labels"

        # Check if the label already exists
        resp = await client.get(path)
        existing_labels = resp.json()
        for existing_label in existing_labels:
            if existing_label["name"] == label:
                return  # Label already exists, do nothing

        # Create the label if it doesn't exist
        payload = {"name": label}
        resp = await client.post(path, json=payload)
        resp.raise_for_status()
