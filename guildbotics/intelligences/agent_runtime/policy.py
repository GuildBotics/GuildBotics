"""User-configurable filesystem boundary for the native Codex adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from guildbotics.utils.fileio import get_person_config_path, load_yaml_file

CODEX_FILESYSTEM_ACCESS = frozenset({"workspace", "host"})


class NativeAgentPolicyError(ValueError):
    """Raised when a native agent policy is malformed or unsupported."""


@dataclass(frozen=True, slots=True)
class NativeAgentPolicy:
    filesystem_access: str = "workspace"


def parse_native_agent_policy(payload: Any) -> NativeAgentPolicy:
    """Validate the one public native-agent policy setting."""
    if not isinstance(payload, dict):
        raise NativeAgentPolicyError("Native agent policy must be a YAML mapping.")
    _reject_unknown_keys(payload, {"codex"}, "root")

    raw = payload.get("codex", {})
    if not isinstance(raw, dict):
        raise NativeAgentPolicyError("Native agent policy 'codex' must be a mapping.")
    _reject_unknown_keys(raw, {"filesystem_access"}, "codex")
    return NativeAgentPolicy(
        filesystem_access=_enum_value(
            raw,
            "filesystem_access",
            "workspace",
            CODEX_FILESYSTEM_ACCESS,
        )
    )


def load_native_agent_policy(person_id: str) -> NativeAgentPolicy:
    path = get_person_config_path(person_id, "intelligences/native_agent_policy.yml")
    return parse_native_agent_policy(load_yaml_file(path))


def _enum_value(
    raw: dict[Any, Any], key: str, default: str, allowed: frozenset[str]
) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str) or value not in allowed:
        supported = ", ".join(sorted(allowed))
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
