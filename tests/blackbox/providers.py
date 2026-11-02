"""Provider-side setup, human merge, and CI controls, outside the member API.

Journeys use only Stack.member for GuildBotics operations. A provider adapter
owns the other side of the boundary: local uses its documented files and bare
remote; an embedded code host can implement these controls through its owner
and runner interfaces without changing the journeys.
"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from tests.blackbox.harness import PERSON, REPOSITORY, TRACKER


@dataclass
class LocalProvider:
    workspace: Path
    repository: str = REPOSITORY
    tracker: str = TRACKER
    member_identity: str = f"local-{PERSON}"

    @property
    def services(self):
        return {
            "code_hosting_service": {
                "name": "local",
                "owner": "acme/team",
                "checks": "pending",
            },
            "ticket_service": {"name": "local", "owner": "acme/team"},
            "chat_service": {"name": "local"},
        }

    @property
    def code_dir(self):
        return (
            self.workspace
            / ".guildbotics/local/services/code"
            / quote(self.repository, safe="")
        )

    @property
    def remote(self):
        return self.code_dir / "repository.git"

    @property
    def clone_url(self):
        return self.remote.as_uri()

    def seed(self):
        tracker = (
            self.workspace
            / ".guildbotics/local/services/tickets"
            / quote(self.tracker, safe="")
        )
        tracker.mkdir(parents=True)
        (tracker / "tracker.json").write_text(
            json.dumps({"repository": self.repository, "labels": []}),
            encoding="utf-8",
        )

    def finish_checks(self, head, conclusion="success"):
        path = self.code_dir / "checks" / head
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(conclusion, encoding="utf-8")

    def provision_repository(self, checkout):
        self.remote.parent.mkdir(parents=True, exist_ok=True)
        checkout.git("clone", "-q", "--bare", str(checkout.path), str(self.remote))

    def merge(self, url, checkout):
        """Supply a human's merge, including the host's closing-link effects.

        The local provider has no merge API. Like its port contract fixture,
        this writes the human-owned state; assertions read it through the CLI.
        """
        path = self.code_dir / "pulls" / f"{url.rsplit('/', 1)[1]}.json"
        pull = json.loads(path.read_text(encoding="utf-8"))
        head = checkout.git("rev-parse", pull["head"])
        base = checkout.git("--git-dir", str(self.remote), "rev-parse", pull["base"])
        checkout.git("merge-base", "--is-ancestor", base, head)
        checkout.git(
            "--git-dir",
            str(self.remote),
            "update-ref",
            f"refs/heads/{pull['base']}",
            head,
            base,
        )
        now = datetime.now(UTC).isoformat()
        pull.update(state="closed", merged=True, closed_at=now, updated_at=now)
        path.write_text(json.dumps(pull), encoding="utf-8")
        for link in pull["tickets"]:
            if link["closes"]:
                tracker, key = (
                    link["url"].removeprefix("local://tickets/").rsplit("/", 1)
                )
                ticket_path = (
                    self.workspace
                    / ".guildbotics/local/services/tickets"
                    / quote(tracker, safe="")
                    / f"{key}.json"
                )
                ticket = json.loads(ticket_path.read_text(encoding="utf-8"))
                ticket.update(
                    state="closed", lane="done", closed_at=now, updated_at=now
                )
                ticket_path.write_text(json.dumps(ticket), encoding="utf-8")


# #88 can add an in-process Branvil adapter here. Unknown selections fail,
# rather than silently exercising local while reporting another provider.
PROVIDERS = {"local": LocalProvider}
