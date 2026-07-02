from pathlib import Path

from guildbotics.intelligences.llm_providers import (
    discover_llm_providers,
    provider_env_keys,
)

DEFAULT_ORDER = 1000


def _write_provider(config_root: Path, provider: str, body: str) -> None:
    provider_file = config_root / "intelligences/models" / provider / "default.yml"
    provider_file.parent.mkdir(parents=True, exist_ok=True)
    provider_file.write_text(body, encoding="utf-8")


def test_discover_llm_providers_uses_config_precedence_and_order(
    tmp_path: Path,
) -> None:
    _write_provider(
        tmp_path,
        "openai",
        "\n".join(
            [
                "label: Custom OpenAI",
                "order: 1",
                "api_key_env: CUSTOM_OPENAI_API_KEY",
                "model_class: custom.OpenAI",
                "parameters:",
                "  id: custom-model",
            ]
        ),
    )
    _write_provider(
        tmp_path,
        "zeta",
        "\n".join(
            [
                "label: Zeta",
                "order: 5",
                "api_key_env: ZETA_API_KEY",
                "model_class: custom.Zeta",
                "parameters:",
                "  id: zeta-model",
            ]
        ),
    )

    providers = discover_llm_providers(tmp_path)
    by_name = {provider.provider: provider for provider in providers}

    assert [provider.provider for provider in providers[:2]] == ["openai", "zeta"]
    assert by_name["openai"].label == "Custom OpenAI"
    assert by_name["openai"].api_key_env == "CUSTOM_OPENAI_API_KEY"
    assert by_name["openai"].model_id == "custom-model"
    assert provider_env_keys(tmp_path)["zeta"] == "ZETA_API_KEY"


def test_discover_llm_providers_tolerates_malformed_and_missing_yaml(
    tmp_path: Path,
) -> None:
    _write_provider(tmp_path, "broken", "label: [broken\n")
    (tmp_path / "intelligences/models/missing").mkdir(parents=True)

    providers = {
        provider.provider: provider for provider in discover_llm_providers(tmp_path)
    }

    assert "missing" not in providers
    assert providers["broken"].label == "broken"
    assert providers["broken"].order == DEFAULT_ORDER
    assert providers["broken"].api_key_env == ""
