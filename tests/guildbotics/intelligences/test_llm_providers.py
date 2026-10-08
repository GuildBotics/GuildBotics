from pathlib import Path

import pytest

from guildbotics.intelligences.brains.inference import InferenceFailure
from guildbotics.intelligences.llm_providers import (
    classify_failure,
    discover_llm_providers,
    provider_env_keys,
    provider_of,
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


def _provider_error(provider: str, status: int, error_type: str = "") -> Exception:
    """A provider SDK's error as agno raises it: wrapped, with the SDK's
    error as its cause."""
    import anthropic
    import httpx
    import openai
    from agno.exceptions import ModelProviderError
    from google.genai import errors

    response = httpx.Response(
        status, request=httpx.Request("POST", "https://provider.test")
    )
    body = {"type": error_type, "code": "credit_balance_exhausted"}
    cause: Exception
    if provider == "openai":
        cause = openai.APIStatusError("synthetic", response=response, body=body)
    elif provider == "anthropic":
        cause = anthropic.APIStatusError("synthetic", response=response, body=body)
    else:
        cause = errors.ClientError(status, {"error": {"status": "RESOURCE_EXHAUSTED"}})
    try:
        raise ModelProviderError("synthetic", status_code=status) from cause
    except ModelProviderError as error:
        return error


@pytest.mark.parametrize(
    ("provider", "status", "error_type", "category"),
    [
        # Confirmed with a real response: credit runs out as a 429 of its own type.
        ("openai", 429, "insufficient_quota", "credit"),
        ("openai", 429, "requests", "rate_limit"),
        ("openai", 401, "invalid_request_error", "authentication"),
        ("openai", 500, "server_error", "other"),
        ("anthropic", 403, "permission_error", "authentication"),
        ("anthropic", 429, "rate_limit_error", "rate_limit"),
        # Not confirmed against a real response: told as no more than "other".
        ("anthropic", 400, "invalid_request_error", "other"),
        ("anthropic", 429, "insufficient_quota", "rate_limit"),
        # RESOURCE_EXHAUSTED is a rate limit and an exhausted quota alike.
        ("gemini", 429, "", "other"),
        ("gemini", 401, "", "authentication"),
    ],
)
def test_a_providers_refusal_is_told_by_its_status_and_error_type(
    provider: str, status: int, error_type: str, category: str
) -> None:
    failure = InferenceFailure(_provider_error(provider, status, error_type))

    assert failure.status_code == status
    assert classify_failure(provider, failure) == category


def test_jev_is_told_by_its_http_status() -> None:
    import httpx

    def refused(status: int) -> InferenceFailure:
        request = httpx.Request("POST", "https://api.typesafe.ai/v1/systemone")
        return InferenceFailure(
            httpx.HTTPStatusError(
                "synthetic", request=request, response=httpx.Response(status)
            )
        )

    assert classify_failure("jev", refused(401)) == "authentication"
    assert classify_failure("jev", refused(429)) == "rate_limit"
    assert classify_failure("jev", refused(402)) == "other"


def test_a_failure_without_a_status_is_other() -> None:
    from agno.exceptions import ModelAuthenticationError

    assert classify_failure("openai", InferenceFailure(RuntimeError())) == "other"
    # agno refuses a model with no key before asking the provider.
    assert (
        classify_failure("openai", InferenceFailure(ModelAuthenticationError("no key")))
        == "authentication"
    )


def test_a_failure_names_the_providers_error_never_its_message() -> None:
    failure = InferenceFailure(_provider_error("openai", 429, "insufficient_quota"))

    assert failure.error_type == "APIStatusError"
    assert failure.reported_type == "insufficient_quota"
    assert "synthetic" not in str(failure)


@pytest.mark.parametrize(
    ("path", "provider"),
    [
        ("models/openai/default.yml", "openai"),
        ("models/anthropic/reviewer.yml", "anthropic"),
        ("models/test.yml", ""),
        ("", ""),
    ],
)
def test_provider_of_a_model_definition(path: str, provider: str) -> None:
    assert provider_of(path) == provider
