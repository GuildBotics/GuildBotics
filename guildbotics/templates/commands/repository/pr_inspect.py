"""Combine pull-request resources without moving host guarantees into commands."""

from typing import Any

from guildbotics.commands.errors import CommandError
from guildbotics.commands.repository import RepositoryReader, display, flag
from guildbotics.utils.i18n_tool import t

COMMAND_METADATA = {
    "description": {
        "en": "Inspect a pull request, optionally including feedback and diff coordinates.",
        "ja": "PR を確認します。レビューの会話と差分座標も取得できます。",
    },
    "read_only": True,
}


async def main(
    context: Any,
    repo: str,
    number: str,
    include_comments: str = "false",
    include_diff: str = "false",
) -> str:
    comments, diff = flag(include_comments), flag(include_diff)
    reader = RepositoryReader(context, repo, number)
    result = await reader.one("pull_requests")
    result["checks"] = await reader.one("pull_request_readiness")
    if comments:
        result["conversation_comments"] = await reader.read("issue_comments")
        result["review_summaries"] = await reader.read("pull_request_reviews")
        threads = await reader.read("pull_request_threads")
        for thread in threads:
            if not thread.pop("comments_complete"):
                thread["comments"] = await reader.read(
                    "review_thread_comments", node=thread["id"]
                )
            replies = thread["comments"]
            roots = [c for c in replies if c["reply_to_id"] is None]
            thread["reply_target_id"] = roots[0]["id"] if roots else None
            thread["replyable"] = bool(roots)
        result["review_threads"] = threads
    if diff:
        # Patches expand into per-line comment coordinates in the host response.
        files = await reader.read("pull_request_files", page_size=5)
        if (
            result.get("changed_files") is not None
            and len(files) != result["changed_files"]
        ):
            raise CommandError(
                t(
                    "commands.repository.inspect.incomplete",
                    resource="pull_request_files",
                )
            )
        result["files"] = files
        result["diff_complete"] = all(item["patch_complete"] for item in files)
    return display(result)
