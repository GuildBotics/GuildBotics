"""Representative member journeys through the installed CLI and real HTTP API."""

from tests.blackbox.harness import REPOSITORY, TRACKER


def test_ticket_create_read_and_comment(stack):
    created = stack.member(
        "ticket",
        "create",
        "--tracker",
        TRACKER,
        "--title",
        "黒箱のチケット",
        "--human-approved",
        content="外から作成した本文。",
    )
    url = created["target"]["url"]
    read = stack.member("ticket", "read", "--resource", "tickets", "--subject", url)
    assert read["items"][0]["title"] == "黒箱のチケット"
    assert read["items"][0]["body"] == "外から作成した本文。"
    stack.member("ticket", "comment", "--url", url, content="確認のコメント。")
    comments = stack.member(
        "ticket",
        "read",
        "--resource",
        "ticket_comments",
        "--subject",
        url,
    )
    assert [comment["body"] for comment in comments["items"]] == ["確認のコメント。"]


def test_pr_create_and_read(stack, repository):
    created = stack.member(
        "pr",
        "create",
        "--repo",
        REPOSITORY,
        "--head",
        "feature",
        "--base",
        "main",
        "--title",
        "黒箱の PR",
        content="外から読める変更概要。",
    )
    read = stack.member(
        "pr",
        "read",
        "--resource",
        "pull_requests",
        "--subject",
        created["target"]["url"],
    )
    [pr] = read["items"]
    assert pr["title"] == "黒箱の PR"
    assert pr["body"] == "外から読める変更概要。"
    assert pr["head"] == "feature"
    assert pr["base"] == "main"
    assert pr["head_sha"] == repository


def test_chat_post_and_read(stack):
    posted = stack.member(
        "chat",
        "post",
        "--channel-id",
        "blackbox",
        content="黒箱からの投稿。",
    )
    channel = stack.member("chat", "inspect", "channel", "--channel-id", "blackbox")
    assert [event["text"] for event in channel["messages"]] == ["黒箱からの投稿。"]
    assert channel["messages"][0]["message_id"] == posted["message_id"]


def test_memory_record_and_search(stack):
    recorded = stack.member(
        "memory",
        "record",
        "--scope",
        "team",
        "--title",
        "黒箱の記憶",
        content="blackbox-search-marker: 引き継ぐ設計判断。",
    )
    recalled = stack.member("memory", "recall", "--query", "blackbox-search-marker")
    assert any(hit["doc_id"] == recorded["doc_id"] for hit in recalled["results"])
    document = stack.member("memory", "get", "--id", recorded["doc_id"], "--team")
    assert document["body"] == "blackbox-search-marker: 引き継ぐ設計判断。"
