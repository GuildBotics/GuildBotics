"""Jev's structured question API behind the common Brain interface."""

import json
from pathlib import Path
from typing import Any

from guildbotics.intelligences.brains.inference import JevCall, inference
from guildbotics.observability import span_scope
from guildbotics.runtime.brain import Brain, ExecutionMetadata

JEV_KEY = "TYPESAFE_API_KEY"
JEV_MODEL = "jev-latest"
#: What Jev's calls and their key are recorded as.
JEV_PROVIDER = "jev"


def credential(config_dir: Path) -> str:
    """The workspace's Jev key: only the host holds it."""
    # Imported here: the keychain is the host's.
    from guildbotics.utils.secret_store import KeyringSecretStore

    return KeyringSecretStore(config_dir).get(JEV_KEY) or ""


class JevBrain(Brain):
    """Accept JSON containing state and questions; return Jev's distributions."""

    probabilistic_answers = True

    def __init__(self, *args, model: str = JEV_MODEL, **kwargs):
        super().__init__(*args, **kwargs)
        if model != JEV_MODEL:
            raise ValueError("Unsupported Jev model")
        self.model = model

    @property
    def configuration(self) -> dict[str, Any]:
        return {**super().configuration, "provider": JEV_PROVIDER, "model": self.model}

    async def run(self, message: str, **kwargs):
        payload = json.loads(message)
        with span_scope(JEV_PROVIDER) as span:
            result = await inference().jev(
                JevCall(
                    state=payload["state"],
                    questions=payload["questions"],
                    model=self.model,
                    span=span,
                )
            )
        self.execution = ExecutionMetadata(
            model=result["model"],
            usage=result.get("usage"),
            retries=0,
        )
        return result
