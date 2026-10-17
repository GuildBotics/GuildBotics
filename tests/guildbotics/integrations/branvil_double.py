"""Branvil, as far as GuildBotics' ``branvil`` provider asks it: a Python
double of the HTTP API v1 the Branvil repository documents in
``docs/api.md``, behind an ``httpx.MockTransport``.

It keeps what it is sent and answers as the API says: an entry id is
answered only under the item it belongs to, a branch only by its exact
name, a review or an inline comment only on the current head
(``409 head_moved``) and only on a file the pull request changes
(``400 invalid``; every branch commit changes ``CHANGED``), a label only if
the tracker defines it (``400 label_undefined``), a ticket only on a board
there is, and an artifact only if it is a zip archive. It does
not hold writes to the member's scope -- the provider refuses those before
any request. As Branvil does, it records an author's approval of its own
pull request (which counts toward no merge); a verdict or a new inline
comment consumes its author's review request, a reply does not; marking a
pull request ready for review (or opening it as one) sets ``ready_at``; a merge closes the tickets
the pull request ``closes`` and moves them to ``done``; and a gone branch's
commit is ``""`` with no ``behind_by``.

``writes`` lists the writes it accepted, as ``"<method> <path>"``.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from itertools import count
from typing import Any
from urllib.parse import unquote

import httpx

BASE = "http://127.0.0.1:7878"
MEMBER = "aiko"
TOKEN = "bvt_aiko"
SCOPE = "acme"
REPO = f"{SCOPE}/demo"
TRACKER = f"{SCOPE}/demo-tickets"
BOARD = f"{SCOPE}/board"
#: Another board of the scope, which the member's tickets are not taken from.
OTHER_BOARD = f"{SCOPE}/other-board"
THEIR_REPO = "other/demo"
THEIR_TRACKER = "other/tickets"
LABEL = "bug"
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
        self.trackers: dict[str, dict[str, Any]] = {
            TRACKER: {"key_prefix": "DEMO", "repository": REPO, "board": BOARD},
            THEIR_TRACKER: {
                "key_prefix": "OPS",
                "repository": THEIR_REPO,
                "board": None,
            },
        }
        self.boards = {BOARD, OTHER_BOARD}
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
        self._ticks = count()
        self._ids = count(1)
        self._revisions = count(1)
        theirs = self._ticket(THEIR_TRACKER, "Theirs", "", [], author="them")
        self.their_ticket = theirs["url"]
        self.their_comment = self._entry(theirs["url"], "comments", "them", "Hi")["id"]

    # -- what the contracts do besides the API --------------------------------

    def branch(self, name: str) -> None:
        self.branches[name] = f"{name.replace('/', '-')}-sha"

    def advance(self, name: str) -> None:
        self.branches[name] = f"{name.replace('/', '-')}-sha{next(self._revisions)}"

    def assign(
        self, url: str, lane: str = "ready", name: str = MEMBER, board: str = BOARD
    ) -> None:
        self.tickets[url].update(
            board=board,
            lane=lane,
            assignees=[{"name": name, "assigned_at": self.now()}],
        )

    def add_artifact(self, sha: str, name: str, content: bytes) -> None:
        if content[:4] not in (b"PK\x03\x04", b"PK\x05\x06"):
            raise ValueError("Branvil keeps only zip archives as artifacts.")
        self.artifacts[sha, name] = content

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

    def merge(self, url: str) -> None:
        """A human merges the pull request; the tickets it closes are done."""
        at = self.now()
        self.pulls[url].update(state="closed", merged=True, closed_at=at, merged_at=at)
        for link in self.pulls[url]["tickets"]:
            ticket = self.tickets.get(link["url"])
            if link["closes"] and ticket is not None:
                ticket.update(state="closed", state_reason="completed", closed_at=at)
                if ticket["board"]:
                    ticket["lane"] = "done"

    # -- the API ----------------------------------------------------------------

    def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.raw_path.decode("ascii").partition("?")[0]
        parts = [unquote(part) for part in path.removeprefix("/api/v1/").split("/")]
        if parts == ["health"]:
            return httpx.Response(200, json={"service": "branvil", "api": 1})
        body = json.loads(request.content) if request.content else {}
        try:
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                raise Refusal(401, "unauthorized")
            status, answer = self._route(
                request.method, parts, body, dict(request.url.params)
            )
        except Refusal as refusal:
            error = {"code": refusal.code, "message": refusal.message}
            return httpx.Response(refusal.status, json={"error": error})
        if request.method != "GET":
            self.writes.append(f"{request.method} {path}")
        if isinstance(answer, bytes):
            return httpx.Response(status, content=answer)
        return httpx.Response(status, json=answer)

    def _route(
        self, method: str, parts: list[str], body: dict, params: dict[str, str]
    ) -> tuple[int, Any]:
        match parts:
            case ["me"]:
                return 200, {"name": MEMBER, "kind": "agent", "admin": False}
            case ["pulls"]:
                return 200, self._page([self._patrol(p) for p in self._involving()])
            case ["repos", repo, *rest]:
                return self._repository(method, repo, rest, body, params)
            case ["trackers", tracker, *rest]:
                return self._tracker(method, tracker, rest, body, params)
            case ["boards", board, *rest] if board in self.boards:
                return 200, self._board(board, rest, params)
        raise _missing("/".join(parts))

    def _repository(
        self, method: str, repo: str, rest: list[str], body: dict, params: dict
    ) -> tuple[int, Any]:
        if repo not in (REPO, THEIR_REPO):
            raise _missing(repo)
        match method, rest:
            case "GET", []:
                return 200, {"path": repo, "default_branch": "main"}
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
            case "GET", ["commits", sha, "artifacts", name] if (
                sha,
                name,
            ) in self.artifacts:
                return 200, self.artifacts[sha, name]
        raise _missing("/".join(rest))

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
                if body.get("drop_ticket_links"):
                    pull["tickets"] = []
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

    def _tracker(
        self, method: str, tracker: str, rest: list[str], body: dict, params: dict
    ) -> tuple[int, Any]:
        if tracker not in self.trackers:
            raise _missing(tracker)
        match method, rest:
            case "GET", []:
                return 200, {
                    "path": tracker,
                    **self.trackers[tracker],
                    "labels": [LABEL],
                }
            case "POST", ["tickets"]:
                labels = _defined(tracker, body["labels"])
                return 201, self._ticket(tracker, body["title"], body["body"], labels)
            case _, ["tickets", key, *more]:
                url = f"{BASE}/{tracker}/-/tickets/{key}"
                if url not in self.tickets:
                    raise _missing(url)
                return self._ticket_request(method, url, more, body, params)
        raise _missing("/".join(rest))

    def _ticket_request(
        self, method: str, url: str, rest: list[str], body: dict, params: dict
    ) -> tuple[int, Any]:
        ticket = self.tickets[url]
        match method, rest:
            case "GET", []:
                return 200, ticket
            case "PATCH", []:
                added = _defined(ticket["tracker"], body.get("add_labels", []))
                state_changed = body.get("state", ticket["state"]) != ticket["state"]
                for key in ("title", "body", "state", "state_reason"):
                    if key in body:
                        ticket[key] = body[key]
                if state_changed:
                    ticket["closed_at"] = (
                        self.now() if ticket["state"] == "closed" else None
                    )
                removed = body.get("remove_labels", [])
                kept = [name for name in ticket["labels"] if name not in removed]
                ticket["labels"] = kept + [name for name in added if name not in kept]
                return 200, {"ticket": ticket, "state_changed": state_changed}
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
                if body["board"] not in self.boards:
                    raise _missing(str(body["board"]))
                if body["lane"] not in LANES:
                    raise Refusal(400, "invalid", f"No lane {body['lane']}.")
                ticket.update(board=body["board"], lane=body["lane"])
                return 200, ticket
        raise _missing("/".join(rest))

    def _board(self, board: str, rest: list[str], params: dict) -> Any:
        if rest == ["cards"]:
            lanes = params["lane"].split(",")
            cards = [
                ticket
                | {
                    "comments": self.entries[url]["comments"],
                    "pull_requests": self._linked(url),
                }
                for url, ticket in self.tickets.items()
                if ticket["board"] == board
                and ticket["lane"] in lanes
                and ticket["state"] == params.get("state", ticket["state"])
                and MEMBER in [a["name"] for a in ticket["assignees"]]
            ]
            return self._page(
                sorted(cards, key=lambda card: LANES.index(card["lane"])), params
            )
        if rest == ["closed"]:
            repositories = {
                tracker["repository"]
                for tracker in self.trackers.values()
                if tracker["board"] == board
            }
            closed = [
                {
                    "kind": kind,
                    "url": item["url"],
                    "title": item["title"],
                    "label": item["label"] if kind == "pull_request" else item["key"],
                    "closed_at": item["closed_at"],
                    "merged": bool(item.get("merged")),
                }
                for kind, items in (
                    ("ticket", self.tickets),
                    ("pull_request", self.pulls),
                )
                for item in items.values()
                if (
                    item["board"] == board
                    if kind == "ticket"
                    else item["repository"] in repositories
                )
                and item["closed_at"]
                and params["since"] <= item["closed_at"] <= params["until"]
            ]
            return self._page(closed, params)
        trackers = [path for path, t in self.trackers.items() if t["board"] == board]
        return {"path": board, "lanes": list(LANES), "trackers": trackers}

    # -- items ------------------------------------------------------------------

    def _ticket(
        self,
        tracker: str,
        title: str,
        body: str,
        labels: list[str],
        author: str = MEMBER,
    ) -> dict[str, Any]:
        number = 1 + sum(t["tracker"] == tracker for t in self.tickets.values())
        key = f"{self.trackers[tracker]['key_prefix']}-{number}"
        url = f"{BASE}/{tracker}/-/tickets/{key}"
        self.tickets[url] = {
            "url": url,
            "tracker": tracker,
            "key": key,
            "repository": self.trackers[tracker]["repository"],
            "title": title,
            "body": body,
            "state": "open",
            "state_reason": None,
            "author": author,
            "assignees": [],
            "labels": labels,
            "board": None,
            "lane": None,
            "created_at": self.now(),
            "closed_at": None,
        }
        self.entries[url] = {"comments": []}
        return self.tickets[url]

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
            "assignees": [],
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


def _defined(tracker: str, labels: list[str]) -> list[str]:
    undefined = [label for label in labels if label != LABEL]
    if undefined:
        raise Refusal(
            400,
            "label_undefined",
            f"Label '{undefined[0]}' is not defined by {tracker}. Defined: {LABEL}.",
        )
    return list(dict.fromkeys(labels))


def serve(double: BranvilDouble, monkeypatch: Any) -> None:
    """Have every Branvil client, gate included, talk to ``double``."""
    from guildbotics.integrations.branvil import api

    connect: Callable[..., httpx.AsyncClient] = api.connect

    def connected(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        client = connect(*args, **kwargs)
        client._transport = httpx.MockTransport(double.respond)
        client._mounts = {}
        return client

    monkeypatch.setattr(api, "connect", connected)


def services(
    monkeypatch: Any,
    double: BranvilDouble | None = None,
    code: dict[str, str] | None = None,
    **tickets: str,
) -> tuple[BranvilDouble, Any, Any]:
    """The double, and the code host and the tickets of ``MEMBER`` on it, with
    the human ``otota`` in the team; ``code`` and ``tickets`` are the
    services' configuration besides their scope (and the board)."""
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
            "code_hosting_service": {"name": "branvil", "owner": SCOPE, **(code or {})},
            "ticket_service": {
                "name": "branvil",
                "owner": SCOPE,
                **({"board": BOARD} | tickets),
            },
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
