"""Desktop transport for core judgment availability and credential preparation."""

from collections.abc import Callable
from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel, SecretStr

from guildbotics.app_api.errors import AppApiError
from guildbotics.capabilities.decisions import preparation


class DecisionCredential(BaseModel):
    value: SecretStr


def register(router: APIRouter, config_dir: Callable[[], Path]) -> None:

    @router.get("/intelligences/decisions/options")
    def options():
        return preparation.options(config_dir())

    @router.post("/intelligences/decisions/credential")
    def save_credential(request: DecisionCredential):
        try:
            return preparation.save_credential(
                config_dir(), request.value.get_secret_value()
            )
        except ValueError as exc:
            raise AppApiError("invalid_decision", reason=str(exc)) from exc
