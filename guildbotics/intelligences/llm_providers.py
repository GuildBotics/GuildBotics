from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel

from guildbotics.intelligences.brains.inference import (
    FailureCategory,
    InferenceFailure,
)
from guildbotics.intelligences.effort import (
    EffortField,
    validate_effort_fields,
    validate_effort_overlay,
)
from guildbotics.utils.fileio import (
    get_intelligence_roots,
    load_yaml_dict,
)

PROVIDER_DEFAULT_FILENAME = "default.yml"
_DEFAULT_ORDER = 1000

#: The failure categories the user has to act on: a rate limit passes by itself.
CREDENTIAL_FAILURES: frozenset[str] = frozenset({"authentication", "credit"})
#: The error ``type`` a provider reports for an account out of credit, confirmed
#: against a real response. A provider without one reports its credit shortage
#: as ``other`` until such a response is seen.
_CREDIT_ERROR_TYPES = {"openai": "insufficient_quota"}
#: Providers whose 429 does not tell a rate limit from an exhausted quota
#: (Gemini's ``RESOURCE_EXHAUSTED``).
_UNTOLD_429 = frozenset({"gemini"})
_MODEL_PATH_PARTS = 3
_HTTP_UNAUTHORIZED = 401
_HTTP_FORBIDDEN = 403
_HTTP_TOO_MANY_REQUESTS = 429


class LlmProviderInfo(BaseModel):
    """A selectable LLM provider, discovered from ``models/<provider>/default.yml``.

    This is the single source of truth for the provider catalog: ``provider`` is
    the directory name, and the remaining fields come from that ``default.yml``.
    """

    provider: str
    label: str = ""
    order: int = 1000
    api_key_env: str = ""
    model_class: str = ""
    model_id: str = ""
    #: The provider's default effort overlay, so a slot that switches provider
    #: starts from a mapping that fits that provider's parameter shapes.
    effort: dict[str, dict] = {}
    #: The settings this provider accepts, so an editor can offer typed
    #: controls for a slot that has no definition file yet.
    effort_fields: list[EffortField] = []


def _read_provider_default(roots: list[Path], provider: str) -> dict[str, Any]:
    for root in roots:
        data = load_yaml_dict(root / provider / PROVIDER_DEFAULT_FILENAME)
        if data:
            return data
    return {}


def discover_llm_providers(
    config_dir: Path, person_id: str | None = None
) -> list[LlmProviderInfo]:
    """Discover selectable LLM providers from ``models/<provider>/default.yml``.

    A provider is any directory (in member, team, or template scope) that holds a
    ``default.yml``; the file in the highest-priority scope wins. This is the only
    place that enumerates the provider catalog, so adding a provider is just a
    matter of dropping in ``models/<provider>/default.yml``.
    """
    roots = get_intelligence_roots(config_dir, person_id, "models")
    names: set[str] = set()
    for root in roots:
        if root.is_dir():
            for child in root.iterdir():
                if child.is_dir() and (child / PROVIDER_DEFAULT_FILENAME).exists():
                    names.add(child.name)

    providers: list[LlmProviderInfo] = []
    for name in names:
        data = _read_provider_default(roots, name)
        parameters = data.get("parameters", {})
        if not isinstance(parameters, dict):
            parameters = {}
        try:
            order = int(data.get("order", _DEFAULT_ORDER))
        except (TypeError, ValueError):
            order = _DEFAULT_ORDER
        providers.append(
            LlmProviderInfo(
                provider=name,
                label=str(data.get("label", "") or name),
                order=order,
                api_key_env=str(data.get("api_key_env", "")),
                model_class=str(data.get("model_class", "")),
                model_id=str(parameters.get("id", "")),
                effort_fields=validate_effort_fields(
                    data.get("effort_fields"), where=f"provider '{name}'"
                ),
                effort=validate_effort_overlay(
                    data.get("effort"), where=f"provider '{name}'"
                ),
            )
        )
    providers.sort(key=lambda provider: (provider.order, provider.provider))
    return providers


def provider_of(model_path: str) -> str:
    """The provider of the model definition ``models/<provider>/<file>.yml``;
    empty when the path does not name one."""
    parts = model_path.split("/")
    return parts[1] if len(parts) >= _MODEL_PATH_PARTS else ""


def classify_failure(provider: str, failure: InferenceFailure) -> FailureCategory:
    """Why ``provider`` refused a call, from the status and the error type it
    reported: nothing else it said decides."""
    if failure.status_code in (_HTTP_UNAUTHORIZED, _HTTP_FORBIDDEN):
        return "authentication"
    if failure.status_code != _HTTP_TOO_MANY_REQUESTS or provider in _UNTOLD_429:
        return "other"
    credit = _CREDIT_ERROR_TYPES.get(provider)
    if credit and failure.reported_type == credit:
        return "credit"
    return "rate_limit"


def provider_env_keys(config_dir: Path, person_id: str | None = None) -> dict[str, str]:
    """Map each provider id to the env var holding its API key."""
    return {
        provider.provider: provider.api_key_env
        for provider in discover_llm_providers(config_dir, person_id)
        if provider.api_key_env
    }
