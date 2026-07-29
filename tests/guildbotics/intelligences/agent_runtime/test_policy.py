from __future__ import annotations

import pytest

from guildbotics.intelligences.agent_runtime.policy import (
    POLICY_ADAPTERS,
    NativeAgentPolicyError,
    parse_native_agent_policy,
)


@pytest.mark.parametrize("adapter", POLICY_ADAPTERS)
@pytest.mark.parametrize("filesystem_access", ["workspace", "host"])
def test_parse_policy_accepts_per_adapter_filesystem_access(
    adapter: str, filesystem_access: str
) -> None:
    policy = parse_native_agent_policy(
        {adapter: {"filesystem_access": filesystem_access}}
    )

    assert policy.for_adapter(adapter).filesystem_access == filesystem_access


def test_parse_policy_defaults_every_adapter_to_workspace_access() -> None:
    policy = parse_native_agent_policy({})

    assert [
        policy.for_adapter(adapter).filesystem_access for adapter in POLICY_ADAPTERS
    ] == ["workspace"] * len(POLICY_ADAPTERS)


def test_one_adapter_setting_does_not_leak_into_another() -> None:
    policy = parse_native_agent_policy({"codex": {"filesystem_access": "host"}})

    assert policy.for_adapter("codex").filesystem_access == "host"
    assert policy.for_adapter("grok").filesystem_access == "workspace"


def test_unknown_adapter_has_no_policy() -> None:
    with pytest.raises(NativeAgentPolicyError, match="claude"):
        parse_native_agent_policy({}).for_adapter("claude")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "YAML mapping"),
        ({"codex": []}, "codex.*mapping"),
        ({"grok": []}, "grok.*mapping"),
        ({"codex": {"filesystem_access": "read-only"}}, "filesystem_access"),
        ({"grok": {"filesystem_access": "off"}}, "filesystem_access"),
        ({"codex": {"sandbox": "workspace-write"}}, "sandbox"),
        ({"grok": {"always_approve": True}}, "always_approve"),
        ({"claude": {"permission_mode": "bypassPermissions"}}, "claude"),
    ],
)
def test_parse_policy_rejects_removed_or_invalid_settings(
    payload: object, message: str
) -> None:
    with pytest.raises(NativeAgentPolicyError, match=message):
        parse_native_agent_policy(payload)
