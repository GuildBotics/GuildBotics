"""User-configurable filesystem boundary for the native agent adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from guildbotics.utils.fileio import get_person_config_path, load_yaml_file

FILESYSTEM_ACCESS = frozenset({"workspace", "host"})
#: Native adapters that own a filesystem boundary the user can configure.
POLICY_ADAPTERS = ("codex", "grok", "copilot")


class NativeAgentPolicyError(ValueError):
    """Raised when a native agent policy is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class AdapterFilesystemPolicy:
    """The one public policy setting a native adapter honours."""

    filesystem_access: str = "workspace"


@dataclass(frozen=True, slots=True)
class NativeAgentPolicy:
    """Per-adapter policies, so one adapter never inherits another's sandbox."""

    codex: AdapterFilesystemPolicy = field(default_factory=AdapterFilesystemPolicy)
    grok: AdapterFilesystemPolicy = field(default_factory=AdapterFilesystemPolicy)
    copilot: AdapterFilesystemPolicy = field(default_factory=AdapterFilesystemPolicy)

    def for_adapter(self, adapter: str) -> AdapterFilesystemPolicy:
        policy = getattr(self, adapter, None)
        if not isinstance(policy, AdapterFilesystemPolicy):
            raise NativeAgentPolicyError(
                f"No native agent policy is defined for adapter '{adapter}'."
            )
        return policy


def parse_native_agent_policy(payload: Any) -> NativeAgentPolicy:
    """Validate the per-adapter native-agent policy mapping."""
    if not isinstance(payload, dict):
        raise NativeAgentPolicyError("Native agent policy must be a YAML mapping.")
    _reject_unknown_keys(payload, set(POLICY_ADAPTERS), "root")
    return NativeAgentPolicy(
        **{adapter: _adapter_policy(payload, adapter) for adapter in POLICY_ADAPTERS}
    )


def load_native_agent_policy(person_id: str) -> NativeAgentPolicy:
    path = get_person_config_path(person_id, "intelligences/native_agent_policy.yml")
    return parse_native_agent_policy(load_yaml_file(path))


def _adapter_policy(payload: dict[Any, Any], adapter: str) -> AdapterFilesystemPolicy:
    raw = payload.get(adapter, {})
    if not isinstance(raw, dict):
        raise NativeAgentPolicyError(
            f"Native agent policy '{adapter}' must be a mapping."
        )
    _reject_unknown_keys(raw, {"filesystem_access"}, adapter)
    return AdapterFilesystemPolicy(
        filesystem_access=_enum_value(raw, "filesystem_access", "workspace")
    )


def _enum_value(raw: dict[Any, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or value not in FILESYSTEM_ACCESS:
        supported = ", ".join(sorted(FILESYSTEM_ACCESS))
        raise NativeAgentPolicyError(
            f"Native agent policy value '{key}' must be one of: {supported}."
        )
    return value


def _reject_unknown_keys(raw: dict[Any, Any], allowed: set[str], section: str) -> None:
    unknown = {str(key) for key in raw if key not in allowed}
    if unknown:
        values = ", ".join(sorted(unknown))
        raise NativeAgentPolicyError(
            f"Unknown native agent policy key(s) in {section}: {values}"
        )
