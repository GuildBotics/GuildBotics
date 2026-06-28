from __future__ import annotations

from pathlib import Path
from typing import Any

from guildbotics.app_api.models import LlmProviderInfo
from guildbotics.utils.fileio import (
    get_intelligence_roots,
    load_yaml_dict,
)

PROVIDER_DEFAULT_FILENAME = "default.yml"
_DEFAULT_ORDER = 1000


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
            )
        )
    providers.sort(key=lambda provider: (provider.order, provider.provider))
    return providers


def provider_env_keys(config_dir: Path, person_id: str | None = None) -> dict[str, str]:
    """Map each provider id to the env var holding its API key."""
    return {
        provider.provider: provider.api_key_env
        for provider in discover_llm_providers(config_dir, person_id)
        if provider.api_key_env
    }
