"""Fixed GitHub read routes and GraphQL documents; no caller-supplied queries."""

from typing import Any
from urllib.parse import urlparse

from guildbotics.integrations.github.read_payloads import (
    _comment_summary,
    _graphql_review_comment_summary,
    _project_item_summary,
    _pull_request_file_summary,
    _review_summary,
)

REST_RESOURCES = {
    "issues": "issues/{identifier}",
    "pull_requests": "pulls/{identifier}",
    "issue_comments": "issues/{identifier}/comments",
    "issue_timeline": "issues/{identifier}/timeline",
    "pull_request_reviews": "pulls/{identifier}/reviews",
    "pull_request_files": "pulls/{identifier}/files",
}
DETAIL_RESOURCES = {"issues", "pull_requests", "pull_request_readiness"}
GRAPH_RESOURCES = {"issue_projects", "pull_request_threads", "review_thread_comments"}
_ITEM_URL_PARTS = 4

_COMMENTS = """nodes { databaseId body createdAt url author { login }
replyTo { databaseId } } pageInfo { endCursor hasNextPage }"""
_FIELDS = """nodes {
... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2FieldCommon { name } } }
... on ProjectV2ItemFieldTextValue { text field { ... on ProjectV2FieldCommon { name } } }
... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2FieldCommon { name } } }
... on ProjectV2ItemFieldNumberValue { number field { ... on ProjectV2FieldCommon { name } } }
} pageInfo { hasNextPage }"""


def graph_query(resource: str) -> str:
    """Select a fixed query. Nested connection limits must never look complete."""
    if resource == "review_thread_comments":
        return (
            """query($owner:String!, $repo:String!, $number:Int!, $node:ID!, $after:String, $size:Int!) {
repository(owner:$owner, name:$repo) { pullRequest(number:$number) { id } }
node(id:$node) { ... on PullRequestReviewThread {
pullRequest { id }
comments(first:$size, after:$after) { """
            + _COMMENTS
            + " } } } }"
        )
    if resource == "issue_projects":
        item, connection = "issue", "projectItems"
        fields = (
            "id project { title number url } fieldValues(first:100) { " + _FIELDS + " }"
        )
    else:
        item, connection = "pullRequest", "reviewThreads"
        fields = "id isResolved isOutdated comments(first:100) { " + _COMMENTS + " }"
    return (
        "query($owner:String!, $repo:String!, $number:Int!, $after:String, $size:Int!) {"
        f"repository(owner:$owner, name:$repo) {{ {item}(number:$number) {{"
        f"{connection}(first:$size, after:$after) {{ nodes {{ {fields} }}"
        "pageInfo { endCursor hasNextPage } } } } }"
    )


def graph_connection(resource: str, data: dict[str, Any]) -> dict[str, Any]:
    repository = data["repository"]
    if resource == "review_thread_comments":
        node = data["node"]
        if node["pullRequest"]["id"] != repository["pullRequest"]["id"]:
            raise ValueError("Thread does not belong to this pull request")
        return node["comments"]
    item, connection = (
        ("issue", "projectItems")
        if resource == "issue_projects"
        else ("pullRequest", "reviewThreads")
    )
    return repository[item][connection]


def translate(resource: str, item: dict[str, Any], person: Any) -> dict[str, Any]:
    """Translate one resource, without fetching or combining other resources."""
    if resource == "issue_comments":
        return _comment_summary(person, item)
    if resource == "pull_request_reviews":
        return _review_summary(person, item)
    if resource == "pull_request_files":
        return _pull_request_file_summary(item)
    if resource == "review_thread_comments":
        return {
            **_graphql_review_comment_summary(person, item),
            "reply_to_id": (item.get("replyTo") or {}).get("databaseId"),
        }
    if resource == "pull_request_threads":
        comments = item["comments"]
        return {
            "id": item["id"],
            "resolved": item["isResolved"],
            "outdated": item["isOutdated"],
            "comments": [
                translate("review_thread_comments", c, person)
                for c in comments["nodes"]
            ],
            "comments_complete": not comments["pageInfo"]["hasNextPage"],
        }
    if resource == "issue_projects":
        if item["fieldValues"]["pageInfo"]["hasNextPage"]:
            raise ValueError("Project fields exceed the supported bound")
        return _project_item_summary(item)
    if resource == "issue_timeline":
        source = (item.get("source") or {}).get("issue") or {}
        parts = urlparse(str(source.get("html_url") or "")).path.strip("/").split("/")
        link = None
        if (
            "pull_request" in source
            and len(parts) == _ITEM_URL_PARTS
            and parts[2] == "pull"
            and parts[3].isdigit()
        ):
            link = {"repo": "/".join(parts[:2]), "number": int(parts[3])}
        return {"event": item.get("event"), "pull_request": link}
    return item
