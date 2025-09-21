import re

from httpx import AsyncClient

from guildbotics.entities.message import Message
from guildbotics.entities.team import Person, Service, Team
from guildbotics.integrations.code_hosting_service import (
    CodeHostingService,
    InlineComment,
    PullRequest,
    Reaction,
    ReviewComment,
    ReviewComments,
    get_author,
)
from guildbotics.integrations.github.github_utils import (
    create_github_client,
    get_author_type,
    get_person_name,
    get_proxy_agent_signature,
    is_proxy_agent,
)


class GitHubCodeHostingService(CodeHostingService):
    """GitHub pull request API code hosting service.

    Creates pull requests in GitHub using its REST API. Uses access token for authentication.
    """

    def __init__(self, person: Person, team: Team, repository: str | None = None):
        """Initialize the GitHubCodeHostingService.

        Args:
            person (Person): The person associated with the code hosting service.
            team (Team): The team associated with the code hosting service.
        """
        super().__init__(person, team)
        # Get GitHub-specific config
        config = team.project.get_service_config(Service.CODE_HOSTING_SERVICE)
        self.base_url = str(config.get("api_base_url", "https://api.github.com"))
        self.repo_base_url = str(config.get("repo_base_url", "https://github.com"))
        self.owner = str(config["owner"])
        self.repo = (
            repository if repository else team.project.get_default_repository().name
        )
        self.client: AsyncClient | None = None

        self.username = person.account_info["github_username"]

    async def get_client(self) -> AsyncClient:
        if self.client:
            return self.client

        self.client = await create_github_client(self.person, self.base_url)
        return self.client

    def to_issue_number(self, ticket_url: str) -> int | None:
        """Extract the issue or ticket number from a URL.

        Args:
            ticket_url (str): The URL of the issue or ticket.
        Returns:
            int | None: The extracted issue number, or None if not found.
        """
        match = re.search(r"/issues/(\d+)", ticket_url)
        if match:
            return int(match.group(1))
        return None

    async def create_pull_request(
        self, branch_name: str, title: str, description: str, ticket_url: str
    ) -> str:
        """Create a pull request in GitHub.

        Args:
            branch_name (str): The name of the source branch to merge.
            title (str): The title of the pull request.
            description (str): The description (body) of the pull request.
            ticket_url (str): The URL of the associated ticket or task.

        Returns:
            str: The URL of the created pull request.
        """
        # Endpoint for listing and creating pull requests
        endpoint = f"/repos/{self.owner}/{self.repo}/pulls"

        # Check for existing open PRs with the same head branch
        default_branch = await self.get_default_branch()
        params = {
            "head": f"{self.owner}:{branch_name}",
            "base": default_branch,
            "state": "open",
        }
        client = await self.get_client()
        resp = await client.get(endpoint, params=params)
        existing_prs = resp.json()
        if existing_prs:
            # Return the URL of the first existing pull request
            return existing_prs[0].get("html_url", "")

        issue_number = self.to_issue_number(ticket_url)
        if issue_number:
            description = f"{description}\n\nCloses #{issue_number}"

        # Create a new pull request
        payload: dict = {
            "title": title,
            "head": branch_name,
            "base": default_branch,
            "body": description,
        }
        if is_proxy_agent(self.person):
            payload["draft"] = True
        resp = await client.post(endpoint, json=payload)
        pr = resp.json()

        return pr.get("html_url", "")

    def get_pr_number_from_url(self, html_url: str) -> str:
        """Extract the pull request number from the HTML URL."""
        return html_url.rstrip("/").split("/")[-1]

    async def get_pull_request_comments(
        self, html_url: str, include_all_comments: bool = False
    ) -> ReviewComments:
        """Retrieve all issue and review comments for a pull request.

        Args:
            html_url (str): The HTML URL of the pull request.
            include_all_comments (bool): Whether to include all comments or only unresolved ones.

        Returns:
            list[dict]: A combined list of issue and review comment objects.
        """
        # Extract pull request number from the URL
        pr_number = self.get_pr_number_from_url(html_url)

        # Get authenticated HTTP client
        client = await self.get_client()

        # Fetch issue comments
        review_comments: list[ReviewComment] = []
        issue_comments_resp = await client.get(
            f"/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments"
        )
        _issue_comments: list[dict] = issue_comments_resp.json()
        for c in _issue_comments:
            reactions = await self._get_comment_reactions(
                client, "issues", int(c["id"])
            )
            review_comments.append(
                ReviewComment(
                    comment_id=c.get("id"),
                    body=c["body"],
                    author=self.get_author_name(c["user"]["login"], c["body"]),
                    created_at=c["created_at"],
                    is_reviewee=c["user"]["login"] == self.username,
                    is_checked=self.is_checked(reactions),
                )
            )

        reviews_resp = await client.get(
            f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/reviews"
        )
        _review_comments: list[dict] = reviews_resp.json()
        review_comments.extend(
            ReviewComment(
                comment_id=c.get("id"),
                body=c["body"],
                author=self.get_author_name(c["user"]["login"], c["body"]),
                created_at=c["submitted_at"],
                is_reviewee=c["user"]["login"] == self.username,
            )
            for c in _review_comments
            if c["body"]
        )

        # Fetch inline comments
        review_comments_resp = await client.get(
            f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/comments"
        )
        _inline_comments: list[dict] = review_comments_resp.json()
        inline_comments: list[InlineComment] = []
        for comment in _inline_comments:
            reactions = await self._get_comment_reactions(
                client, "pulls", int(comment["id"])
            )
            inline_comments.append(
                self.from_dict(comment, self.username, reactions=reactions)
            )

        return ReviewComments(
            review_comments=review_comments,
            inline_comments=inline_comments,
            include_all_comments=include_all_comments,
        )

    def get_author_name(self, username: str, comment_body: str) -> str:
        person_name = get_person_name(self.team.members, username, comment_body)
        author_type = get_author_type(self.person, username, comment_body)
        return get_author(person_name, author_type == Message.ASSISTANT)

    def is_checked(self, reactions: list[Reaction] | None) -> bool:
        """Check if the reviewee has reacted to the comment."""
        return any(self.username == reaction.username for reaction in (reactions or []))

    def from_dict(
        self, data: dict, reviewee: str, reactions: list[Reaction] | None = None
    ) -> InlineComment:
        """Create an InlineComment instance from a GitHub API response dict.

        Args:
            data (dict): A dict containing keys:
                - path (str)
                - line (int)
                - body (str)
                - id (int)
                - user (dict with "login" key)
                - created_at (str)

        Returns:
            InlineComment: Parsed inline comment model.
        """
        author = data["user"]["login"]
        is_reviewee = author == reviewee
        return InlineComment(
            path=data["path"],
            line=data.get("line", 0) or 0,
            body=data["body"],
            comment_id=data["id"],
            author=get_author(author, is_reviewee),
            created_at=data["created_at"],
            is_reviewee=is_reviewee,
            line_content=self.get_line_content(data),
            is_checked=self.is_checked(reactions),
        )

    async def _get_comment_reactions(
        self, client: AsyncClient, comment_type: str, comment_id: int
    ) -> list[Reaction]:
        """Fetch reactions for an issue or pull request comment and group by content with usernames."""
        resp = await client.get(
            f"/repos/{self.owner}/{self.repo}/{comment_type}/comments/{comment_id}/reactions"
        )
        items: list[dict] = resp.json()
        return self._group_reactions(items)

    def _group_reactions(self, items: list[dict]) -> list[Reaction]:
        """Group reaction payloads by username, aggregating contents."""
        by_user: dict[str, set[str]] = {}
        for it in items:
            content = str(it.get("content", ""))
            user = it.get("user", {})
            login = str(user.get("login", ""))
            if not content or not login:
                continue
            by_user.setdefault(login, set()).add(content)
        return [
            Reaction(username=u, contents=sorted(list(cs))) for u, cs in by_user.items()
        ]

    def get_line_content(self, data: dict) -> str:
        """Extract the content of a specific line from a GitHub diff hunk.

        Args:
            data (dict): A dict containing keys:
                - diff_hunk (str): The diff hunk content from GitHub API
                - line (int): The target line number in the new file

        Returns:
            str: The content of the target line without diff prefix,
                 or empty string if not found.
        """
        diff_hunk = data["diff_hunk"]
        target_new_line = data["line"]

        new_line_number = 0

        for line in diff_hunk.splitlines():
            if line.startswith("@@"):
                # Parse hunk header to get starting line number for new file
                match = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
                if match:
                    new_line_number = int(match.group(1)) - 1
                continue

            # Skip file headers
            if line.startswith("+++") or line.startswith("---"):
                continue

            # Process different types of diff lines
            if line.startswith("+"):
                # Added line: increment new file line number
                new_line_number += 1
                if new_line_number == target_new_line:
                    return line[1:]  # Return content without '+' prefix
            elif line.startswith("-"):
                # Deleted line: don't increment new file line number
                pass
            elif line.startswith(" ") or (
                line and not line.startswith(("+", "-", "@"))
            ):
                # Context line: increment new file line number
                new_line_number += 1
                if new_line_number == target_new_line:
                    return line[1:] if line.startswith(" ") else line

        return ""

    async def respond_to_comments(
        self, html_url: str, comments: ReviewComments
    ) -> None:
        """Respond to review comments on a pull request.

        Args:
            html_url (str): The HTML URL of the pull request.
            comments (ReviewComments): The review comments with replies to post.
        """
        # Extract PR number from HTML URL
        pr_number = html_url.rstrip("/").split("/")[-1]
        client = await self.get_client()

        proxy_agent = is_proxy_agent(self.person)
        signature = get_proxy_agent_signature(self.person) if proxy_agent else ""

        # Post replies to each inline comment if provided
        for inline in comments.inline_comment_threads:
            if inline.reply and inline.comments:
                reply = (
                    f"{inline.reply}\n\n{signature}" if proxy_agent else inline.reply
                )

                comment_id = inline.comments[0].comment_id
                await client.post(
                    f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}/comments/{comment_id}/replies",
                    json={"body": reply},
                )

        # Post a general comment if provided
        if comments.reply:
            reply = (
                f"{comments.reply}\n\n{signature}" if proxy_agent else comments.reply
            )
            await client.post(
                f"/repos/{self.owner}/{self.repo}/issues/{pr_number}/comments",
                json={"body": reply},
            )

    async def add_reaction_to_comment(
        self, html_url: str, comment_id: int, reaction: str, is_inline: bool
    ) -> None:
        """Add a reaction to a PR comment (inline or top-level)."""
        client = await self.get_client()
        headers = {"Accept": "application/vnd.github+json"}
        if is_inline:
            endpoint = (
                f"/repos/{self.owner}/{self.repo}/pulls/comments/{comment_id}/reactions"
            )
        else:
            endpoint = f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}/reactions"
        await client.post(endpoint, json={"content": reaction}, headers=headers)

    async def get_repository_url(self) -> str:
        """Get the repository URL for the current code hosting service."""
        return f"{self.repo_base_url}/{self.owner}/{self.repo}.git"

    async def get_default_branch(self) -> str:
        """Get the default branch of the repository."""
        client = await self.get_client()
        resp = await client.get(f"/repos/{self.owner}/{self.repo}")
        repo_info = resp.json()
        return repo_info.get("default_branch", "main")

    async def get_pull_request(self, html_url: str) -> PullRequest:
        """
        Get a pull request by its HTML URL.

        Args:
            html_url (str): The HTML URL of the pull request.
        Returns:
            bool: True if the pull request is merged, False otherwise.
        """
        pr_number = self.get_pr_number_from_url(html_url)
        client = await self.get_client()
        resp = await client.get(f"/repos/{self.owner}/{self.repo}/pulls/{pr_number}")
        pr = resp.json()
        merged = pr.get("merged_at") is not None
        return PullRequest(
            title=pr.get("title", ""),
            description=pr.get("body", ""),
            is_merged=merged,
            review_comments=await self.get_pull_request_comments(
                html_url, include_all_comments=True
            ),
        )
