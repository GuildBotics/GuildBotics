from __future__ import annotations

import pytest

from guildbotics.intelligences.agent_runtime.policy import (
    NativeAgentPolicyError,
    parse_native_agent_policy,
)


@pytest.mark.parametrize("filesystem_access", ["workspace", "host"])
def test_parse_policy_accepts_codex_filesystem_access(
    filesystem_access: str,
) -> None:
    policy = parse_native_agent_policy(
        {"codex": {"filesystem_access": filesystem_access}}
    )

    assert policy.filesystem_access == filesystem_access


def test_parse_policy_defaults_to_workspace_access() -> None:
    assert parse_native_agent_policy({}).filesystem_access == "workspace"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "YAML mapping"),
        ({"codex": []}, "codex.*mapping"),
        ({"codex": {"filesystem_access": "read-only"}}, "filesystem_access"),
        ({"codex": {"sandbox": "workspace-write"}}, "sandbox"),
        ({"claude": {"permission_mode": "bypassPermissions"}}, "claude"),
    ],
)
def test_parse_policy_rejects_removed_or_invalid_settings(
    payload: object, message: str
) -> None:
    with pytest.raises(NativeAgentPolicyError, match=message):
        parse_native_agent_policy(payload)
