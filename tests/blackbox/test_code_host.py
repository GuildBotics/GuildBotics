"""The smoke journeys, through member CLI processes and a replaceable server."""

import json

from tests.blackbox.harness import PERSON


def create_ticket(stack, title, *options):
    return stack.member(
        "ticket",
        "create",
        "--tracker",
        stack.provider.tracker,
        "--title",
        title,
        "--human-approved",
        *options,
        content="作成時の本文。",
    )["target"]["url"]


def read(stack, kind, resource, url, **parameters):
    return stack.member(
        kind,
        "read",
        "--resource",
        resource,
        "--subject",
        url,
        "--params",
        json.dumps(parameters),
    )["items"]


def ticket(stack, url):
    [result] = read(stack, "ticket", "tickets", url)
    return result


def create_pr(stack, *options):
    return stack.member(
        "pr",
        "create",
        "--repo",
        stack.provider.repository,
        "--head",
        "feature",
        "--base",
        "main",
        "--title",
        "黒箱の変更",
        *options,
        content="メンバーから push した変更。",
    )["target"]["url"]


def readiness(stack, url):
    [result] = read(stack, "pr", "pull_request_readiness", url)
    return result


def assert_blocked(result, head, required_checks):
    assert result["head_sha"] == head
    assert result["required_checks"] == required_checks


def assert_ready(result, head):
    assert result["head_sha"] == head
    assert result["required_checks"] == "passed"


def test_tickets_and_their_relations(stack):
    parent = create_ticket(stack, "Parent")
    blocker = create_ticket(stack, "Blocker")
    url = create_ticket(
        stack,
        "Fix login",
        "--assignee",
        PERSON,
        "--lane",
        "ready",
        "--parent",
        parent,
        "--blocked-by",
        blocker,
    )
    stack.member(
        "ticket",
        "update",
        "--url",
        url,
        "--title",
        "Fix the login",
        content="更新後の本文。",
    )
    item = ticket(stack, url)
    assert (item["title"], item["body"], item["state"]) == (
        "Fix the login",
        "更新後の本文。",
        "open",
    )
    assert item["assignees"] == [stack.provider.member_identity]
    assert item["lane"] == "ready"
    assert item["parent"]["url"] == parent
    assert [(b["url"], b["state"]) for b in item["blocked_by"]] == [(blocker, "open")]
    assert [c["url"] for c in ticket(stack, parent)["children"]] == [url]
    assert [b["url"] for b in ticket(stack, blocker)["blocks"]] == [url]

    stack.member(
        "ticket",
        "update",
        "--url",
        blocker,
        "--state",
        "closed",
        "--state-reason",
        "completed",
        "--human-approved",
    )
    assert ticket(stack, url)["blocked_by"][0]["state"] == "closed"
    stack.member(
        "ticket",
        "update",
        "--url",
        url,
        "--parent",
        "",
        "--remove-blocked-by",
        blocker,
    )
    item = ticket(stack, url)
    assert (item["parent"], item["blocked_by"]) == (None, [])
    assert ticket(stack, parent)["children"] == []
    assert ticket(stack, blocker)["blocks"] == []


def test_push_pr_and_human_merge(stack, checkout):
    url = create_ticket(stack, "Close on merge")
    head = checkout.git("rev-parse", "HEAD")
    pushed = checkout.push(stack)
    assert pushed["pushed"] is True
    assert checkout.git("ls-remote", "origin", "refs/heads/feature").split()[0] == head
    pr_url = create_pr(stack, "--issue-url", url, "--closes-issue")
    [pr] = read(stack, "pr", "pull_requests", pr_url)
    assert (pr["head"], pr["base"], pr["head_sha"]) == ("feature", "main", head)
    assert (pr["state"], pr["merged"]) == ("open", False)
    [linked] = read(stack, "ticket", "ticket_pull_requests", url)
    assert linked["url"] == pr_url
    assert ticket(stack, url)["state"] == "open"

    stack.provider.finish_checks(head)
    assert_ready(readiness(stack, pr_url), head)
    stack.provider.merge(pr_url, checkout)
    [merged] = read(stack, "pr", "pull_requests", pr_url)
    assert (merged["state"], merged["merged"]) == ("closed", True)
    assert checkout.git("ls-remote", "origin", "refs/heads/main").split()[0] == head
    assert (ticket(stack, url)["state"], ticket(stack, url)["lane"]) == (
        "closed",
        "done",
    )


def test_checks_follow_the_head_and_base(stack, checkout, repository):
    url = create_pr(stack)
    assert_blocked(readiness(stack, url), repository, "running")
    stack.provider.finish_checks(repository)
    assert_ready(readiness(stack, url), repository)

    head = checkout.commit("next.txt", "a second head\n")
    checkout.push(stack)
    assert_blocked(readiness(stack, url), head, "running")
    stack.provider.finish_checks(head, "failure")
    failed = readiness(stack, url)
    assert_blocked(failed, head, "failed")
    [logs] = read(stack, "pr", "pull_request_readiness", url, failed_logs=True)
    assert logs["failed_logs"]
    assert all(job["conclusion"] == "failure" for job in logs["failed_logs"])
    stack.provider.finish_checks(head)
    assert_ready(readiness(stack, url), head)

    checkout.git("switch", "-q", "main")
    checkout.commit("base.txt", "new base\n")
    stack.provider.advance_base(stack, checkout)
    stale = readiness(stack, url)
    assert_ready(stale, head)

    checkout.git("switch", "-q", "feature")
    checkout.git("merge", "-q", "main")
    updated = checkout.git("rev-parse", "HEAD")
    checkout.push(stack)
    assert_blocked(readiness(stack, url), updated, "running")
    stack.provider.finish_checks(updated)
    assert_ready(readiness(stack, url), updated)
