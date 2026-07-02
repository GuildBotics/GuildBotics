from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from httpx import AsyncClient

from guildbotics.capabilities.member_memory import MemberMemoryService
from guildbotics.capabilities.member_reference import capability_reference_text
from guildbotics.entities.team import Person, Service, Team
from guildbotics.integrations.github.github_utils import (
    create_github_client,
    get_author_type,
    get_github_username,
    get_proxy_agent_signature,
    is_proxy_agent,
)
from guildbotics.utils.person_profile import build_member_communication_style

REPO_WITH_OWNER_PART_COUNT = 2
GITHUB_RESOURCE_MIN_PART_COUNT = 4


class MemberCapabilityError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitHubResource:
    owner: str
    repo: str
    number: int
    kind: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True)
class GitHubPullRequestHead:
    owner: str
    repo: str
    branch: str

    @property
    def full_repo(self) -> str:
        return f"{self.owner}/{self.repo}"


class MemberGitHubCapabilityService:
    def __init__(self, person: Person, team: Team) -> None:
        self.person = person
        self.team = team
        ticket_config = team.project.get_service_config(Service.TICKET_MANAGER)
        code_config = team.project.get_service_config(Service.CODE_HOSTING_SERVICE)
        self.base_url = str(
            code_config.get("api_base_url")
            or ticket_config.get("base_url")
            or "https://api.github.com"
        ).rstrip("/")
        self.owner = str(code_config.get("owner") or ticket_config.get("owner") or "")
        self.project_owner = str(ticket_config.get("owner") or self.owner)
        self.project_id = str(ticket_config.get("project_id") or "")
        self.project_url = str(ticket_config.get("url") or "")
        self._client: AsyncClient | None = None
        self._project_node_id: str | None = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def context(self, check_credentials: bool = False) -> dict[str, Any]:
        credential_status = "unchecked"
        if check_credentials:
            client = await self._get_client()
            # /rate_limit is readable by every credential type (PAT, machine
            # user, and GitHub App installation token). /user would 403 for
            # installation tokens (github_apps members), so it cannot be used
            # as a generic credential probe.
            resp = await client.get("/rate_limit")
            _raise_for_status(resp)
            credential_status = "ok"
        role_summaries = {
            role_id: {
                "summary": role.summary,
                "description": role.description,
            }
            for role_id, role in self.person.roles.items()
        }
        return {
            "person_id": self.person.person_id,
            "name": self.person.name,
            "person_type": self.person.person_type,
            "is_active": self.person.is_active,
            "roles": role_summaries,
            "profile": self.person.profile,
            "speaking_style": self.person.speaking_style,
            "communication_style": build_member_communication_style(self.person),
            "github_username": get_github_username(self.person),
            "proxy_agent_signature": (
                get_proxy_agent_signature(self.person)
                if is_proxy_agent(self.person)
                else ""
            ),
            "credential_status": credential_status,
            "memory": MemberMemoryService(self.person).load_context_memory(),
            # The full member command surface and cross-cutting rules. This is
            # the same reference printed by ``guildbotics member help`` and is
            # the single source every entrypoint relies on (context is the
            # mandatory first call), so each member can perform GitHub, git, and
            # chat work regardless of which workflow invoked it. Task contracts
            # (primary objective, required completion command) stay in the
            # prompts, never here.
            "capabilities": capability_reference_text(),
        }

    async def issue_inspect(self, url: str) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="issue")
        client = await self._get_client()
        issue_resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}"
        )
        _raise_for_status(issue_resp)
        comments_resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments"
        )
        _raise_for_status(comments_resp)
        issue = issue_resp.json()
        comments = [
            self._comment_summary(comment) for comment in _as_list(comments_resp.json())
        ]
        project_metadata = await self._issue_project_metadata(resource)
        linked_pull_request_candidates = await self._linked_pull_request_candidates(
            resource
        )
        return {
            "repo": resource.full_repo,
            "number": resource.number,
            "title": issue.get("title", ""),
            "body": issue.get("body", "") or "",
            "state": issue.get("state", ""),
            "html_url": issue.get("html_url", url),
            "assignees": [item.get("login", "") for item in issue.get("assignees", [])],
            "labels": [item.get("name", "") for item in issue.get("labels", [])],
            "project_metadata": project_metadata,
            "linked_pull_request_candidates": linked_pull_request_candidates,
            "comments": comments,
        }

    async def issue_comment(self, url: str, body: str) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="issue")
        comment = await self._post_comment(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments",
            body,
        )
        return _comment_result(comment)

    async def issue_create(
        self, repo: str, title: str, body: str, add_to_project: bool
    ) -> dict[str, Any]:
        owner, repo_name = self.parse_repo(repo)
        client = await self._get_client()
        resp = await client.post(
            f"/repos/{owner}/{repo_name}/issues",
            json={"title": title, "body": body},
        )
        _raise_for_status(resp)
        issue = resp.json()
        project_item_id = None
        if add_to_project and issue.get("node_id"):
            project_item_id = await self.add_project_item(str(issue["node_id"]))
        return {
            "issue_number": issue.get("number"),
            "issue_url": issue.get("html_url"),
            "project_item_id": project_item_id,
        }

    async def pr_inspect(self, url: str, include_comments: bool) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}"
        )
        _raise_for_status(resp)
        pr = resp.json()
        head = self._pull_request_head(resource, pr)
        result: dict[str, Any] = {
            "repo": resource.full_repo,
            "number": resource.number,
            "title": pr.get("title", ""),
            "body": pr.get("body", "") or "",
            "state": pr.get("state", ""),
            "merged": pr.get("merged_at") is not None,
            "draft": bool(pr.get("draft", False)),
            "html_url": pr.get("html_url", url),
            "head": head.branch,
            "head_repo": head.full_repo,
            "head_owner": head.owner,
            "head_repo_name": head.repo,
            "base": (pr.get("base") or {}).get("ref", ""),
        }
        if include_comments:
            result["conversation_comments"] = await self._issue_comments(resource)
            result["review_threads"] = await self._review_threads(resource)
        return result

    async def pr_create(
        self,
        repo: str,
        head: str,
        base: str,
        title: str,
        body: str,
        issue_url: str,
        draft: str,
    ) -> dict[str, Any]:
        owner, repo_name = self.parse_repo(repo)
        client = await self._get_client()
        base_branch = base.strip() or await self.default_branch(owner, repo_name)
        endpoint = f"/repos/{owner}/{repo_name}/pulls"
        params = {"head": f"{owner}:{head}", "base": base_branch, "state": "open"}
        existing_resp = await client.get(endpoint, params=params)
        _raise_for_status(existing_resp)
        existing = _as_list(existing_resp.json())
        if existing:
            pr = existing[0]
            return {
                "pr_number": pr.get("number"),
                "pr_url": pr.get("html_url"),
                "created": False,
                "draft": bool(pr.get("draft", False)),
                "head": head,
                "base": base_branch,
            }

        body = _append_closes(body, issue_url)
        payload: dict[str, Any] = {
            "title": title,
            "head": head,
            "base": base_branch,
            "body": body,
        }
        if draft == "true" or (draft == "auto" and is_proxy_agent(self.person)):
            payload["draft"] = True
        elif draft == "false":
            payload["draft"] = False
        resp = await client.post(endpoint, json=payload)
        _raise_for_status(resp)
        pr = resp.json()
        return {
            "pr_number": pr.get("number"),
            "pr_url": pr.get("html_url"),
            "created": True,
            "draft": bool(pr.get("draft", payload.get("draft", False))),
            "head": head,
            "base": base_branch,
        }

    async def pr_comment(self, url: str, body: str) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        comment = await self._post_comment(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments",
            body,
        )
        return _comment_result(comment)

    async def pr_reply(
        self, url: str, reply_target_id: int, body: str
    ) -> dict[str, Any]:
        resource = self.parse_url(url, expected_kind="pull")
        threads = await self._review_threads(resource)
        allowed = {
            int(thread["reply_target_id"])
            for thread in threads
            if thread.get("replyable") and thread.get("reply_target_id") is not None
        }
        if reply_target_id not in allowed:
            raise MemberCapabilityError(
                f"Review comment '{reply_target_id}' is not replyable for this PR."
            )
        client = await self._get_client()
        resp = await client.post(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}/comments/{reply_target_id}/replies",
            json={"body": self._append_signature(body)},
        )
        _raise_for_status(resp)
        reply = resp.json()
        return {
            "reply_comment_id": reply.get("id"),
            "html_url": reply.get("html_url"),
            "created_at": reply.get("created_at"),
        }

    async def reaction_add(
        self, repo: str, target: str, comment_id: int, reaction: str
    ) -> dict[str, Any]:
        owner, repo_name = self.parse_repo(repo)
        if target == "issue-comment":
            endpoint = (
                f"/repos/{owner}/{repo_name}/issues/comments/{comment_id}/reactions"
            )
        elif target == "pr-review-comment":
            endpoint = (
                f"/repos/{owner}/{repo_name}/pulls/comments/{comment_id}/reactions"
            )
        else:
            raise MemberCapabilityError(f"Unsupported reaction target '{target}'.")
        client = await self._get_client()
        resp = await client.post(
            endpoint,
            json={"content": reaction},
            headers={"Accept": "application/vnd.github+json"},
        )
        _raise_for_status(resp)
        payload = resp.json()
        return {
            "reaction_id": payload.get("id"),
            "content": payload.get("content", reaction),
            "comment_id": comment_id,
        }

    async def default_branch(self, owner: str, repo: str) -> str:
        client = await self._get_client()
        resp = await client.get(f"/repos/{owner}/{repo}")
        _raise_for_status(resp)
        return str(resp.json().get("default_branch") or "main")

    async def get_clone_url(self, owner: str, repo: str) -> str:
        return f"{self.web_base_url()}/{owner}/{repo}.git"

    def commit_url_from_remote(self, remote_url: str, sha: str) -> str:
        web_url = _remote_web_url(remote_url)
        if not web_url:
            return ""
        configured_host = urlparse(self.web_base_url()).hostname
        remote_host = urlparse(web_url).hostname
        if configured_host != remote_host:
            return ""
        return f"{web_url}/commit/{sha}"

    async def get_pr_head_branch(self, url: str) -> str:
        return (await self.get_pr_head(url)).branch

    async def get_pr_head(self, url: str) -> GitHubPullRequestHead:
        resource = self.parse_url(url, expected_kind="pull")
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/pulls/{resource.number}"
        )
        _raise_for_status(resp)
        return self._pull_request_head(resource, resp.json())

    def parse_repo(self, repo: str) -> tuple[str, str]:
        parts = [part for part in repo.strip().split("/") if part]
        if len(parts) == 1 and self.owner:
            return self.owner, parts[0]
        if len(parts) == REPO_WITH_OWNER_PART_COUNT:
            return parts[0], parts[1]
        raise MemberCapabilityError(
            f"Repository must be '<owner>/<repo>' or '<repo>': {repo}"
        )

    def parse_url(self, url: str, expected_kind: str | None = None) -> GitHubResource:
        parsed = urlparse(url)
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) < GITHUB_RESOURCE_MIN_PART_COUNT:
            raise MemberCapabilityError(f"Unsupported GitHub URL: {url}")
        owner, repo, kind, number_text = parts[0], parts[1], parts[2], parts[3]
        if kind == "pull":
            normalized_kind = "pull"
        elif kind == "issues":
            normalized_kind = "issue"
        else:
            raise MemberCapabilityError(f"Unsupported GitHub URL kind '{kind}'.")
        if expected_kind and normalized_kind != expected_kind:
            raise MemberCapabilityError(
                f"Expected {expected_kind} URL, got {normalized_kind} URL."
            )
        return GitHubResource(
            owner=owner,
            repo=repo,
            number=int(number_text),
            kind=normalized_kind,
        )

    def web_base_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base in ("https://api.github.com", "http://api.github.com"):
            return "https://github.com"
        for suffix in ("/api/v3", "/api"):
            if base.endswith(suffix):
                return base[: -len(suffix)]
        return base

    async def add_project_item(self, issue_node_id: str) -> str | None:
        if not self.project_id:
            return None
        project_node_id = await self._project_node()
        mutation = """
        mutation($proj: ID!, $content: ID!) {
          addProjectV2ItemById(input:{ projectId: $proj, contentId: $content }) {
            item { id }
          }
        }
        """
        data = await self._graphql(
            mutation, {"proj": project_node_id, "content": issue_node_id}
        )
        return data["addProjectV2ItemById"]["item"]["id"]

    async def _project_node(self) -> str:
        if self._project_node_id:
            return self._project_node_id
        if not self.project_owner or not self.project_id:
            raise MemberCapabilityError("GitHub ProjectV2 is not configured.")
        query_type = "organization" if "/orgs/" in self.project_url else "user"
        query = f"""
        query($owner:String!, $num:Int!) {{
          {query_type}(login:$owner) {{
            projectV2(number:$num) {{ id }}
          }}
        }}
        """
        data = await self._graphql(
            query, {"owner": self.project_owner, "num": int(self.project_id)}
        )
        project = data[query_type]["projectV2"]
        if not project:
            raise MemberCapabilityError(
                f"ProjectV2 number={self.project_id} not found for {self.project_owner}."
            )
        self._project_node_id = str(project["id"])
        return self._project_node_id

    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.post(
            "/graphql", json={"query": query, "variables": variables}
        )
        _raise_for_status(resp)
        payload = resp.json()
        if payload.get("errors"):
            raise MemberCapabilityError(str(payload["errors"]))
        return payload["data"]

    async def _get_client(self) -> AsyncClient:
        if self._client is None:
            self._client = await create_github_client(self.person, self.base_url)
        return self._client

    async def _issue_comments(self, resource: GitHubResource) -> list[dict[str, Any]]:
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/comments"
        )
        _raise_for_status(resp)
        return [self._comment_summary(comment) for comment in _as_list(resp.json())]

    async def _review_threads(self, resource: GitHubResource) -> list[dict[str, Any]]:
        query = """
        query($owner: String!, $repo: String!, $number: Int!, $after: String) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $number) {
              reviewThreads(first: 50, after: $after) {
                nodes {
                  isResolved
                  isOutdated
                  comments(first: 50) {
                    nodes {
                      databaseId
                      body
                      createdAt
                      url
                      author { login }
                      replyTo { databaseId }
                    }
                  }
                }
                pageInfo { endCursor hasNextPage }
              }
            }
          }
        }
        """
        nodes: list[dict[str, Any]] = []
        after: str | None = None
        while True:
            data = await self._graphql(
                query,
                {
                    "owner": resource.owner,
                    "repo": resource.repo,
                    "number": resource.number,
                    "after": after,
                },
            )
            pull_request = (data.get("repository") or {}).get("pullRequest")
            if not pull_request:
                raise MemberCapabilityError(
                    f"Pull request review threads unavailable for {resource.full_repo}#{resource.number}."
                )
            review_threads = pull_request["reviewThreads"]
            nodes.extend(review_threads.get("nodes") or [])
            page_info = review_threads["pageInfo"]
            if not page_info["hasNextPage"]:
                break
            after = page_info["endCursor"]

        threads: list[dict[str, Any]] = []
        for thread in nodes:
            comments = [
                comment
                for comment in ((thread.get("comments") or {}).get("nodes") or [])
                if comment.get("databaseId") is not None
            ]
            if not comments:
                continue
            comments.sort(key=lambda item: str(item.get("createdAt", "")))
            root = next(
                (comment for comment in comments if not comment.get("replyTo")),
                comments[0],
            )
            latest = comments[-1]
            root_id = int(root["databaseId"])
            latest_id = int(latest["databaseId"])
            resolved = bool(thread.get("isResolved"))
            outdated = bool(thread.get("isOutdated"))
            replyable = True
            threads.append(
                {
                    "root_comment_id": root_id,
                    "latest_comment_id": latest_id,
                    "resolved": resolved,
                    "outdated": outdated,
                    "replyable": replyable,
                    "reply_target_id": root_id if replyable else None,
                    "comments": [
                        self._graphql_review_comment_summary(comment)
                        for comment in comments
                    ],
                }
            )
        return threads

    async def _linked_pull_request_candidates(
        self, resource: GitHubResource
    ) -> list[dict[str, Any]]:
        client = await self._get_client()
        resp = await client.get(
            f"/repos/{resource.owner}/{resource.repo}/issues/{resource.number}/timeline",
            headers={"Accept": "application/vnd.github+json"},
        )
        try:
            resp.raise_for_status()
        except Exception:
            return []

        urls: list[str] = []
        for event in _as_list(resp.json()):
            source = event.get("source", {})
            issue = source.get("issue", {}) if isinstance(source, dict) else {}
            if "pull_request" in issue and issue.get("html_url"):
                urls.append(str(issue["html_url"]))

        candidates = []
        for url in dict.fromkeys(urls):
            try:
                pr_resource = self.parse_url(url, expected_kind="pull")
            except Exception:
                continue
            pr = await self.pr_inspect(url, include_comments=False)
            candidates.append(
                {
                    "number": pr_resource.number,
                    "url": pr.get("html_url", url),
                    "title": pr.get("title", ""),
                    "state": pr.get("state", ""),
                    "merged": pr.get("merged", False),
                }
            )
        return candidates

    async def _issue_project_metadata(self, resource: GitHubResource) -> dict[str, Any]:
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
          repository(owner: $owner, name: $repo) {
            issue(number: $number) {
              projectItems(first: 20) {
                nodes {
                  id
                  project { title number url }
                  fieldValues(first: 20) {
                    nodes {
                      ... on ProjectV2ItemFieldSingleSelectValue {
                        name
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldTextValue {
                        text
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldDateValue {
                        date
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                      ... on ProjectV2ItemFieldNumberValue {
                        number
                        field { ... on ProjectV2FieldCommon { name } }
                      }
                    }
                  }
                }
              }
            }
          }
        }
        """
        try:
            data = await self._graphql(
                query,
                {
                    "owner": resource.owner,
                    "repo": resource.repo,
                    "number": resource.number,
                },
            )
        except Exception:
            return {}
        issue = ((data.get("repository") or {}).get("issue")) or {}
        items = ((issue.get("projectItems") or {}).get("nodes")) or []
        metadata = [_project_item_summary(item) for item in items if item]
        return {"items": metadata} if metadata else {}

    async def _post_comment(self, endpoint: str, body: str) -> dict[str, Any]:
        client = await self._get_client()
        resp = await client.post(endpoint, json={"body": self._append_signature(body)})
        _raise_for_status(resp)
        return resp.json()

    def _append_signature(self, body: str) -> str:
        stripped = body.rstrip()
        if not is_proxy_agent(self.person):
            return stripped
        signature = get_proxy_agent_signature(self.person)
        if stripped.endswith(signature):
            return stripped
        return f"{stripped}\n\n{signature}" if stripped else signature

    def _comment_summary(self, comment: dict[str, Any]) -> dict[str, Any]:
        body = str(comment.get("body") or "")
        user = comment.get("user") or {}
        login = str(user.get("login") or "")
        return {
            "id": comment.get("id"),
            "body": body,
            "author": login,
            "author_type": get_author_type(self.person, login, body) if login else "",
            "created_at": comment.get("created_at"),
            "html_url": comment.get("html_url"),
        }

    def _graphql_review_comment_summary(
        self, comment: dict[str, Any]
    ) -> dict[str, Any]:
        body = str(comment.get("body") or "")
        author = comment.get("author") or {}
        login = str(author.get("login") or "")
        return {
            "id": comment.get("databaseId"),
            "body": body,
            "author": login,
            "author_type": get_author_type(self.person, login, body) if login else "",
            "created_at": comment.get("createdAt"),
            "html_url": comment.get("url"),
        }

    def _pull_request_head(
        self, resource: GitHubResource, pr: dict[str, Any]
    ) -> GitHubPullRequestHead:
        raw_head = pr.get("head")
        head = raw_head if isinstance(raw_head, dict) else {}
        branch = str(head.get("ref") or "")
        raw_repo = head.get("repo")
        repo = raw_repo if isinstance(raw_repo, dict) else {}
        full_name = str(repo.get("full_name") or "")
        raw_owner = repo.get("owner")
        repo_owner = raw_owner if isinstance(raw_owner, dict) else {}
        owner = str(repo_owner.get("login") or "")
        repo_name = str(repo.get("name") or "")
        if full_name and "/" in full_name:
            owner, repo_name = full_name.split("/", 1)
        if not branch:
            raise MemberCapabilityError(
                f"Pull request head branch not found for {resource.full_repo}#{resource.number}."
            )
        if not owner or not repo_name:
            label = str(head.get("label") or "")
            if label.startswith(f"{resource.owner}:"):
                owner, repo_name = resource.owner, resource.repo
            else:
                raise MemberCapabilityError(
                    f"Pull request head repository not found for {resource.full_repo}#{resource.number}."
                )
        return GitHubPullRequestHead(owner=owner, repo=repo_name, branch=branch)


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _raise_for_status(resp: Any) -> None:
    try:
        resp.raise_for_status()
    except Exception as exc:
        status_code = getattr(resp, "status_code", "")
        raise MemberCapabilityError(
            f"GitHub API request failed with status {status_code}."
        ) from exc


def _comment_result(comment: dict[str, Any]) -> dict[str, Any]:
    user = comment.get("user") or {}
    return {
        "comment_id": comment.get("id"),
        "html_url": comment.get("html_url"),
        "author": user.get("login", ""),
        "created_at": comment.get("created_at"),
    }


def _project_item_summary(item: dict[str, Any]) -> dict[str, Any]:
    project = item.get("project") or {}
    field_values = []
    for value in ((item.get("fieldValues") or {}).get("nodes")) or []:
        field = value.get("field") or {}
        field_name = field.get("name")
        if not field_name:
            continue
        field_values.append(
            {
                "field": field_name,
                "value": value.get("name")
                or value.get("text")
                or value.get("date")
                or value.get("number"),
            }
        )
    return {
        "item_id": item.get("id"),
        "project_title": project.get("title"),
        "project_number": project.get("number"),
        "project_url": project.get("url"),
        "fields": field_values,
    }


def _append_closes(body: str, issue_url: str) -> str:
    if not issue_url:
        return body
    match = re.search(r"/issues/(\d+)", issue_url)
    if not match:
        return body
    issue_number = match.group(1)
    closes = f"Closes #{issue_number}"
    if re.search(
        rf"\b(close[sd]?|fix(e[sd])?|resolve[sd]?)\s+#{issue_number}\b", body, re.I
    ):
        return body
    return f"{body.rstrip()}\n\n{closes}" if body.strip() else closes


def _remote_web_url(remote_url: str) -> str:
    value = remote_url.strip()
    if not value:
        return ""
    if "://" not in value and ":" in value:
        host, path = value.split(":", 1)
        host = host.rsplit("@", 1)[-1]
        return _strip_git_suffix(f"https://{host}/{path}") if host and path else ""
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.path:
        return _strip_git_suffix(f"{parsed.scheme}://{parsed.netloc}{parsed.path}")
    if parsed.scheme == "ssh" and parsed.hostname and parsed.path:
        return _strip_git_suffix(f"https://{parsed.hostname}{parsed.path}")
    return ""


def _strip_git_suffix(url: str) -> str:
    return url.removesuffix(".git").rstrip("/")
