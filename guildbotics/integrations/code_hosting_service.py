import time
from abc import ABC, abstractmethod

from pydantic import BaseModel, Field

from guildbotics.entities.message import Message
from guildbotics.entities.team import Person, Team


class Reaction(BaseModel):
    """Reactions grouped by username: a list of reaction emojis or similar content that a user added to a comment."""

    username: str
    contents: list[str] = Field(default_factory=list)


def get_author(author: str, is_reviewee: bool) -> str:
    """Get the author string for the inline comment."""
    return "Reviewee (myself)" if is_reviewee else f"Reviewer ({author})"


class InlineComment(BaseModel):
    """Model for inline comments in a pull request."""

    path: str
    line: int
    body: str
    comment_id: int
    author: str
    created_at: str
    is_reviewee: bool = False
    line_content: str | None = None
    is_checked: bool = False

    def __lt__(self, other: "InlineComment") -> bool:
        """Compare two inline comments by path, then line, then creation time."""
        if self.path != other.path:
            return self.path < other.path
        if self.line != other.line:
            return self.line < other.line
        return self.created_at < other.created_at

    def to_simple_dict(self) -> dict:
        return {Message.ASSISTANT if self.is_reviewee else Message.USER: self.body}


class InlineCommentThread(BaseModel):
    """Model for a thread of inline comments in a pull request."""

    path: str
    line: int
    comments: list[InlineComment]
    reply: str | None = None

    def is_replied(self) -> bool:
        """Check if the last comment in the thread is a reply from the reviewee."""
        return (
            len(self.comments) == 0
            or self.comments[-1].is_reviewee
            or self.comments[-1].is_checked
        )

    def to_dict(self) -> dict:
        """Convert the inline comment thread to a dictionary format."""
        return {
            "path": self.path,
            "line": self.line,
            "line_content": self.comments[0].line_content if self.comments else "",
            "comments": [comment.to_simple_dict() for comment in self.comments],
        }

    def __str__(self):
        """String representation of the inline comment."""
        header = f"**File:** {self.path}\n**Line:** {self.line}\n```\n{self.comments[0].line_content}"
        comment_bodies = "\n".join(
            f"{comment.author}: {comment.body}" for comment in self.comments
        )
        return f"{header}\n```\n**Review Comment:**\n{comment_bodies}"

    def add_reply(self, reply: str):
        """Add a reply to the inline comment thread."""
        self.reply = reply
        self.comments.append(
            InlineComment(
                path=self.path,
                line=self.line,
                body=reply,
                comment_id=-1,  # Placeholder for reply comment ID
                author=get_author("reviewee", True),
                created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                is_reviewee=True,
                is_checked=True,
            )
        )


class ReviewComment(BaseModel):
    """Model for review comments in a pull request."""

    comment_id: int | None = None
    body: str
    author: str
    created_at: str
    is_reviewee: bool = False
    is_checked: bool = False

    def __lt__(self, other: "ReviewComment") -> bool:
        """Compare two comments by creation time."""
        return self.created_at < other.created_at

    def __str__(self):
        """String representation of the review comment."""
        return f"{self.author}: {self.body}"

    def to_simple_dict(self) -> dict:
        """Convert the review comment to a simple dictionary format."""
        return {Message.ASSISTANT if self.is_reviewee else Message.USER: self.body}


class ReviewComments:
    """Model for review comments in a pull request."""

    body: str | None = None
    review_comments: list[ReviewComment] = []
    inline_comment_threads: list[InlineCommentThread] = []
    reply: str | None = None
    is_replied: bool = False

    def __init__(
        self,
        review_comments: list[ReviewComment],
        inline_comments: list[InlineComment],
        include_all_comments: bool = False,
    ):
        """Initialize and sort both review_comments and inline_comments."""
        review_comments.sort()
        self.review_comments = review_comments
        self.body = (
            "\n\n".join(str(comment) for comment in review_comments)
            if review_comments
            else ""
        )

        # Sort inline_comments by path, line, created_at (uses InlineComment.__lt__)
        inline_comments.sort()

        # Merge inline_comments to self.inline_comment_threads with identical path and line
        merged_inline: list[InlineCommentThread] = []
        for comment in inline_comments:
            if merged_inline and (comment.path, comment.line) == (
                merged_inline[-1].path,
                merged_inline[-1].line,
            ):
                # Append body separated by newline; keep other fields from first occurrence
                merged_inline[-1].comments.append(comment)
            else:
                merged_inline.append(
                    InlineCommentThread(
                        path=comment.path, line=comment.line, comments=[comment]
                    )
                )
        if include_all_comments:
            self.inline_comment_threads = merged_inline
        else:
            self.inline_comment_threads = [
                comment_thread
                for comment_thread in merged_inline
                if not comment_thread.is_replied()
            ]

        review_comments_replied = (
            len(review_comments) == 0
            or review_comments[-1].is_reviewee
            or review_comments[-1].is_checked
        )
        self.is_replied = (
            len(self.inline_comment_threads) == 0 and review_comments_replied
        )

    def __str__(self):
        """String representation of the review comment."""
        return f"Review Comments:\n{self.body}\n" if self.body else ""

    def to_simple_dicts(self) -> list[dict]:
        """Convert the review comments to a list of simple dictionaries."""
        return [comment.to_simple_dict() for comment in self.review_comments]


class PullRequest:
    """Model for a pull request."""

    title: str
    description: str
    review_comments: ReviewComments
    is_merged: bool

    def __init__(
        self,
        title: str,
        description: str,
        review_comments: ReviewComments,
        is_merged: bool,
    ):
        """
        Initialize the PullRequest.

        Args:
            title (str): The title of the pull request.
            description (str): The description of the pull request.
            review_comments (ReviewComments): The comments on the pull request.
            is_merged (bool): Whether the pull request has been merged.
        """
        self.title = title
        self.description = description
        self.review_comments = review_comments
        self.is_merged = is_merged


class CodeHostingService(ABC):
    """Abstract base class for code hosting services."""

    def __init__(self, person: Person, team: Team):
        """
        Initialize the CodeHostingService.
        Args:
            person (Person): The person associated with the code hosting service.
            team (Team): The team associated with the code hosting service.
        """
        self.person = person
        self.team = team

    @abstractmethod
    async def create_pull_request(
        self, branch_name: str, title: str, description: str, ticket_url: str
    ) -> str:
        """
        Create a pull request in the code hosting service.

        Args:
            branch_name (str): The name of the branch to merge.
            title (str): The title of the pull request.
            description (str): The description of the pull request.
            ticket_url (str): The URL of the associated ticket or task.

        Returns:
            str: The URL of the created pull request.
        """
        pass

    @abstractmethod
    async def get_pull_request_comments(
        self, html_url: str, include_all_comments: bool = False
    ) -> ReviewComments:
        """
        Get comments from a pull request.

        Args:
            html_url (str): The URL of the pull request.
            include_all_comments (bool): Whether to include all comments or only unresolved ones.

        Returns:
            list[dict]: A list of comments on the pull request.
        """
        pass

    @abstractmethod
    async def respond_to_comments(
        self, html_url: str, comments: ReviewComments
    ) -> None:
        """
        Respond to comments on a pull request.

        Args:
            html_url (str): The URL of the pull request.
            comments (ReviewComments): The comments to respond to.
        """
        pass

    @abstractmethod
    async def add_reaction_to_comment(
        self, html_url: str, comment_id: int, reaction: str, is_inline: bool
    ) -> None:
        """
        Add a reaction to a pull request comment.

        Args:
            html_url (str): The URL of the pull request.
            comment_id (int): The comment ID to react to.
            reaction (str): The reaction content (e.g., "+1", "eyes").
            is_inline (bool): True for inline review comments, False for top-level issue comments.
        """
        pass

    @abstractmethod
    async def get_repository_url(self) -> str:
        """
        Get the URL of the repository.

        Returns:
            str: The URL of the repository.
        """
        pass

    @abstractmethod
    async def get_default_branch(self) -> str:
        """
        Get the default branch of the repository.

        Returns:
            str: The name of the default branch.
        """
        pass

    @abstractmethod
    async def get_pull_request(self, html_url: str) -> PullRequest:
        """
        Get a pull request by its HTML URL.

        Args:
            html_url (str): The HTML URL of the pull request.

        Returns:
            PullRequest: The pull request object.
        """
        pass
