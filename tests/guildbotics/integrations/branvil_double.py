"""Branvil, as far as GuildBotics' ``branvil`` provider asks it: a Python
double of the HTTP API v1 the Branvil repository documents in
``docs/api.md``, behind an ``httpx.MockTransport``.

It keeps what it is sent and answers as the API says: an entry id is
answered only under the item it belongs to, a branch only by its exact
name, a review or an inline comment only on the current head
(``409 head_moved``) and only on a file the pull request changes
(``400 invalid``; every branch commit changes ``CHANGED``), a label only if
the ticket's repository defines it (``400 label_undefined``), a ticket's
relation only to a ticket of its workspace (``400 invalid``), its move into
``in_progress`` only once its blockers are closed (``409 blocked``), a new pull
request's link that closes only to a ticket of its repository (``422
unsupported``), and an artifact only if it is a zip archive. A repository
(``<workspace>/<name>``) numbers its tickets from 1, apart from its pull
requests, and a ticket is always in a lane of its workspace's board: a new one in
the lane it names, ``new`` if none, and a lane that is not one (``null``
included) is refused (``400 invalid``). A ticket is not ``interactive`` until a
test makes it so, as a human does in Branvil's GUI. A ticket keeps
its ``parent`` and ``blocked_by`` as URLs and is answered with every relation
shown from both sides. It does not hold writes to the member's scope -- the
provider refuses those before any request. The author's own review is only
a ``comment`` (``400 invalid`` otherwise); a verdict or a new inline comment
consumes its author's review request, a reply does not; marking a pull
request ready for review (or opening it as one) sets ``ready_at``; a merge
closes the tickets the pull request ``closes`` and moves them to ``done``; a
workspace board's closed items are the closed tickets and pull requests of the
workspace's repositories; a gone branch's commit is ``""`` with no
``behind_by``; and the dependency alerts of a repository's upstream are read
only for a repository that has one (``422 unsupported`` otherwise), by
``state`` and by ``id``.

Its administration answers as Branvil's public admin API does: a token is
its member's, the owner's (the admin token GuildBotics holds, labelled
``app:GuildBotics``) administers members, their tokens and accounts,
workspaces, entry links and the CI runner's image loads, and an agent may
not (``403 forbidden``). A
token issued with ``replace`` revokes the member's tokens of its label; the
labels Branvil uses itself are refused. The consent screen is
:meth:`BranvilDouble.authorize`, which answers the owner's choice with where
Branvil sends the browser; its code is exchanged once, for the redirect it
was given and the verifier of its challenge, for a new admin token.

``writes`` lists the writes it accepted, as ``"<method> <path>"``.
:func:`refuse` has it refuse connections, as a Branvil that is restarting
does, while the clients wait on a virtual :class:`Clock`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from base64 import urlsafe_b64encode
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from itertools import count
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import httpx

BASE = "http://127.0.0.1:7878"
MEMBER = "aiko"
TOKEN = "bvt_aiko"
#: Branvil's owner, the GuildBotics human.
OWNER = "otota"
#: The owner's token Branvil issued GuildBotics on the owner's consent.
ADMIN = "bvt_admin"
APP_LABEL = "app:GuildBotics"
SCOPE = "acme"
REPO = f"{SCOPE}/demo"
#: Another workspace, outside the scope.
THEIRS = "other"
THEIR_REPO = f"{THEIRS}/demo"
LABEL = "bug"
#: The labels each repository defines.
LABELS = {REPO: [LABEL], THEIR_REPO: []}
LANES = ("new", "ready", "in_progress", "done")
#: The file every commit on a branch changes.
CHANGED = "a.py"
_START = datetime(2026, 10, 14, 10, tzinfo=UTC)


class Refusal(Exception):
    """What Branvil answers with an error."""

    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _missing(what: str) -> Refusal:
    return Refusal(404, "not_found", f"{what} was not found.")


class BranvilDouble:
    def __init__(self) -> None:
        self.writes: list[str] = []
        #: The branches of ``REPO``: name -> commit.
        self.branches: dict[str, str] = {"main": "main-sha"}
        self.tickets: dict[str, dict[str, Any]] = {}
        #: The roots of the resolved review threads.
        self.resolved: set[str] = set()
        #: Where each thread is: its root's path, line, side, and commit.
        self.positions: dict[str, dict[str, Any]] = {}
        self.pulls: dict[str, dict[str, Any]] = {}
        #: Each item's entries, by its URL: comments, reviews, inline comments.
        self.entries: dict[str, dict[str, list[dict[str, Any]]]] = {}
        #: Each commit's checks; a commit without is checked as passing.
        self.checks: dict[str, list[dict[str, Any]]] = {}
        self.logs: dict[str, str] = {}
        #: What the checks of every pull request say of its base.
        self.behind_by = 0
        self.checks_expected = False
        #: Whether making a pull request a draft is refused.
        self.refuse_draft = False
        self.artifacts: dict[tuple[str, str], bytes] = {}
        #: The repositories with an upstream, and the dependency alerts its
        #: host reports for ``REPO``.
        self.upstreams = {REPO}
        self.alerts: list[dict[str, Any]] = []
        self._ticks = count()
        self._ids = count(1)
        self._revisions = count(1)
        self.members: dict[str, dict[str, Any]] = {}
        #: Every token, by its value: its member, label and id.
        self.tokens: dict[str, dict[str, str]] = {}
        #: The members' accounts on other hosts: (member, host) -> credential.
        self.accounts: dict[tuple[str, str], dict[str, Any]] = {}
        #: The members' pictures.
        self.avatars: dict[str, bytes] = {}
        self.workspaces = [SCOPE, THEIRS]
        #: The consent codes not yet exchanged.
        self._codes: dict[str, dict[str, str]] = {}
        #: The latest image load into the CI runner, as Branvil reports it.
        self.operation: dict[str, Any] | None = None
        #: How a running load ends after ``ci_reads`` more reads of the CI
        #: settings: the references it loaded, or why it failed.
        self.ci_outcome: list[str] | str = []
        self.ci_reads = 1
        self.member(OWNER, "owner")
        self.member(MEMBER, "agent")
        self.issue(OWNER, APP_LABEL, ADMIN)
        self.issue(MEMBER, "cli", TOKEN)
        theirs = self._ticket(THEIR_REPO, "Theirs", "", [], author="them")
        self.their_ticket = theirs["url"]
        self.their_comment = self._entry(theirs["url"], "comments", "them", "Hi")["id"]

    # -- what the contracts do besides the API --------------------------------

    def member(self, name: str, kind: str) -> dict[str, Any]:
        self.members[name] = {
            "name": name,
            "display_name": name.title(),
            "kind": kind,
            "accounts": [],
        }
        return self.members[name]

    def issue(self, member: str, label: str, token: str = "") -> dict[str, str]:
        """A token of ``member`` labelled ``label``."""
        token = token or f"bvt_{next(self._ids)}"
        self.tokens[token] = {"member": member, "label": label, "id": f"tok_{token}"}
        return self.tokens[token]

    def labels(self, member: str) -> list[str]:
        """The labels of ``member``'s tokens."""
        return sorted(t["label"] for t in self.tokens.values() if t["member"] == member)

    def authorize(self, url: str, allow: bool = True) -> str:
        """Where Branvil sends the browser once the owner answers the consent
        screen at ``url``."""
        parts = urlsplit(url)
        query = dict(parse_qsl(parts.query))
        assert f"{parts.scheme}://{parts.netloc}{parts.path}" == f"{BASE}/-/authorize"
        assert query["code_challenge_method"] == "S256"
        redirect = query["redirect_uri"]
        assert urlsplit(redirect).hostname in ("127.0.0.1", "localhost", "::1")
        if not allow:
            answer = {"error": "access_denied", "state": query["state"]}
        else:
            code = f"code_{next(self._ids)}"
            self._codes[code] = query
            answer = {"code": code, "state": query["state"]}
        return f"{redirect}?{urlencode(answer)}"

    def branch(self, name: str) -> None:
        self.branches[name] = f"{name.replace('/', '-')}-sha"

    def advance(self, name: str) -> None:
        self.branches[name] = f"{name.replace('/', '-')}-sha{next(self._revisions)}"

    def assign(self, url: str, lane: str = "ready", name: str = MEMBER) -> None:
        self.tickets[url].update(
            lane=lane, assignee={"name": name, "assigned_at": self.now()}
        )

    def add_artifact(self, sha: str, name: str, content: bytes) -> None:
        if content[:4] not in (b"PK\x03\x04", b"PK\x05\x06"):
            raise ValueError("Branvil keeps only zip archives as artifacts.")
        self.artifacts[sha, name] = content

    def alert(self, state: str = "open", patched: str | None = "4.17.21") -> str:
        """A dependency alert the upstream host reports for ``REPO``; its id."""
        number = str(len(self.alerts) + 1)
        self.alerts.append(
            {
                "id": number,
                "state": state,
                "url": f"https://github.com/acme/demo/security/dependabot/{number}",
                "package": "lodash",
                "ecosystem": "npm",
                "manifest_path": "package-lock.json",
                "severity": "high",
                "identifiers": [{"type": "GHSA", "value": f"GHSA-{number}"}],
                "summary": "Command Injection in lodash",
                "description": "...",
                "affected_versions": "< 4.17.21",
                "patched_version": patched,
                "created_at": self.now(),
                "updated_at": self.now(),
            }
        )
        return number

    def now(self) -> str:
        moment = _START + timedelta(seconds=next(self._ticks))
        return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def comment(self, url: str, author: str, body: str, **meta: Any) -> dict[str, Any]:
        """Someone's comment on the ticket or pull request ``url``."""
        return self._entry(url, "comments", author, body, meta=meta or None)

    def inline(
        self, url: str, author: str, body: str, reply_to: str | None = None
    ) -> dict[str, Any]:
        """Someone's inline comment on the pull request's head, or reply."""
        pull = self.pulls[url]
        if reply_to is not None:
            return self._entry(url, "inline", author, body, reply_to_id=reply_to)
        self._consume(pull, author)
        commit = self._pull(pull)["head_sha"]
        return self._root(url, author, body, commit=commit, path=CHANGED, line=1)

    def review(
        self, url: str, author: str, body: str = "", verdict: str = "comment"
    ) -> dict[str, Any]:
        """Someone's review of the pull request's head."""
        pull = self.pulls[url]
        self._consume(pull, author)
        commit = self._pull(pull)["head_sha"]
        return self._entry(url, "reviews", author, body, verdict=verdict, commit=commit)

    def ready(self, url: str) -> None:
        """A human marks the pull request ready for review."""
        self.pulls[url].update(draft=False, ready_at=self.now())

    def make_theirs(self, url: str) -> None:
        """Someone else is the pull request's author."""
        self.pulls[url]["author"] = "them"

    def merge(self, url: str) -> None:
        """A human merges the pull request; the tickets it closes are done."""
        at = self.now()
        self.pulls[url].update(state="closed", merged=True, closed_at=at, merged_at=at)
        for link in self.pulls[url]["tickets"]:
            ticket = self.tickets.get(link["url"])
            if link["closes"] and ticket is not None:
                ticket.update(
                    state="closed", state_reason="completed", closed_at=at, lane="done"
                )

    # -- the API ----------------------------------------------------------------

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode("ascii").partition("?")[0]
        parts = [unquote(part) for part in path.removeprefix("/api/v1/").split("/")]
        if parts == ["health"]:
            return httpx.Response(200, json={"service": "branvil", "api": 1})
        # A picture is put as it is; everything else is JSON.
        body: Any = request.content
        if not path.endswith("/avatar"):
            body = json.loads(request.content) if request.content else {}
        try:
            if parts == ["apps", "token"]:
                status, answer = self._exchange(body)
            else:
                bearer = request.headers.get("authorization", "")
                token = self.tokens.get(bearer.removeprefix("Bearer "))
                if token is None:
                    raise Refusal(401, "unauthorized")
                status, answer = self._route(
                    request.method,
                    parts,
                    body,
                    dict(request.url.params),
                    self.members[token["member"]],
                )
        except Refusal as refusal:
            error = {"code": refusal.code, "message": refusal.message}
            return httpx.Response(refusal.status, json={"error": error})
        if request.method != "GET":
            self.writes.append(f"{request.method} {path}")
        if answer is None:
            return httpx.Response(status)
        if isinstance(answer, bytes):
            return httpx.Response(status, content=answer)
        return httpx.Response(status, json=answer)

    def _route(
        self,
        method: str,
        parts: list[str],
        body: Any,
        params: dict[str, str],
        me: dict[str, Any],
    ) -> tuple[int, Any]:
        owner = me["kind"] == "owner"
        match method, parts:
            case _, ["me"]:
                return 200, me
            case _, ["members", *rest] if owner or rest[:1] == [me["name"]]:
                return self._members(method, rest, body, params)
            case "GET", ["workspaces"]:
                return 200, self._page(
                    [{"name": name, "lanes": list(LANES)} for name in self.workspaces],
                    params,
                )
            case "POST", ["workspaces"] if owner:
                if body["name"].casefold() in map(str.casefold, self.workspaces):
                    raise Refusal(409, "conflict", f"{body['name']} exists.")
                self.workspaces.append(body["name"])
                return 201, {"name": body["name"], "lanes": list(LANES)}
            case "POST", ["session", "links"] if owner:
                code = next(self._ids)
                return 201, {"url": f"{BASE}/-/enter#code{code}", "expires_at": ""}
            case "POST", ["admin", "ci", "images", "load"] if owner:
                return 202, self._load(body["path"])
            case "GET", ["admin", "ci"] if owner:
                return 200, self._ci()
            case (_, ["members" | "session" | "admin", *_]) | ("POST", ["workspaces"]):
                raise Refusal(403, "forbidden", "The member may not do that.")
        match parts:
            case ["pulls"]:
                return 200, self._page([self._patrol(p) for p in self._involving()])
            case ["repos", repo, *rest]:
                return self._repository(method, repo, rest, body, params)
            case ["workspaces", workspace, *rest] if workspace in self.workspaces:
                return 200, self._workspace(workspace, rest, params)
        raise _missing("/".join(parts))

    def _members(
        self, method: str, rest: list[str], body: Any, params: dict
    ) -> tuple[int, Any]:
        match method, rest:
            case "GET", []:
                return 200, self._page(list(self.members.values()), params)
            case "POST", []:
                if body["name"] in self.members:
                    raise Refusal(409, "conflict", f"{body['name']} exists.")
                member = self.member(body["name"], body["kind"])
                member["display_name"] = body.get("display_name") or body["name"]
                return 201, member
            case _, [name, *more] if name not in self.members:
                raise _missing(f"Member {name}")
            case "GET", [name]:
                return 200, self.members[name]
            case "GET", [name, "tokens"]:
                issued = [
                    {"id": t["id"], "label": t["label"], "created_at": self.now()}
                    for t in self.tokens.values()
                    if t["member"] == name
                ]
                return 200, self._page(issued, params)
            case "POST", [name, "tokens"]:
                label = body["label"]
                if label in ("owner-file", "auto") or label.startswith("app:"):
                    raise Refusal(400, "invalid", f"The label {label} is Branvil's.")
                if body.get("replace"):
                    self._revoke(name, label)
                token, issued = self._issued(name, label)
                return 201, {
                    "token": token,
                    "id": issued["id"],
                    "label": label,
                    "created_at": self.now(),
                }
            case "DELETE", [name, "tokens", token_id]:
                self.tokens = {
                    v: t
                    for v, t in self.tokens.items()
                    if (t["member"], t["id"]) != (name, token_id)
                }
                return 204, None
            case "PUT", [name, "avatar"]:
                self.avatars[name] = body
                return 200, self.members[name]
            case "PUT", [name, "accounts", host]:
                assert set(body) in ({"token"}, {"app"}), body
                self.accounts[name, host] = body
                return 200, self.members[name]
        raise _missing("/".join(rest))

    def _load(self, path: str) -> dict[str, Any]:
        """Begin loading the archive at ``path`` into the CI runner."""
        if not path.startswith("/"):
            raise Refusal(400, "invalid", "Give the absolute path of the archive.")
        if self.operation and self.operation["state"] == "running":
            raise Refusal(409, "conflict", "Another image operation is running.")
        self.operation = {
            "kind": "load",
            "target": path,
            "state": "running",
            "detail": None,
            "started_at": self.now(),
            "finished_at": None,
            "loaded": [],
        }
        return {"operation": dict(self.operation)}

    def _ci(self) -> dict[str, Any]:
        """The CI settings, as far as GuildBotics reads them."""
        operation = self.operation
        if operation and operation["state"] == "running":
            self.ci_reads -= 1
            if self.ci_reads <= 0:
                failed = isinstance(self.ci_outcome, str)
                operation.update(
                    state="failed" if failed else "succeeded",
                    detail=self.ci_outcome if failed else None,
                    finished_at=self.now(),
                    loaded=[]
                    if failed
                    else [{"reference": r} for r in self.ci_outcome],
                )
        return {"operation": dict(operation) if operation else None}

    def _issued(self, name: str, label: str) -> tuple[str, dict[str, str]]:
        token = f"bvt_{next(self._ids)}"
        return token, self.issue(name, label, token)

    def _revoke(self, name: str, label: str) -> None:
        self.tokens = {
            v: t
            for v, t in self.tokens.items()
            if (t["member"], t["label"]) != (name, label)
        }

    def _exchange(self, body: dict) -> tuple[int, Any]:
        """A consent code, once, for a new admin token."""
        grant = self._codes.pop(body["code"], None)
        challenge = urlsafe_b64encode(sha256(body["code_verifier"].encode()).digest())
        if (
            grant is None
            or grant["redirect_uri"] != body["redirect_uri"]
            or grant["code_challenge"] != challenge.rstrip(b"=").decode()
        ):
            raise Refusal(400, "invalid", "The code is not valid.")
        label = f"app:{grant['client']}"
        self._revoke(OWNER, label)
        token, issued = self._issued(OWNER, label)
        return 201, {
            "token": token,
            "id": issued["id"],
            "label": label,
            "member": OWNER,
        }

    def _repository(
        self, method: str, repo: str, rest: list[str], body: dict, params: dict
    ) -> tuple[int, Any]:
        if repo not in (REPO, THEIR_REPO):
            raise _missing(repo)
        match method, rest:
            case "GET", []:
                return 200, {
                    "path": repo,
                    "default_branch": "main",
                    "labels": LABELS[repo],
                }
            case "POST", ["tickets"]:
                labels = _defined(repo, body["labels"])
                ticket = self._ticket(
                    repo, body["title"], body["body"], labels, _lane(body, "new")
                )
                return 201, self._view(ticket)
            case _, ["tickets", number, *more]:
                url = f"{BASE}/{repo}/-/tickets/{number}"
                if url not in self.tickets:
                    raise _missing(url)
                return self._ticket_request(method, url, more, body, params)
            case "GET", ["branches", name] if name in self.branches:
                return 200, {"name": name, "sha": self.branches[name]}
            case "POST", ["pulls"]:
                return self._new_pull(repo, body)
            case "GET", ["pulls"]:
                pulls = [
                    self._pull(pull)
                    for pull in self.pulls.values()
                    if pull["repository"] == repo
                    and params.get("state") in (None, pull["state"])
                    and params.get("head") in (None, pull["head"])
                ]
                return 200, self._page(pulls, params)
            case _, ["pulls", number, *more]:
                url = f"{BASE}/{repo}/-/pulls/{number}"
                if url not in self.pulls:
                    raise _missing(url)
                return self._pull_request(method, url, more, body, params)
            case "GET", ["jobs", attempt, "log"] if attempt in self.logs:
                log = self.logs[attempt][-int(params["tail_bytes"]) :]
                truncated = len(log) < len(self.logs[attempt])
                return 200, {"log": log, "log_bytes": len(log), "truncated": truncated}
            case "GET", ["upstream", "dependency-alerts", *alert]:
                return 200, self._alerts(repo, alert, params)
            case "GET", ["commits", sha, "artifacts", name] if (
                sha,
                name,
            ) in self.artifacts:
                return 200, self.artifacts[sha, name]
        raise _missing("/".join(rest))

    def _alerts(self, repo: str, alert: list[str], params: dict) -> Any:
        if repo not in self.upstreams:
            raise Refusal(422, "unsupported", f"{repo} has no upstream.")
        if alert:
            for found in self.alerts:
                if [found["id"]] == alert:
                    return found
            raise _missing(f"Alert {alert[0]}")
        state = params.get("state", "open")
        if state not in ("open", "resolved", "dismissed"):
            raise Refusal(400, "invalid", "state is open, resolved, or dismissed.")
        return self._page([a for a in self.alerts if a["state"] == state], params)

    def _pull_request(
        self, method: str, url: str, rest: list[str], body: dict, params: dict
    ) -> tuple[int, Any]:
        pull = self.pulls[url]
        entries = self.entries[url]
        match method, rest:
            case "GET", []:
                return 200, self._pull(pull)
            case "PATCH", []:
                if self.refuse_draft and "draft" in body:
                    raise Refusal(403, "forbidden", "The member may not do that.")
                for key in ("title", "body", "draft", "tickets"):
                    if key in body:
                        pull[key] = body[key]
                if body.get("draft") is False:
                    self.ready(url)
                return 200, self._pull(pull)
            case "GET", ["activity"]:
                return 200, self._activity(url)
            case "GET", [("comments" | "reviews") as kind]:
                return 200, self._page(entries[kind], params)
            case "POST", ["comments"]:
                return 201, self.comment(
                    url, MEMBER, body["body"], **body.get("meta") or {}
                )
            case "POST", ["reviews"]:
                if body["verdict"] != "comment" and pull["author"].casefold() == MEMBER:
                    raise Refusal(
                        400,
                        "invalid",
                        "The author cannot approve or request changes on their"
                        " own pull request; only comment.",
                    )
                self._at_head(pull, body["commit"])
                fields = {key: body[key] for key in ("verdict", "commit")}
                self._consume(pull, MEMBER)
                review = self._entry(url, "reviews", MEMBER, body["body"], **fields)
                keys = ("id", "verdict", "commit", "submitted_at", "url")
                return 201, {key: review[key] for key in keys}
            case "POST", ["inline-comments"]:
                self._at_head(pull, body["commit"])
                if body["path"] != CHANGED:
                    raise Refusal(
                        400,
                        "invalid",
                        f"{body['path']} is not changed by !{pull['number']}.",
                    )
                fields = {k: v for k, v in body.items() if k != "body"}
                self._consume(pull, MEMBER)
                return 201, self._root(url, MEMBER, body["body"], **fields)
            case "GET", ["threads"]:
                return 200, self._page(self._threads(url), params)
            case "GET", ["threads", root, "comments"]:
                return 200, self._page(self._thread(url, root)["comments"], params)
            case "PUT", ["threads", root, "resolved"]:
                self._thread(url, root)
                (self.resolved.add if body["resolved"] else self.resolved.discard)(root)
                return 200, self._thread(url, root)
            case "POST", ["threads", root, "replies"]:
                self._thread(url, root)
                reply = self._entry(
                    url, "inline", MEMBER, body["body"], reply_to_id=root
                )
                return 201, reply
            case "POST", ["reactions"]:
                return 200, self._react(url, ("comments", "inline", "reviews"), body)
            case "GET", ["files"]:
                return 200, self._page([_CHANGE], params)
            case "GET", ["checks"]:
                view = self._pull(pull)
                checks = self.checks.get(view["head_sha"], [_PASSED])
                return 200, {
                    "head_sha": view["head_sha"],
                    "base_sha": view["base_sha"],
                    "behind_by": self.behind_by
                    if view["head_sha"] and view["base_sha"]
                    else None,
                    "checks": checks,
                    "checks_expected": self.checks_expected,
                    "base_checks": 0,
                }
        raise _missing("/".join(rest))

    def _ticket_request(
        self, method: str, url: str, rest: list[str], body: dict, params: dict
    ) -> tuple[int, Any]:
        ticket = self.tickets[url]
        match method, rest:
            case "GET", []:
                return 200, self._view(ticket)
            case "PATCH", []:
                added = _defined(ticket["repository"], body.get("add_labels", []))
                blockers = body.get("add_blocked_by", [])
                for related in filter(None, [body.get("parent"), *blockers]):
                    self._related(url, related)
                state_changed = body.get("state", ticket["state"]) != ticket["state"]
                for key in ("title", "body", "state", "state_reason", "parent"):
                    if key in body:
                        ticket[key] = body[key]
                if state_changed:
                    ticket["closed_at"] = (
                        self.now() if ticket["state"] == "closed" else None
                    )
                # A ticket closed on the board leaves its lane for done.
                if state_changed and ticket["state"] == "closed":
                    ticket["lane"] = "done"
                for key, more in (("labels", added), ("blocked_by", blockers)):
                    removed = body.get(f"remove_{key}", [])
                    kept = [name for name in ticket[key] if name not in removed]
                    ticket[key] = kept + [name for name in more if name not in kept]
                return 200, {
                    "ticket": self._view(ticket),
                    "state_changed": state_changed,
                }
            case "GET", ["comments"]:
                return 200, self._page(self.entries[url]["comments"], params)
            case "POST", ["comments"]:
                return 201, self.comment(
                    url, MEMBER, body["body"], **body.get("meta") or {}
                )
            case "POST", ["reactions"]:
                return 200, self._react(url, ("comments",), body)
            case "GET", ["pulls"]:
                return 200, self._page(self._linked(url), params)
            case "PUT", ["board"]:
                lane = _lane(body)
                if lane == "in_progress" != ticket["lane"] and any(
                    self.tickets[blocker]["state"] == "open"
                    for blocker in ticket["blocked_by"]
                ):
                    raise Refusal(409, "blocked", f"{url} waits for its blockers.")
                ticket["lane"] = lane
                return 200, self._view(ticket)
        raise _missing("/".join(rest))

    def _workspace(self, workspace: str, rest: list[str], params: dict) -> Any:
        def of(item: dict[str, Any]) -> bool:
            return item["repository"].partition("/")[0] == workspace

        if rest == ["tickets"]:
            assert params["mentions"] == "me"
            cards = [
                self._view(ticket)
                | {
                    "comments": self.entries[url]["comments"],
                    "pull_requests": self._linked(url),
                }
                for url, ticket in self.tickets.items()
                if of(ticket)
                and ticket["state"] == params.get("state", ticket["state"])
                and any(
                    MEMBER in entry["mentions"]
                    for entry in [self._view(ticket), *self.entries[url]["comments"]]
                )
            ]
            return self._page(cards, params)

        if rest == ["board", "cards"]:
            lanes = params["lane"].split(",")
            cards = [
                self._view(ticket)
                | {
                    "comments": self.entries[url]["comments"],
                    "pull_requests": self._linked(url),
                }
                for url, ticket in self.tickets.items()
                if of(ticket)
                and ticket["lane"] in lanes
                and ticket["state"] == params.get("state", ticket["state"])
                and (ticket["assignee"] or {}).get("name") == MEMBER
            ]
            return self._page(
                sorted(cards, key=lambda card: LANES.index(card["lane"])), params
            )
        if rest == ["board", "closed"]:
            closed = [
                {
                    "kind": kind,
                    "url": item["url"],
                    "title": item["title"],
                    "label": item["label"]
                    if kind == "pull_request"
                    else f"#{item['key']}",
                    "closed_at": item["closed_at"],
                    "merged": bool(item.get("merged")),
                }
                for kind, items in (
                    ("ticket", self.tickets),
                    ("pull_request", self.pulls),
                )
                for item in items.values()
                if of(item)
                and item["closed_at"]
                and params["since"] <= item["closed_at"] <= params["until"]
            ]
            return self._page(closed, params)
        return {"name": workspace, "lanes": list(LANES)}

    # -- items ------------------------------------------------------------------

    def _ticket(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str],
        lane: str = "new",
        author: str = MEMBER,
    ) -> dict[str, Any]:
        number = 1 + sum(t["repository"] == repo for t in self.tickets.values())
        url = f"{BASE}/{repo}/-/tickets/{number}"
        self.tickets[url] = {
            "url": url,
            "repository": repo,
            "key": str(number),
            "title": title,
            "body": body,
            "state": "open",
            "state_reason": None,
            "author": author,
            "assignee": None,
            "labels": labels,
            "lane": lane,
            "interactive": False,
            "parent": None,
            "blocked_by": [],
            "created_at": self.now(),
            "closed_at": None,
        }
        self.entries[url] = {"comments": []}
        return self.tickets[url]

    def _related(self, url: str, other: str) -> None:
        """Refuse a relation of the ticket ``url`` to ``other`` unless both
        are tickets of one workspace."""
        if other not in self.tickets:
            raise _missing(other)
        workspaces = {
            self.tickets[ticket]["repository"].partition("/")[0]
            for ticket in (url, other)
        }
        if len(workspaces) > 1:
            raise Refusal(400, "invalid", f"{other} is of another workspace.")

    def _view(self, ticket: dict[str, Any]) -> dict[str, Any]:
        """The ticket as Branvil answers it, its relations shown from both
        sides."""
        url = ticket["url"]

        def relative(other: str) -> dict[str, Any]:
            keys = ("url", "repository", "key", "title", "state", "state_reason")
            return {key: self.tickets[other][key] for key in keys}

        return ticket | {
            "mentions": self._mentions(ticket["body"]),
            "parent": ticket["parent"] and relative(ticket["parent"]),
            "children": [
                relative(u) for u, t in self.tickets.items() if t["parent"] == url
            ],
            "blocked_by": [relative(blocker) for blocker in ticket["blocked_by"]],
            "blocks": [
                relative(u) for u, t in self.tickets.items() if url in t["blocked_by"]
            ],
        }

    def _new_pull(self, repo: str, body: dict) -> tuple[int, Any]:
        base = body["base"] or "main"
        if body["head"] not in self.branches:
            raise _missing(f"Branch {body['head']}")
        for link in body["tickets"]:
            ticket = self.tickets.get(link["url"])
            if link["url"].startswith(f"{BASE}/") and ticket is None:
                raise _missing(link["url"])
            if link["closes"] and (ticket or {}).get("repository") != repo:
                raise Refusal(
                    422, "unsupported", "A merge closes only this repository's ticket."
                )
        for pull in self.pulls.values():
            if pull["state"] == "open" and (pull["head"], pull["base"]) == (
                body["head"],
                base,
            ):
                return 200, {"created": False, "pull": self._pull(pull)}
        number = 1 + sum(p["repository"] == repo for p in self.pulls.values())
        url = f"{BASE}/{repo}/-/pulls/{number}"
        self.pulls[url] = {
            "url": url,
            "repository": repo,
            "number": number,
            "label": f"!{number}",
            "title": body["title"],
            "body": body["body"],
            "state": "open",
            "merged": False,
            "draft": body["draft"],
            "author": MEMBER,
            "ready_at": None if body["draft"] else self.now(),
            "head": body["head"],
            "head_repository": repo,
            "base": base,
            "reviewers": [],
            "tickets": body["tickets"],
            "labels": [],
            "changed_files": 1,
            "created_at": self.now(),
            "updated_at": self.now(),
            "closed_at": None,
            "merged_at": None,
        }
        self.entries[url] = {"comments": [], "reviews": [], "inline": []}
        return 201, {"created": True, "pull": self._pull(self.pulls[url])}

    def _pull(self, pull: dict[str, Any]) -> dict[str, Any]:
        """The pull request with its branches' tips as Git has them now."""
        return pull | {
            "head_sha": self.branches.get(pull["head"], ""),
            "base_sha": self.branches.get(pull["base"], ""),
        }

    def _patrol(self, pull: dict[str, Any]) -> dict[str, Any]:
        return self._pull(pull) | {"activity": self._activity(pull["url"])}

    def _involving(self) -> list[dict[str, Any]]:
        return sorted(
            (
                pull
                for pull in self.pulls.values()
                if pull["state"] == "open"
                and not pull["draft"]
                and (
                    pull["author"] == MEMBER
                    or MEMBER in map(str.casefold, pull["reviewers"])
                    or any(
                        entry["author"] == MEMBER
                        for kind in ("reviews", "inline")
                        for entry in self.entries[pull["url"]][kind]
                    )
                )
            ),
            key=lambda pull: pull["updated_at"],
        )

    def _activity(self, url: str) -> dict[str, Any]:
        entries = self.entries[url]
        return {
            "comments": entries["comments"],
            "reviews": entries["reviews"],
            "threads": self._threads(url),
        }

    def _threads(self, url: str) -> list[dict[str, Any]]:
        inline = self.entries[url]["inline"]
        return [
            {
                "id": root["id"],
                "resolved": root["id"] in self.resolved,
                "outdated": False,
                "start_line": None,
                "start_side": None,
                **self.positions[root["id"]],
                "comments": [
                    c for c in inline if root["id"] in (c["id"], c["reply_to_id"])
                ],
            }
            for root in inline
            if root["reply_to_id"] is None
        ]

    def _thread(self, url: str, root: str) -> dict[str, Any]:
        found = next((t for t in self._threads(url) if t["id"] == root), None)
        if found is None:
            raise _missing(f"Thread {root} of {url}")
        return found

    def _linked(self, ticket: str) -> list[dict[str, Any]]:
        return [
            {
                key: pull[key]
                for key in ("url", "label", "title", "state", "merged", "updated_at")
            }
            | {"closes": link["closes"]}
            for pull in self.pulls.values()
            for link in pull["tickets"]
            if link["url"] == ticket
        ]

    def _root(
        self, url: str, author: str, body: str, **position: Any
    ) -> dict[str, Any]:
        """An inline comment that roots a thread at ``position``."""
        root = self._entry(url, "inline", author, body, reply_to_id=None)
        self.positions[root["id"]] = {"side": "RIGHT", **position}
        return root

    def _entry(
        self, url: str, kind: str, author: str, body: str, **fields: Any
    ) -> dict[str, Any]:
        """A comment, a review, or a thread comment (``inline``), in the shape
        Branvil answers it."""
        entry_id = f"{kind[:3]}_{next(self._ids)}"
        at = self.now()
        entry = {
            "id": entry_id,
            "author": author,
            "body": body,
            "mentions": self._mentions(body),
            "reactions": [],
            "url": f"{url}#{entry_id}",
            **(
                {"submitted_at": at, "reply_only": False}
                if kind == "reviews"
                else {"created_at": at, "meta": None}
            ),
            **fields,
        }
        self.entries[url][kind].append(entry)
        if url in self.pulls:
            self.pulls[url]["updated_at"] = at
        return entry

    def _react(self, url: str, kinds: tuple[str, ...], body: dict) -> dict[str, Any]:
        """A reaction to an entry of the item ``url``, and of no other."""
        found = next(
            (
                entry
                for kind in kinds
                for entry in self.entries[url].get(kind, [])
                if entry["id"] == body["target_id"]
            ),
            None,
        )
        if found is None:
            raise _missing(f"{body['target_id']} of {url}")
        found["reactions"].append({"author": MEMBER, "reaction": body["reaction"]})
        return {"id": found["id"], "reaction": body["reaction"]}

    def _consume(self, pull: dict[str, Any], author: str) -> None:
        """A verdict or an inline comment answers its author's review request."""
        pull["reviewers"] = [r for r in pull["reviewers"] if r.casefold() != author]

    def _at_head(self, pull: dict[str, Any], commit: str) -> None:
        if self._pull(pull)["head_sha"] != commit:
            raise Refusal(409, "head_moved", "The head is no longer that commit.")

    def _page(
        self, items: list[dict[str, Any]], params: dict | None = None
    ) -> dict[str, Any]:
        """A page of ``items``; a cursor is an offset this double gave."""
        params = params or {}
        cursor = params.get("cursor", "o:0")
        if not cursor.startswith("o:") or not cursor[2:].isdigit():
            raise Refusal(400, "invalid_cursor")
        start = int(cursor[2:])
        end = start + int(params.get("limit", 30))
        return {
            "items": items[start:end],
            "next_cursor": f"o:{end}" if end < len(items) else None,
        }

    def _mentions(self, body: str) -> list[str]:
        """Existing Branvil members, in the order they occur in the text."""
        found = []
        for member in self.members:
            pattern = rf"(?<![\w.@-])@{re.escape(member)}(?![\w-]|\.[\w-])"
            match = re.search(pattern, body, flags=re.IGNORECASE | re.ASCII)
            if match:
                found.append((match.start(), member))
        return [member for _, member in sorted(found)]


#: What every pull request changes: ``CHANGED``, added on the branch.
_CHANGE = {
    "path": CHANGED,
    "previous_path": None,
    "status": "added",
    "additions": 3,
    "deletions": 0,
    "patch": "@@ -0,0 +1,3 @@\n+a\n+b\n+c",
    "patch_complete": True,
}

_PASSED = {
    "name": "test",
    "status": "completed",
    "conclusion": "success",
    "attempt_id": "att_1",
    "attempt": 1,
    "details_url": f"{BASE}/{REPO}/-/jobs/att_1",
    "artifact_names": [],
}


def _lane(body: dict[str, Any], default: str | None = None) -> str:
    """The lane ``body`` names, ``default`` if it names none."""
    lane = body.get("lane", default)
    if lane not in LANES:
        raise Refusal(400, "invalid", f"No lane {lane}.")
    return lane


def _defined(repo: str, labels: list[str]) -> list[str]:
    undefined = [label for label in labels if label not in LABELS[repo]]
    if undefined:
        raise Refusal(
            400,
            "label_undefined",
            f"Label '{undefined[0]}' is not defined by {repo}. Defined: {LABELS[repo]}.",
        )
    return list(dict.fromkeys(labels))


def serve(double: BranvilDouble, monkeypatch: Any) -> None:
    """Have every Branvil client, gate included, talk to ``double``."""
    from guildbotics.integrations.branvil import api

    made: Callable[..., httpx.AsyncClient] = api._client

    def served(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = made(*args, **kwargs)
        # Looked up on each request, so that a test may change how it answers.
        client._transport = httpx.MockTransport(lambda r: double.respond(r))
        client._mounts = {}
        return client

    monkeypatch.setattr(api, "_client", served)


class Clock:
    """The time Branvil's clients wait on, which a sleep moves on at once."""

    def __init__(self) -> None:
        self.now = 0.0
        #: Every wait, in seconds.
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def wait_virtually(monkeypatch: Any) -> Clock:
    """Have Branvil's clients wait on a virtual clock."""
    from guildbotics.integrations.branvil import api

    clock = Clock()
    monkeypatch.setattr(api, "time", SimpleNamespace(monotonic=clock.monotonic))
    monkeypatch.setattr(
        api, "asyncio", SimpleNamespace(sleep=clock.sleep, timeout=asyncio.timeout)
    )
    return clock


def refuse(
    double: BranvilDouble,
    monkeypatch: Any,
    times: float = math.inf,
    error: type[httpx.TransportError] = httpx.ConnectError,
    method: str = "",
    path: str = "",
) -> Clock:
    """Have ``double`` refuse its next ``times`` connections (of ``method``
    and ``path`` only, if named) with ``error``, before any request reaches it; the
    clients wait on the clock returned."""
    clock = wait_virtually(monkeypatch)
    respond = double.respond
    refused = count(1)

    def refusing(request: httpx.Request) -> httpx.Response:
        if (
            (not method or request.method == method)
            and (not path or request.url.path == path)
            and next(refused) <= times
        ):
            raise error("refused", request=request)
        return respond(request)

    monkeypatch.setattr(double, "respond", refusing)
    return clock


def connect_admin(token: str = ADMIN, base_url: str = BASE) -> None:
    """Hold ``token`` as the identities' admin token for the Branvil at
    ``base_url``, as a consent leaves them connected."""
    from guildbotics.integrations.branvil.api import CONNECTION_PATH
    from guildbotics.utils.fileio import get_identity_config_dir, save_yaml_file
    from guildbotics.utils.secret_store import identity_secret_store

    identity_secret_store().set("BRANVIL_ADMIN_TOKEN", token)
    path = get_identity_config_dir() / CONNECTION_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    save_yaml_file(path, {"base_url": base_url})


def services(
    monkeypatch: Any,
    double: BranvilDouble | None = None,
    code: dict[str, str] | None = None,
    **tickets: str,
) -> tuple[BranvilDouble, Any, Any]:
    """The double, and the code host and the tickets of ``MEMBER`` on it, with
    the human ``otota`` in the team; ``code`` and ``tickets`` are the
    services' configuration besides their workspace ``SCOPE``."""
    from guildbotics.entities.team import Person, Project, Team
    from guildbotics.integrations.factory import ServiceIntegrationFactory

    double = double or BranvilDouble()
    serve(double, monkeypatch)
    monkeypatch.setenv(f"{MEMBER.upper()}_BRANVIL_TOKEN", TOKEN)
    member = Person(
        person_id=MEMBER,
        name="Aiko",
        person_type="agent",
        account_info={"branvil_username": MEMBER},
    )
    human = Person(
        person_id="otota",
        name="Otota",
        person_type="human",
        account_info={"branvil_username": "otota"},
    )
    project = Project(
        name="demo",
        services={
            "code_hosting_service": {
                "name": "branvil",
                "workspace": SCOPE,
                **(code or {}),
            },
            "ticket_service": {"name": "branvil", "workspace": SCOPE, **tickets},
        },
    )
    team = Team(project=project, members=[member, human])
    factory = ServiceIntegrationFactory()
    logger = logging.getLogger(__name__)
    return (
        double,
        factory.create_code_hosting_service(logger, member, team),
        factory.create_ticket_service(logger, member, team),
    )
