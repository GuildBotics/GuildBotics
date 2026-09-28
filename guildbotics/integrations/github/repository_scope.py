"""Which GitHub repositories a member writes to.

A fine-grained token or a GitHub App limited to selected repositories does not
draw this line: GitHub accepts issues, comments, reviews, and reactions on any
public repository from them. So GuildBotics draws it itself. A member writes
only to repositories of the owner the project is configured with, and every
request of a member's GitHub client passes the judgment here before it is
sent (``get_async_client``), as does every push of a member's git.

Reads are not limited by owner, but nothing is sent to a host other than the
client's API. What is not recognized as a read or as a write to the
configured owner is refused, so a new kind of write is refused until it is
classified here.
"""

from __future__ import annotations

import json
import re

import httpx

from guildbotics.entities.team import Project, Service
from guildbotics.observability.diagnostics_events import record_correlated_event

#: An owner or repository name as a URL of the code host carries it.
NAME = re.compile(r"[A-Za-z0-9_.-]+")

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

#: The GraphQL writes: the configured Project's own operations. They are the
#: only mutation documents GuildBotics sends, so they are matched as written.
PROJECT_MUTATIONS = frozenset(
    {
        ADD_PROJECT_ITEM,
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


class RepositoryScopeError(RuntimeError):
    """A write outside the repositories of the configured owner."""


def configured_owner(project: Project) -> str:
    """The owner whose repositories the project's members write to."""
    code = project.get_service_config(Service.CODE_HOSTING_SERVICE)
    ticket = project.get_service_config(Service.TICKET_MANAGER)
    return str(code.get("owner") or ticket.get("owner") or "")


def check_repository(scope: str, owner: str, repository: str) -> None:
    """Refuse a write to ``owner/repository`` unless ``scope`` owns it.

    Raises:
        RepositoryScopeError: If the repository is not the configured owner's.
    """
    if not _in_scope(scope, owner, repository):
        _refuse(scope, "/".join(name for name in (owner, repository) if name))


def check_request(scope: str, base_url: httpx.URL, request: httpx.Request) -> None:
    """Refuse ``request`` unless it reads or writes within ``scope``.

    Args:
        scope: The configured owner.
        base_url: The client's API base URL. Only its host is sent to, and the
            request's path starts with its path (``/api/v3`` on GitHub
            Enterprise Server).
        request: The request about to be sent.

    Raises:
        RepositoryScopeError: If the request goes to another host or writes
            outside the configured owner.
    """
    if (request.url.scheme, request.url.netloc) != (base_url.scheme, base_url.netloc):
        _refuse(scope, f"{request.method} {request.url.scheme}://{request.url.host}")
    if request.method in _READ_METHODS:
        return
    path = request.url.raw_path.decode("ascii").split("?", 1)[0]
    base_path = base_url.raw_path.decode("ascii").rstrip("/")
    if base_path and path.startswith(f"{base_path}/"):
        path = path.removeprefix(base_path)
    if path == "/graphql":
        permitted = _graphql_permitted(request.content)
    elif _TOKEN_RENEWAL.fullmatch(path):
        permitted = True
    else:
        segments = path.split("/")
        permitted = (
            len(segments) >= _REPOSITORY_PATH_MIN_SEGMENTS
            and segments[:2] == ["", "repos"]
            and _in_scope(scope, segments[2], segments[3])
        )
    if not permitted:
        _refuse(scope, f"{request.method} {path}")


def _in_scope(scope: str, owner: str, repository: str) -> bool:
    return (
        bool(scope)
        and all(
            NAME.fullmatch(name) and name not in {".", ".."}
            for name in (owner, repository)
        )
        and owner.casefold() == scope.casefold()
    )


def _graphql_permitted(content: bytes) -> bool:
    try:
        body = json.loads(content)
    except ValueError:
        return False
    document = body.get("query") if isinstance(body, dict) else None
    if not isinstance(document, str):
        return False
    return document in PROJECT_MUTATIONS or not _MUTATION.search(document)


def _refuse(scope: str, target: str) -> None:
    record_correlated_event(
        event_type="github.scope_refused",
        default_source="github",
        attributes={"github.scope_owner": scope},
        payload={"target": target, "scope_owner": scope},
    )
    raise RepositoryScopeError(
        f"GitHub writes are limited to repositories of '{scope}', "
        f"the owner the project is configured with: refused {target}."
        if scope
        else f"No GitHub owner is configured for the project: refused {target}."
    )
