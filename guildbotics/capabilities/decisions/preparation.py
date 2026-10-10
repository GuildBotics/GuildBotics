"""Read Jev credential presence and save credentials in SecretStore."""

from pathlib import Path

from guildbotics.intelligences.brains.jev import JEV_KEY, credential
from guildbotics.utils.i18n_tool import t
from guildbotics.utils.secret_store import KeyringSecretStore


def options(root: Path):
    return {"credential_present": bool(credential(root))}


def save_credential(root: Path, value: str):
    value = value.strip()
    if not value:
        raise ValueError(t("intelligences.decisions.credentials_missing"))
    try:
        KeyringSecretStore(root).set(JEV_KEY, value)
    except Exception as exc:
        raise ValueError(t("intelligences.decisions.credentials_missing")) from exc
    return {"state": "unverified"}
