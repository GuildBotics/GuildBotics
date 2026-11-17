"""Caller-side Git hooks and tracking against the embedded code host."""

import json
import os
import shlex
import subprocess

import yaml

from tests.blackbox.harness import PERSON


def test_current_git_uses_the_caller_commit_hook_the_pushurl_and_remote_tracking(
    stack, checkout, tmp_path
):
    identity = (
        tmp_path
        / f".guildbotics/identity/.guildbotics/config/team/members/{PERSON}/identity.yml"
    )
    config = yaml.safe_load(identity.read_text())
    config.setdefault("account_info", {})["git_email"] = "blackbox@example.test"
    identity.write_text(yaml.safe_dump(config), encoding="utf-8")
    owner = stack.provider.clone_url
    other = stack.provider.post("repos", {"path": "acme/other"})["clone_url"]
    checkout.env |= {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_2": f"http.{other}.extraHeader",
        "GIT_CONFIG_VALUE_2": checkout.env["GIT_CONFIG_VALUE_1"],
    }
    original = checkout.git("rev-parse", "HEAD")
    checkout.git("push", "-q", other, "feature")
    checkout.git("remote", "set-url", "origin", other)
    checkout.git("remote", "set-url", "--push", "origin", owner)
    for name, script in [
        (
            "pre-commit",
            "#!/bin/sh\nprintf '%s' \"host${GUILDBOTICS_TEST_GUEST-}\" > hook-ran\n",
        ),
        ("pre-push", "#!/bin/sh\ntouch push-ran\nexit 1\n"),
    ]:
        hook = checkout.path / ".git/hooks" / name
        hook.write_text(script, encoding="utf-8")
        hook.chmod(0o755)
    (checkout.path / "change").write_text("new content", encoding="utf-8")
    checkout.git("add", "change")
    sub = checkout.path / "sub"
    sub.mkdir()
    global_config = tmp_path / "caller.gitconfig"
    global_config.write_text(
        "[author]\nname = Configured Human\nemail = configured@example.test\n",
        encoding="utf-8",
    )
    caller_env = stack.env | {
        "GIT_AUTHOR_NAME": "Environment Human",
        "GIT_COMMITTER_EMAIL": "environment@example.test",
        "GIT_CONFIG_GLOBAL": str(global_config),
    }
    message = tmp_path / "commit-message"
    message.write_text("publish current workspace", encoding="utf-8")

    def member(action, repository):
        command = os.environ.get(
            "GUILDBOTICS_BLACKBOX_MEMBER_CLI", os.environ["GUILDBOTICS_BLACKBOX_CLI"]
        )
        args = [
            *shlex.split(command),
            "member",
            "git",
            action,
            "--workspace-mode",
            "current",
            "--person",
            PERSON,
            "--format",
            "json",
            "--repo-path",
            str(repository),
        ]
        if action == "publish":
            args.extend(["--content-file", str(message)])
        return subprocess.run(
            args,
            cwd=sub,
            env=caller_env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    wrong = member("publish", tmp_path / "seed")
    assert wrong.returncode == 1
    assert "current workspace repository" in wrong.stderr
    assert not (checkout.path / "hook-ran").exists()
    assert member("publish", "../missing").stderr == wrong.stderr
    published = member("publish", "..")
    assert published.returncode == 0, published.stderr
    receipt = json.loads(published.stdout)
    head = checkout.git("rev-parse", "HEAD")
    assert receipt["commit_sha"] == head
    assert receipt["pushed"] is True
    assert receipt["commits"][0]["message"] == "publish current workspace"
    assert checkout.git("log", "-1", "--format=%an <%ae> / %cn <%ce>") == (
        "Blackbox <blackbox@example.test> / Blackbox <blackbox@example.test>"
    )
    assert (checkout.path / "hook-ran").read_text() == "host"
    assert not (checkout.path / "push-ran").exists()

    def remote_head(remote, branch="feature"):
        return checkout.git("ls-remote", remote, f"refs/heads/{branch}").split()[0]

    assert remote_head(owner) == head
    assert remote_head(other) == original
    assert checkout.git("rev-parse", "refs/remotes/origin/feature") == head
    # An independent, divergent writer advances the push destination.
    checkout.git("switch", "-c", "independent", original)
    ahead = checkout.commit("independent", "other change")
    checkout.git(
        "-c", "core.hooksPath=/dev/null", "push", "-q", "--force", owner, "HEAD:feature"
    )
    checkout.git("switch", "feature")
    lock = checkout.path / ".git/refs/remotes/origin/feature.lock"
    lock.write_text("fixture conflict")
    twice_refused = member("push", checkout.path)
    assert twice_refused.returncode == 1
    assert "Failed to push 'feature'" in twice_refused.stderr
    assert "feature.lock" in twice_refused.stderr
    assert remote_head(owner) == ahead
    lock.unlink()
    rejected = member("push", checkout.path)
    assert rejected.returncode == 1
    assert "Failed to push 'feature'" in rejected.stderr
    assert remote_head(owner) == ahead
    assert checkout.git("rev-parse", "refs/remotes/origin/feature") == ahead
    assert checkout.git("rev-parse", "HEAD") == head
    checkout.git("switch", "-c", "next")
    lock = lock.with_name("next.lock")
    lock.write_text("fixture conflict")
    refused = member("push", checkout.path)
    assert refused.returncode == 1
    assert "next.lock" in refused.stderr and "Failed to push" not in refused.stderr
    assert remote_head(owner, "next") == head
    lock.unlink()
    checkout.git("switch", "--orphan", "empty")
    empty = member("push", checkout.path)
    assert empty.returncode == 1
    assert "Branch 'empty' has no commit to push." in empty.stderr
