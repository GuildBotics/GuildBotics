"""Embedded Branvil setup and human/CI controls outside the member interface."""

import base64
import time
from dataclasses import dataclass

import httpx

from tests.blackbox.harness import PERSON, REPOSITORY, TRACKER


@dataclass
class BranvilProvider:
    repository: str = REPOSITORY
    tracker: str = TRACKER
    member_identity: str = PERSON

    @property
    def services(self):
        return {
            "code_hosting_service": {"name": "branvil", "workspace": "acme"},
            "ticket_service": {"name": "branvil", "workspace": "acme"},
            "chat_service": {"name": "local"},
        }

    def seed(self, stack, control):
        self.control = control
        link = stack.api.post("session/links", json={})
        assert link.is_success, link.status_code
        entered = stack.api.post(
            "session", json={"code": link.json()["url"].split("#")[1]}
        )
        assert entered.status_code == 200, entered.text
        self.api = httpx.Client(
            base_url=str(stack.api.base_url).split("/-/")[0] + "/api/v1/",
            cookies=entered.cookies,
            headers={"X-GuildBotics-CSRF": entered.json()["csrf"]},
            trust_env=False,
            timeout=10,
        )
        deadline = time.monotonic() + 30
        while True:
            ready = self.api.get("workspaces")
            if ready.is_success:
                break
            assert ready.status_code == 503 and time.monotonic() < deadline, ready.text
            time.sleep(0.05)
        self.post("workspaces", {"name": "acme"})
        repo = self.post("repos", {"path": self.repository})
        self.clone_url = repo["clone_url"]
        owner = self.api.get("me").json()["name"]
        token = self.post(f"members/{owner}/tokens", {"label": "blackbox seed"})[
            "token"
        ]
        basic = base64.b64encode(f"{owner}:{token}".encode()).decode()
        # Generated test credentials only; never inherit a host credential helper.
        self.git_env = {
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": f"http.{self.clone_url}.extraHeader",
            "GIT_CONFIG_VALUE_1": f"Authorization: Basic {basic}",
        }

    def post(self, path, body):
        response = self.api.post(path, json=body)
        assert response.is_success, response.text
        return response.json()

    def finish_checks(self, head, conclusion="success", branch="feature"):
        self.control(
            action="check",
            repo=self.repository,
            branch=branch,
            sha=head,
            conclusion=conclusion,
        )

    def provision_repository(self, checkout):
        checkout.git("remote", "add", "origin", self.clone_url)
        checkout.git("push", "-q", "origin", "main")

    def advance_base(self, stack, checkout):
        """Use a human merge to advance Branvil's protected default branch."""
        checkout.git("push", "-q", "origin", "HEAD:base-update")
        created = stack.member(
            "pr",
            "create",
            "--repo",
            self.repository,
            "--head",
            "base-update",
            "--base",
            "main",
            "--title",
            "Advance the base",
            content="Independent base change.",
        )
        self.finish_checks(checkout.git("rev-parse", "HEAD"), branch="base-update")
        self.merge(created["url"], checkout, branch="main")

    def merge(self, url, checkout, branch="feature"):
        self.control(
            action="merge",
            repo=self.repository,
            number=int(url.rsplit("/", 1)[1]),
            head=checkout.git("rev-parse", branch),
            base=checkout.git("ls-remote", "origin", "refs/heads/main").split()[0],
        )

    def close(self):
        if hasattr(self, "api"):
            self.api.close()
