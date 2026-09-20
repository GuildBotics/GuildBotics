"""Jev's structured question API behind the common Brain interface."""

import json
from pathlib import Path
from typing import Any

import httpx

from guildbotics.intelligences.brains.brain import Brain, ExecutionMetadata
from guildbotics.utils.fileio import get_workspace_config_dir
from guildbotics.utils.secret_store import KeyringSecretStore

JEV_KEY = "TYPESAFE_API_KEY"
JEV_MODELS = ("jev-1.13.0", "jev-latest", "jev-preview")


def credential(config_dir: Path) -> str:
    return KeyringSecretStore(config_dir).get(JEV_KEY) or ""


async def request(config_dir: Path, method: str, path: str, payload: Any = None):
    key = credential(config_dir)
    if not key:
        raise ValueError("credentials_missing")
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.request(
            method,
            "https://api.typesafe.ai/v1" + path,
            headers={"Authorization": f"Bearer {key}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()


class JevBrain(Brain):
    """Accept JSON containing state and questions; return Jev's distributions."""

    probabilistic_answers = True

    def __init__(self, *args, model: str = "jev-latest", **kwargs):
        super().__init__(*args, **kwargs)
        if model not in JEV_MODELS:
            raise ValueError("Unsupported Jev model")
        self.model = model

    async def run(self, message: str, **kwargs):
        payload = json.loads(message)
        result = await request(
            get_workspace_config_dir(),
            "POST",
            "/systemone",
            {
                "state": payload["state"],
                "questions": payload["questions"],
                "model": self.model,
            },
        )
        self.execution = ExecutionMetadata(
            model=result["model"],
            usage=result.get("usage"),
            retries=0,
        )
        return result
