import pytest

from guildbotics.capabilities.member_github import MemberGitHubCapabilityService
from guildbotics.entities.team import Person, Project, Role, Team

HTTP_BAD_REQUEST = 400
ISSUE_NUMBER = 42
REPLY_COMMENT_ID = 123
ROOT_REVIEW_COMMENT_ID = 101
LATEST_REVIEW_COMMENT_ID = 102


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= HTTP_BAD_REQUEST:
            raise RuntimeError(self.status_code)


class FakeClient:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.get_payloads = {}
        self.post_payloads = {}
        self.graphql_payloads = []

    async def get(self, endpoint, params=None, headers=None):
        self.gets.append((endpoint, params, headers))
        return FakeResponse(self.get_payloads.get(endpoint, []))

    async def post(self, endpoint, json=None, headers=None):
        self.posts.append((endpoint, json, headers))
        if endpoint == "/graphql":
            payload = self.graphql_payloads.pop(0) if self.graphql_payloads else {}
            return FakeResponse(payload)
        if endpoint in self.post_payloads:
            return FakeResponse(self.post_payloads[endpoint])
        if endpoint.endswith("/reactions"):
            return FakeResponse({"id": 10, "content": json["content"]})
        return FakeResponse(
            {
                "id": 123,
                "html_url": "https://github.com/owner/repo/pull/1#discussion_r123",
                "created_at": "2026-01-01T00:00:00Z",
                "user": {"login": "bot"},
            }
        )


def _service(person_type="proxy_agent"):
    person = Person(
        person_id="aiko",
        name="Aiko",
        person_type=person_type,
        account_info={"github_username": "bot"},
    )
    team = Team(
        project=Project(
            name="demo",
            services={
                "code_hosting_service": {
                    "name": "github",
                    "owner": "owner",
                    "api_base_url": "https://api.github.com",
                },
                "ticket_manager": {
                    "name": "github",
                    "owner": "owner",
                    "project_id": "1",
                    "url": "https://github.com/orgs/owner/projects/1",
                },
            },
        ),
        members=[person],
    )
    return MemberGitHubCapabilityService(person, team)


def test_parse_github_issue_and_pull_request_urls():
    service = _service()

    issue = service.parse_url("https://github.com/owner/repo/issues/42")
    pull = service.parse_url("https://github.com/owner/repo/pull/7")

    assert issue.full_repo == "owner/repo"
    assert issue.number == ISSUE_NUMBER
    assert issue.kind == "issue"
    assert pull.kind == "pull"


@pytest.mark.asyncio
async def test_context_returns_llm_ready_communication_style():
    person = Person(
        person_id="yuki",
        name="Yuki Nakamura",
        person_type="machine_user",
        speaking_style="柔らかく親しみやすい日本語で話す。",
        roles={
            "programmer": Role(
                id="programmer",
                summary="プロダクトのコードベースを改善する役割。",
                description="",
            )
        },
        profile={
            "character": {
                "archetype": "親しみやすいアイデアメーカー",
                "traits": ["明るい", "共感的"],
                "interests": ["UX"],
                "conversation_preferences": {
                    "contribution_style": ["具体案をやわらかく提案する"]
                },
            }
        },
        account_info={"github_username": "yuki-bot"},
    )
    team = Team(project=Project(name="demo"), members=[person])
    service = MemberGitHubCapabilityService(person, team)

    result = await service.context()

    assert result["speaking_style"] == "柔らかく親しみやすい日本語で話す。"
    style = result["communication_style"]
    assert "Yuki Nakamura" in style["active_member_instruction"]
    assert "親しみやすいアイデアメーカー" in style["voice_basis"]
    assert "具体案をやわらかく提案する" in style["voice_basis"]
    assert "interactive replies" in style["interactive_replies"]
    assert "GitHub issue comments" in style["github_comments"]
    assert "PR titles/bodies" in style["neutral_documents"]
    assert "workflow AgentResponse.message" in style["machine_outputs"]
    assert "token" not in str(style).lower()


@pytest.mark.asyncio
async def test_context_check_credentials_uses_rate_limit_endpoint():
    # /rate_limit works for every credential type, including GitHub App
    # installation tokens (github_apps) where /user would 403.
    service = _service(person_type="github_apps")
    fake = FakeClient()
    service._client = fake

    result = await service.context(check_credentials=True)

    assert result["credential_status"] == "ok"
    assert [endpoint for endpoint, *_ in fake.gets] == ["/rate_limit"]


@pytest.mark.asyncio
async def test_context_includes_capability_reference():
    service = _service()
    fake = FakeClient()
    service._client = fake

    result = await service.context()

    # The context carries the full capability reference (single source of truth)
    # instead of a flat command-name list, and not the old fields.
    assert "guildbotics member chat reply" in result["capabilities"]
    assert "guildbotics member github pr create" in result["capabilities"]
    assert "available_member_commands" not in result
    assert "safety_note" not in result


@pytest.mark.asyncio
async def test_context_without_check_credentials_is_unchecked():
    service = _service()
    fake = FakeClient()
    service._client = fake

    result = await service.context()

    assert result["credential_status"] == "unchecked"
    assert fake.gets == []


@pytest.mark.asyncio
async def test_pr_reply_uses_pull_replies_endpoint_and_proxy_signature():
    service = _service()
    fake = FakeClient()
    fake.graphql_payloads.append(_review_threads_payload())
    service._client = fake

    result = await service.pr_reply(
        "https://github.com/owner/repo/pull/7", ROOT_REVIEW_COMMENT_ID, "Fixed."
    )

    assert result["reply_comment_id"] == REPLY_COMMENT_ID
    assert fake.posts[-1:] == [
        (
            "/repos/owner/repo/pulls/7/comments/101/replies",
            {"body": "Fixed.\n\n⚙aiko"},
            None,
        )
    ]


@pytest.mark.asyncio
async def test_pr_reply_allows_outdated_thread():
    service = _service()
    fake = FakeClient()
    fake.graphql_payloads.append(_review_threads_payload(outdated=True))
    service._client = fake

    result = await service.pr_reply(
        "https://github.com/owner/repo/pull/7", ROOT_REVIEW_COMMENT_ID, "Fixed."
    )

    assert result["reply_comment_id"] == REPLY_COMMENT_ID
    assert fake.posts[-1:] == [
        (
            "/repos/owner/repo/pulls/7/comments/101/replies",
            {"body": "Fixed.\n\n⚙aiko"},
            None,
        )
    ]


@pytest.mark.asyncio
async def test_pr_reply_allows_resolved_thread():
    service = _service()
    fake = FakeClient()
    fake.graphql_payloads.append(_review_threads_payload(resolved=True))
    service._client = fake

    result = await service.pr_reply(
        "https://github.com/owner/repo/pull/7", ROOT_REVIEW_COMMENT_ID, "Fixed."
    )

    assert result["reply_comment_id"] == REPLY_COMMENT_ID
    assert fake.posts[-1:] == [
        (
            "/repos/owner/repo/pulls/7/comments/101/replies",
            {"body": "Fixed.\n\n⚙aiko"},
            None,
        )
    ]


@pytest.mark.asyncio
async def test_pr_inspect_includes_review_thread_resolution_fields():
    service = _service()
    fake = FakeClient()
    fake.get_payloads["/repos/owner/repo/pulls/7"] = {
        "title": "PR",
        "body": "Body",
        "state": "open",
        "merged_at": None,
        "draft": False,
        "html_url": "https://github.com/owner/repo/pull/7",
        "head": {"ref": "feature", "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main"},
    }
    fake.get_payloads["/repos/owner/repo/issues/7/comments"] = []
    fake.graphql_payloads.append(_review_threads_payload(resolved=True))
    service._client = fake

    result = await service.pr_inspect(
        "https://github.com/owner/repo/pull/7", include_comments=True
    )

    assert result["review_threads"][0]["root_comment_id"] == ROOT_REVIEW_COMMENT_ID
    assert result["review_threads"][0]["latest_comment_id"] == LATEST_REVIEW_COMMENT_ID
    assert result["review_threads"][0]["resolved"] is True
    assert result["review_threads"][0]["replyable"] is True
    assert result["review_threads"][0]["reply_target_id"] == ROOT_REVIEW_COMMENT_ID


@pytest.mark.asyncio
async def test_pr_inspect_includes_fork_head_repository():
    service = _service()
    fake = FakeClient()
    fake.get_payloads["/repos/owner/repo/pulls/7"] = {
        "title": "PR",
        "body": "Body",
        "state": "open",
        "merged_at": None,
        "draft": False,
        "html_url": "https://github.com/owner/repo/pull/7",
        "head": {
            "ref": "feature",
            "repo": {
                "full_name": "contributor/repo",
                "name": "repo",
                "owner": {"login": "contributor"},
            },
        },
        "base": {"ref": "main"},
    }
    service._client = fake

    result = await service.pr_inspect(
        "https://github.com/owner/repo/pull/7", include_comments=False
    )
    head = await service.get_pr_head("https://github.com/owner/repo/pull/7")

    assert result["head"] == "feature"
    assert result["head_repo"] == "contributor/repo"
    assert result["head_owner"] == "contributor"
    assert result["head_repo_name"] == "repo"
    assert head.full_repo == "contributor/repo"
    assert head.branch == "feature"


@pytest.mark.asyncio
async def test_pr_inspect_marks_outdated_thread_replyable():
    service = _service()
    fake = FakeClient()
    fake.get_payloads["/repos/owner/repo/pulls/7"] = {
        "title": "PR",
        "body": "Body",
        "state": "open",
        "merged_at": None,
        "draft": False,
        "html_url": "https://github.com/owner/repo/pull/7",
        "head": {"ref": "feature", "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main"},
    }
    fake.get_payloads["/repos/owner/repo/issues/7/comments"] = []
    fake.graphql_payloads.append(_review_threads_payload(outdated=True))
    service._client = fake

    result = await service.pr_inspect(
        "https://github.com/owner/repo/pull/7", include_comments=True
    )

    assert result["review_threads"][0]["outdated"] is True
    assert result["review_threads"][0]["replyable"] is True
    assert result["review_threads"][0]["reply_target_id"] == ROOT_REVIEW_COMMENT_ID


@pytest.mark.asyncio
async def test_issue_inspect_returns_linked_pull_request_candidates():
    service = _service()
    fake = FakeClient()
    fake.get_payloads["/repos/owner/repo/issues/42"] = {
        "title": "Issue",
        "body": "Body",
        "state": "open",
        "html_url": "https://github.com/owner/repo/issues/42",
        "assignees": [],
        "labels": [],
    }
    fake.get_payloads["/repos/owner/repo/issues/42/comments"] = []
    fake.get_payloads["/repos/owner/repo/issues/42/timeline"] = [
        {
            "source": {
                "issue": {
                    "pull_request": {},
                    "html_url": "https://github.com/owner/repo/pull/5",
                }
            }
        }
    ]
    fake.get_payloads["/repos/owner/repo/pulls/5"] = {
        "title": "PR",
        "body": "",
        "state": "open",
        "merged_at": None,
        "draft": False,
        "html_url": "https://github.com/owner/repo/pull/5",
        "head": {"ref": "feature", "repo": {"full_name": "owner/repo"}},
        "base": {"ref": "main"},
    }
    service._client = fake

    result = await service.issue_inspect("https://github.com/owner/repo/issues/42")

    assert result["project_metadata"] == {}
    assert result["linked_pull_request_candidates"] == [
        {
            "number": 5,
            "url": "https://github.com/owner/repo/pull/5",
            "title": "PR",
            "state": "open",
            "merged": False,
        }
    ]


@pytest.mark.asyncio
async def test_issue_inspect_returns_project_metadata_when_available():
    service = _service()
    fake = FakeClient()
    fake.get_payloads["/repos/owner/repo/issues/42"] = {
        "title": "Issue",
        "body": "Body",
        "state": "open",
        "html_url": "https://github.com/owner/repo/issues/42",
        "assignees": [],
        "labels": [],
    }
    fake.get_payloads["/repos/owner/repo/issues/42/comments"] = []
    fake.get_payloads["/repos/owner/repo/issues/42/timeline"] = []
    fake.graphql_payloads.append(
        {
            "data": {
                "repository": {
                    "issue": {
                        "projectItems": {
                            "nodes": [
                                {
                                    "id": "PVTI_1",
                                    "project": {
                                        "title": "Tasks",
                                        "number": 1,
                                        "url": "https://github.com/orgs/owner/projects/1",
                                    },
                                    "fieldValues": {
                                        "nodes": [
                                            {
                                                "name": "Ready",
                                                "field": {"name": "Status"},
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    service._client = fake

    result = await service.issue_inspect("https://github.com/owner/repo/issues/42")

    assert result["project_metadata"] == {
        "items": [
            {
                "item_id": "PVTI_1",
                "project_title": "Tasks",
                "project_number": 1,
                "project_url": "https://github.com/orgs/owner/projects/1",
                "fields": [{"field": "Status", "value": "Ready"}],
            }
        ]
    }


@pytest.mark.asyncio
async def test_issue_create_creates_real_issue_and_adds_project_item():
    service = _service()
    fake = FakeClient()
    fake.post_payloads["/repos/owner/repo/issues"] = {
        "number": 43,
        "html_url": "https://github.com/owner/repo/issues/43",
        "node_id": "ISSUE_node",
    }
    fake.graphql_payloads.extend(
        [
            {"data": {"organization": {"projectV2": {"id": "PROJECT_node"}}}},
            {"data": {"addProjectV2ItemById": {"item": {"id": "PROJECT_ITEM_node"}}}},
        ]
    )
    service._client = fake

    result = await service.issue_create("repo", "Follow-up", "Body", True)

    assert result == {
        "issue_number": 43,
        "issue_url": "https://github.com/owner/repo/issues/43",
        "project_item_id": "PROJECT_ITEM_node",
    }
    assert fake.posts[0] == (
        "/repos/owner/repo/issues",
        {"title": "Follow-up", "body": "Body"},
        None,
    )
    assert fake.posts[1][0] == "/graphql"
    assert fake.posts[2][0] == "/graphql"


@pytest.mark.asyncio
async def test_pr_create_uses_explicit_base_branch():
    service = _service(person_type="human")
    fake = FakeClient()
    fake.get_payloads["/repos/owner/repo/pulls"] = []
    fake.post_payloads["/repos/owner/repo/pulls"] = {
        "number": 8,
        "html_url": "https://github.com/owner/repo/pull/8",
        "draft": False,
    }
    service._client = fake

    result = await service.pr_create(
        "owner/repo",
        "feature",
        "ticket-driven-workflow",
        "Title",
        "Body",
        "",
        "false",
    )

    assert result == {
        "pr_number": 8,
        "pr_url": "https://github.com/owner/repo/pull/8",
        "created": True,
        "draft": False,
        "head": "feature",
        "base": "ticket-driven-workflow",
    }
    assert fake.gets[0] == (
        "/repos/owner/repo/pulls",
        {
            "head": "owner:feature",
            "base": "ticket-driven-workflow",
            "state": "open",
        },
        None,
    )
    assert fake.posts[0] == (
        "/repos/owner/repo/pulls",
        {
            "title": "Title",
            "head": "feature",
            "base": "ticket-driven-workflow",
            "body": "Body",
            "draft": False,
        },
        None,
    )


@pytest.mark.asyncio
async def test_pr_create_uses_default_branch_when_base_is_empty():
    service = _service(person_type="human")
    fake = FakeClient()
    fake.get_payloads["/repos/owner/repo"] = {"default_branch": "develop"}
    fake.get_payloads["/repos/owner/repo/pulls"] = []
    fake.post_payloads["/repos/owner/repo/pulls"] = {
        "number": 9,
        "html_url": "https://github.com/owner/repo/pull/9",
        "draft": False,
    }
    service._client = fake

    result = await service.pr_create(
        "owner/repo",
        "feature",
        "",
        "Title",
        "Body",
        "",
        "false",
    )

    assert result["base"] == "develop"
    assert fake.gets[:2] == [
        ("/repos/owner/repo", None, None),
        (
            "/repos/owner/repo/pulls",
            {"head": "owner:feature", "base": "develop", "state": "open"},
            None,
        ),
    ]
    assert fake.posts[0][1]["base"] == "develop"


@pytest.mark.asyncio
async def test_reaction_add_uses_target_specific_endpoint():
    service = _service(person_type="human")
    fake = FakeClient()
    service._client = fake

    result = await service.reaction_add("owner/repo", "pr-review-comment", 101, "+1")

    assert result == {"reaction_id": 10, "content": "+1", "comment_id": 101}
    assert fake.posts == [
        (
            "/repos/owner/repo/pulls/comments/101/reactions",
            {"content": "+1"},
            {"Accept": "application/vnd.github+json"},
        )
    ]


def _review_threads_payload(resolved=False, outdated=False):
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "nodes": [
                            {
                                "isResolved": resolved,
                                "isOutdated": outdated,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": ROOT_REVIEW_COMMENT_ID,
                                            "body": "Please fix",
                                            "createdAt": "2026-01-01T00:00:00Z",
                                            "url": "https://github.com/owner/repo/pull/7#discussion_r101",
                                            "author": {"login": "reviewer"},
                                            "replyTo": None,
                                        },
                                        {
                                            "databaseId": LATEST_REVIEW_COMMENT_ID,
                                            "body": "More context",
                                            "createdAt": "2026-01-01T00:01:00Z",
                                            "url": "https://github.com/owner/repo/pull/7#discussion_r102",
                                            "author": {"login": "reviewer"},
                                            "replyTo": {
                                                "databaseId": ROOT_REVIEW_COMMENT_ID
                                            },
                                        },
                                    ]
                                },
                            }
                        ],
                        "pageInfo": {"endCursor": None, "hasNextPage": False},
                    }
                }
            }
        }
    }
