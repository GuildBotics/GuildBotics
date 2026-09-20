"""Check the configured brain and prepare Jev credentials."""

from pathlib import Path

from guildbotics.intelligences.brains.jev import JEV_KEY, credential
from guildbotics.intelligences.decisions.engines import evaluate
from guildbotics.intelligences.decisions.models import DecisionConfig, Question
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.secret_store import KeyringSecretStore


def options(root: Path):
    return {"credential_present": bool(credential(root))}


async def check(config: DecisionConfig, root: Path, person_id: str | None = None):
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
        person_id=person_id or "default_person",
        logger=get_logger(),
    )
    return {"state": result.error or "verified", "model": result.model}


def save_credential(root: Path, value: str):
    value = value.strip()
    if not value:
        raise ValueError(t("intelligences.decisions.credentials_missing"))
    try:
        KeyringSecretStore(root).set(JEV_KEY, value)
    except Exception as exc:
        raise ValueError(t("intelligences.decisions.credentials_missing")) from exc
    return {"state": "unverified"}
