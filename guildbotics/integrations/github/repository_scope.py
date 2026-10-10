"""Which GitHub requests a member's client may send.

Every request of a member's GitHub client passes the judgment here before it
is sent (``get_async_client``): a write goes only to repositories of the
configured owner (:mod:`guildbotics.integrations.repository_scope`).

Reads are not limited by owner, but nothing is sent to a host other than the
client's API. A GraphQL write names a node rather than a repository, so the
gate reads where that node lives before it lets the write through. What is not
recognized as a read or as a write to the configured owner is refused, so a
new kind of write is refused until it is classified here.
"""

from __future__ import annotations

import json
import re

import httpx

from guildbotics.integrations.repository_scope import in_scope, refuse

ADD_PROJECT_ITEM = """
mutation($proj: ID!, $content: ID!) {
  addProjectV2ItemById(input: {projectId: $proj, contentId: $content}) {
    item { id }
  }
}
"""

UPDATE_PROJECT_ITEM_STATUS = """
mutation($proj: ID!, $item: ID!, $field: ID!, $opt: String!) {
  updateProjectV2ItemFieldValue(
    input: {
      projectId: $proj
      itemId: $item
      fieldId: $field
      value: {singleSelectOptionId: $opt}
    }
  ) {
    projectV2Item { id }
  }
}
"""

UPDATE_PROJECT_FIELD_OPTIONS = """
mutation($field: ID!, $options: [ProjectV2SingleSelectFieldOptionInput!]) {
  updateProjectV2Field(input: {fieldId: $field, singleSelectOptions: $options}) {
    projectV2Field {
      ... on ProjectV2SingleSelectField { id }
    }
  }
}
"""

CREATE_PROJECT_FIELD = """
mutation(
  $proj: ID!
  $name: String!
  $dataType: ProjectV2CustomFieldType!
  $options: [ProjectV2SingleSelectFieldOptionInput!]
) {
  createProjectV2Field(
    input: {
      projectId: $proj
      name: $name
      dataType: $dataType
      singleSelectOptions: $options
    }
  ) {
    projectV2Field {
      ... on ProjectV2Field { id name dataType }
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

#: A reaction on anything GitHub reacts to through GraphQL only: REST has no
#: endpoint for a review's body.
ADD_REACTION = """
mutation($subject: ID!, $content: ReactionContent!) {
  addReaction(input: {subjectId: $subject, content: $content}) {
    reaction { content }
  }
}
"""

#: The patrol's hand-off of a pull request to a human: REST cannot make an
#: open pull request a draft.
CONVERT_PULL_REQUEST_TO_DRAFT = """
mutation($pullRequest: ID!) {
  convertPullRequestToDraft(input: {pullRequestId: $pullRequest}) {
    pullRequest { isDraft }
  }
}
"""

#: The GraphQL writes that name a node in some repository, each with the
#: variable that carries the node: the gate reads where the node lives first.
#: Adding an issue to the Project is one: the issue records it in its timeline.
NODE_MUTATIONS = {
    ADD_PROJECT_ITEM: "content",
    ADD_REACTION: "subject",
    CONVERT_PULL_REQUEST_TO_DRAFT: "pullRequest",
}

#: Where the node of a ``NODE_MUTATIONS`` write lives, read before it is sent.
NODE_REPOSITORY = """
query($id: ID!) {
  node(id: $id) {
    ... on RepositoryNode { repository { name owner { login } } }
  }
}
"""

#: The GraphQL writes that name no node outside the configured owner: the
#: configured Project's own operations. With ``NODE_MUTATIONS`` they are the
#: only mutation documents GuildBotics sends, so they are matched as written.
PROJECT_MUTATIONS = frozenset(
    {
        UPDATE_PROJECT_ITEM_STATUS,
        UPDATE_PROJECT_FIELD_OPTIONS,
        CREATE_PROJECT_FIELD,
    }
)

_READ_METHODS = frozenset({"GET", "HEAD"})
#: A GraphQL document with a mutation operation names the keyword.
_MUTATION = re.compile(r"(?<![_0-9A-Za-z])mutation(?![_0-9A-Za-z])")
#: A GitHub App renewing its installation token.
_TOKEN_RENEWAL = re.compile(r"/app/installations/[0-9]+/access_tokens")
_REPOSITORY_PATH_MIN_SEGMENTS = 4


async def check_request(
    scope: str, base_url: httpx.URL, request: httpx.Request, client: httpx.AsyncClient
) -> None:
    """Refuse ``request`` unless it reads or writes within ``scope``.

    Args:
        scope: The configured owner.
        base_url: The client's API base URL. Only its host is sent to, and the
            request's path starts with its path (``/api/v3`` on GitHub
            Enterprise Server).
        request: The request about to be sent.
        client: The client sending it, which reads where a node it writes lives.

    Raises:
        RepositoryScopeError: If the request goes to another host or writes
            outside the configured owner.
    """
    if (request.url.scheme, request.url.netloc) != (base_url.scheme, base_url.netloc):
        refuse(scope, f"{request.method} {request.url.scheme}://{request.url.host}")
    if request.method in _READ_METHODS:
        return
    path = request.url.raw_path.decode("ascii").split("?", 1)[0]
    base_path = base_url.raw_path.decode("ascii").rstrip("/")
    if base_path and path.startswith(f"{base_path}/"):
        path = path.removeprefix(base_path)
    if path == "/graphql":
        permitted = await _graphql_permitted(scope, request.content, client)
    elif _TOKEN_RENEWAL.fullmatch(path):
        permitted = True
    else:
        segments = path.split("/")
        permitted = (
            len(segments) >= _REPOSITORY_PATH_MIN_SEGMENTS
            and segments[:2] == ["", "repos"]
            and in_scope(scope, segments[2], segments[3])
        )
    if not permitted:
        refuse(scope, f"{request.method} {path}")


async def _graphql_permitted(
    scope: str, content: bytes, client: httpx.AsyncClient
) -> bool:
    try:
        body = json.loads(content)
    except ValueError:
        return False
    if not isinstance(body, dict) or not isinstance(document := body.get("query"), str):
        return False
    if (variable := NODE_MUTATIONS.get(document)) is not None:
        variables = body.get("variables")
        node = variables.get(variable) if isinstance(variables, dict) else None
        return isinstance(node, str) and await _node_in_scope(scope, node, client)
    return document in PROJECT_MUTATIONS or not _MUTATION.search(document)


async def _node_in_scope(scope: str, node: str, client: httpx.AsyncClient) -> bool:
    response = await client.post(
        "/graphql", json={"query": NODE_REPOSITORY, "variables": {"id": node}}
    )
    try:
        repository = response.json()["data"]["node"]["repository"]
        owner, name = repository["owner"]["login"], repository["name"]
    except (ValueError, LookupError, TypeError):
        return False  # not a node in any repository GitHub would name
    return (
        isinstance(owner, str)
        and isinstance(name, str)
        and in_scope(scope, owner, name)
    )
