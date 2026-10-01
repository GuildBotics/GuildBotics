"""Combine issue resources into a complete inspection, without inference."""

from typing import Any

from guildbotics.commands.repository import RepositoryReader, display

COMMAND_METADATA = {
    "description": {
        "en": "Inspect an issue, comments, projects and linked pull requests.",
        "ja": "Issue の本文・コメント・Project・関連 PR を確認します。",
    },
    "read_only": True,
}


async def main(context: Any, repo: str, number: str) -> str:
    reader = RepositoryReader(context, repo, number)
    result = await reader.one("issues")
    result["comments"] = await reader.read("issue_comments")
    result["project_metadata"] = {"items": await reader.read("issue_projects")}
    timeline = await reader.read("issue_timeline")
    candidates = []
    seen = set()
    for event in timeline:
        link = event.get("pull_request")
        if not link or (link["repo"], link["number"]) in seen:
            continue
        seen.add((link["repo"], link["number"]))
        linked = RepositoryReader(context, link["repo"], str(link["number"]))
        linked.bytes = reader.bytes
        pr = await linked.one("pull_requests")
        reader.bytes = linked.bytes
        candidates.append(
            {key: pr[key] for key in ("number", "html_url", "title", "state", "merged")}
        )
    result["linked_pull_request_candidates"] = candidates
    return display(result)
