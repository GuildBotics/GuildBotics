"""Desktop transport for core judgment availability and credential preparation."""

from collections.abc import Callable
from pathlib import Path

from fastapi import Depends, FastAPI
from pydantic import BaseModel, SecretStr

from guildbotics.app_api.errors import AppApiError
from guildbotics.intelligences.decisions import preparation


class DecisionCredential(BaseModel):
    value: SecretStr


def register(app: FastAPI, config_dir: Callable[[], Path], authorize: Callable) -> None:

    @app.get("/intelligences/decisions/options", dependencies=[Depends(authorize)])
    def options():
        return preparation.options(config_dir())

    @app.post("/intelligences/decisions/credential", dependencies=[Depends(authorize)])
    def save_credential(request: DecisionCredential):
        try:
            return preparation.save_credential(
                config_dir(), request.value.get_secret_value()
            )
        except ValueError as exc:
            raise AppApiError("invalid_decision", reason=str(exc)) from exc
