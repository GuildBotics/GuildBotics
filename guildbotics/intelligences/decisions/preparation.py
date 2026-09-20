"""Credential preparation and connection checks, shared by desktop and execution."""

from pathlib import Path
from typing import Any

from guildbotics.intelligences.decisions.engines import (
    error_code,
    evaluate,
    jev_request,
)
from guildbotics.intelligences.decisions.models import DecisionConfig, Question
from guildbotics.intelligences.decisions.settings import (
    JEV_KEY,
    JEV_MODELS,
    availability,
    connection_identity,
    read_config,
    record_connection,
)
from guildbotics.intelligences.llm_providers import discover_llm_providers
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.secret_store import KeyringSecretStore


def options(root: Path, person_id: str | None = None) -> dict[str, Any]:
    selected = read_config(root, person_id)
    candidates = [
        DecisionConfig(engine="jev"),
        DecisionConfig(engine="cli", provider="claude"),
    ]
    candidates.extend(
        DecisionConfig(engine="agno", provider=p.provider)
        for p in discover_llm_providers(root, person_id)
    )
    candidates = [
        selected
        if (c.engine, c.provider) == (selected.engine, selected.provider)
        else c
        for c in candidates
    ]
    return {
        "selected": selected,
        "options": [
            {
                "engine": c.engine,
                "provider": c.provider,
                "models": list(JEV_MODELS) if c.engine == "jev" else [],
                **availability(c, root, person_id, require_model=False).model_dump(),
            }
            for c in candidates
        ],
    }


async def check(
    config: DecisionConfig, root: Path, person_id: str | None = None
) -> dict[str, Any]:
    # A previous authentication refusal must not prevent a deliberate recheck.
    identity = connection_identity(config, root, person_id)
    record_connection(config, root, "unverified", person_id, identity=identity)
    ready = availability(config, root, person_id)
    if not ready.available:
        return {"status": ready, "models": []}
    models: list[str] = []
    try:
        if config.engine == "jev":
            data = await jev_request(root, "GET", "/models")
            models = [item["name"] for item in data["models"]]
        result = await evaluate(
            config,
            {"text": "connection check"},
            {
                "present": Question(type="noul", instructions="Is text present?"),
                "kind": Question(
                    type="choice",
                    instructions="Which value matches text?",
                    criteria={"check": "A connection check", "other": "Other text"},
                ),
            },
            config_dir=root,
            person_id=person_id or "default",
            logger=get_logger(),
        )
        code = result.error or "verified"
    except Exception as exc:
        code = error_code(exc)
    state = (
        code
        if code in {"verified", "authentication_error", "connection_error"}
        else "invalid"
    )
    record_connection(config, root, state, person_id, identity=identity)
    if identity != connection_identity(config, root, person_id):
        return {"status": availability(config, root, person_id), "models": models}
    return {
        "status": ready.model_copy(
            update={
                "state": state,
                "available": state in {"verified", "connection_error"},
                "reason": t("intelligences.decisions." + code),
            }
        ),
        "models": models,
    }


def save_credential(root: Path, value: str):
    value = value.strip()
    if not value:
        raise ValueError(t("intelligences.decisions.credentials_missing"))
    try:
        KeyringSecretStore(root).set(JEV_KEY, value)
    except Exception as exc:
        raise ValueError(t("intelligences.decisions.credentials_missing")) from exc
    config = DecisionConfig(engine="jev", model="jev-latest")
    record_connection(config, root, "unverified")
    return availability(config, root)
