"""Shared resolution and availability for editing and executing judgments."""

import hashlib
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel

from guildbotics.intelligences.agent_environment.status import device_status
from guildbotics.intelligences.decisions.models import DecisionConfig
from guildbotics.intelligences.llm_providers import discover_llm_providers
from guildbotics.utils.fileio import get_intelligence_roots, load_yaml_dict
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.secret_store import KeyringSecretStore

CONFIG_FILE = "decision.yml"
JEV_KEY = "TYPESAFE_API_KEY"
JEV_MODELS = ("jev-1.13.0", "jev-latest", "jev-preview")
# A tool-free, stateless JSON invocation is implemented for Claude.
DECISION_CLI_TOOLS = ("claude",)
_CONNECTIONS: dict[str, str] = {}
AvailabilityState = Literal[
    "missing",
    "unverified",
    "verified",
    "authentication_error",
    "connection_error",
    "invalid",
]


def status_message(code: str) -> str:
    return {
        "missing": t("intelligences.decisions.credentials_missing"),
        "unverified": t("intelligences.decisions.unverified"),
        "verified": t("intelligences.decisions.verified"),
        "authentication_error": t("intelligences.decisions.authentication_error"),
        "connection_error": t("intelligences.decisions.connection_error"),
        "invalid": t("intelligences.decisions.invalid"),
        "invalid_response": t("intelligences.decisions.invalid_response"),
        "evaluation_failed": t("intelligences.decisions.evaluation_failed"),
    }[code]


def _connection_key(config: DecisionConfig, config_dir: Path, value: str) -> str:
    return hashlib.sha256(
        f"{config_dir}\0{config.engine}\0{config.provider}\0{config.model}\0{value}".encode()
    ).hexdigest()


def connection_identity(
    config: DecisionConfig, config_dir: Path, person_id: str | None = None
) -> str:
    key = (
        JEV_KEY
        if config.engine == "jev"
        else next(
            (
                p.api_key_env
                for p in discover_llm_providers(config_dir, person_id)
                if p.provider == config.provider
            ),
            "",
        )
    )
    return _connection_key(
        config, config_dir, credential(config_dir, key) if key else ""
    )


def record_connection(
    config: DecisionConfig,
    config_dir: Path,
    state: str,
    person_id: str | None = None,
    *,
    identity: str | None = None,
) -> None:
    _CONNECTIONS[identity or connection_identity(config, config_dir, person_id)] = state


class Availability(BaseModel):
    available: bool
    state: AvailabilityState
    reason: str = ""
    recovery: Literal["credentials", "environment", "model"] = "credentials"


def read_config(config_dir: Path, person_id: str | None = None) -> DecisionConfig:
    for root in get_intelligence_roots(config_dir, person_id, ""):
        path = root / CONFIG_FILE
        if path.is_file():
            return DecisionConfig.model_validate(load_yaml_dict(path))
    return DecisionConfig()


def credential(config_dir: Path, key: str) -> str:
    # A process environment may still contain a key loaded before its deletion.
    # The device's current SecretStore is authoritative for every invocation.
    return KeyringSecretStore(config_dir).get(key) or ""


def availability(
    config: DecisionConfig,
    config_dir: Path,
    person_id: str | None = None,
    *,
    require_model: bool = True,
) -> Availability:
    """Check local usability afresh; key presence is never authentication success."""
    if require_model and not config.model.strip():
        return Availability(
            available=False,
            state="invalid",
            reason=t("intelligences.decisions.model_required"),
            recovery="model",
        )
    providers = discover_llm_providers(config_dir, person_id)
    incompatible = (config.engine != "jev" and config.model in JEV_MODELS) or any(
        config.model == p.model_id
        and not (
            (config.engine == "agno" and config.provider == p.provider)
            or (
                config.engine == "cli"
                and config.provider == "claude"
                and p.provider == "anthropic"
            )
        )
        for p in providers
        if p.model_id
    )
    if require_model and incompatible:
        return Availability(
            available=False,
            state="invalid",
            reason=t("intelligences.decisions.model_incompatible"),
            recovery="model",
        )
    if config.engine == "cli":
        if config.provider not in DECISION_CLI_TOOLS:
            return Availability(
                available=False,
                state="invalid",
                reason=t("intelligences.decisions.unsupported_cli"),
                recovery="environment",
            )
        status = device_status()
        tool = status.tool(config.provider)
        reason = status.refusal or tool.refusal
        checked = cast(
            AvailabilityState,
            _CONNECTIONS.get(_connection_key(config, config_dir, ""), "unverified"),
        )
        return Availability(
            available=not reason and checked not in {"invalid", "authentication_error"},
            state="missing" if reason else checked,
            reason=reason or status_message(checked),
            recovery="environment",
        )
    if config.engine == "jev":
        if config.provider or (require_model and config.model not in JEV_MODELS):
            return Availability(
                available=False,
                state="invalid",
                reason=t("intelligences.decisions.model_incompatible"),
                recovery="model",
            )
        key = JEV_KEY
    else:
        provider = next(
            (p for p in providers if p.provider == config.provider),
            None,
        )
        if provider is None:
            return Availability(
                available=False,
                state="invalid",
                reason=t("intelligences.decisions.provider_required"),
                recovery="model",
            )
        key = provider.api_key_env
    try:
        value = credential(config_dir, key) if key else ""
        usable = bool(value)
    except Exception:
        usable = False
    checked = cast(
        AvailabilityState,
        (
            _CONNECTIONS.get(_connection_key(config, config_dir, value), "unverified")
            if usable
            else "missing"
        ),
    )
    return Availability(
        available=usable and checked not in {"authentication_error", "invalid"},
        state=cast(AvailabilityState, checked),
        reason=status_message(checked),
    )
