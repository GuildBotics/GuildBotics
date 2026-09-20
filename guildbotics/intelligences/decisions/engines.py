"""Thin one-call implementations of evaluate(state, questions)."""

import asyncio
import json
from logging import Logger
from pathlib import Path
from typing import Any

import httpx

from guildbotics.intelligences.agent_runtime.environment import start_probe_environment
from guildbotics.intelligences.brains.agno_agent import (
    AgnoAgentDefaultBrain,
    ModelConfig,
)
from guildbotics.intelligences.decisions.models import (
    DecisionConfig,
    Evaluation,
    Question,
)
from guildbotics.intelligences.decisions.normalization import normalize
from guildbotics.intelligences.decisions.settings import (
    JEV_KEY,
    availability,
    connection_identity,
    credential,
    record_connection,
)
from guildbotics.intelligences.llm_providers import discover_llm_providers
from guildbotics.utils.process_limits import STREAM_READ_LIMIT

JEV_URL = "https://api.typesafe.ai/v1"


async def jev_request(
    config_dir: Path, method: str, path: str, payload: Any = None
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.request(
            method,
            JEV_URL + path,
            headers={"Authorization": f"Bearer {credential(config_dir, JEV_KEY)}"},
            json=payload,
        )
        response.raise_for_status()
        return response.json()


async def evaluate(
    config: DecisionConfig,
    state: dict[str, Any],
    questions: dict[str, Question],
    *,
    config_dir: Path,
    person_id: str,
    logger: Logger,
) -> Evaluation:
    """Evaluate only the supplied state; never retry a malformed model answer."""
    ready = availability(config, config_dir, person_id)
    if not ready.available:
        return Evaluation(error=ready.state)
    request = {
        "state": state,
        "questions": {
            key: q.model_dump(exclude_defaults=True) for key, q in questions.items()
        },
    }
    raw: Any = None
    result = Evaluation()
    identity = ""
    try:
        identity = connection_identity(config, config_dir, person_id)
        if config.engine == "jev":
            raw = await jev_request(
                config_dir, "POST", "/systemone", {**request, "model": config.model}
            )
            result.model = raw["model"]
            result.usage = raw.get("usage")
            result.retries = 0
        else:
            prompt = json.dumps(request, ensure_ascii=False, sort_keys=True)
            instructions = (
                "Evaluate every question independently using only the supplied state. "
                'Return JSON {"answers": {question_id: {"type": "noul" or "choice", "value": answer}}}. '
                'For noul use "true", "false", "unknown". For choice use a criterion key or "unknown". '
                "Do not infer unknown as false or none. Do not use tools or external context. No prose or Markdown."
            )
            if config.engine == "agno":
                provider = next(
                    p
                    for p in discover_llm_providers(config_dir, person_id)
                    if p.provider == config.provider
                )
                model_config = ModelConfig(
                    name="decision",
                    model_class=provider.model_class,
                    parameters={
                        "id": config.model,
                        "api_key": credential(config_dir, provider.api_key_env),
                    },
                )
                brain = AgnoAgentDefaultBrain(
                    person_id,
                    "decision",
                    logger,
                    description=instructions,
                    model_config=model_config,
                )
                details = await brain.run_with_execution_details(
                    prompt, tools=[], tool_call_limit=0
                )
                content = details.content
                raw = content
                raw = json.loads(content) if isinstance(content, str) else content
                result.model = details.model
                result.usage = details.usage or None
            else:
                # Probe environments mount no repository, memory, broker or conversation.
                # Only authentication/session storage and provider API access are present.
                environment = await start_probe_environment(config.provider)
                try:
                    async with asyncio.timeout(120):
                        process = await environment.run(
                            "claude",
                            "-p",
                            "--output-format",
                            "json",
                            "--model",
                            config.model,
                            "--tools",
                            "",
                            "--strict-mcp-config",
                            "--mcp-config",
                            '{"mcpServers":{}}',
                            "--setting-sources",
                            "",
                            "--no-session-persistence",
                            "--system-prompt",
                            instructions,
                            limit=STREAM_READ_LIMIT,
                        )
                        process.stdin.write(prompt.encode())
                        await process.stdin.drain()
                        process.stdin.close()
                        stdout, _stderr = await process.communicate()
                        if await process.wait():
                            raise ValueError("cli_failed")
                        raw = stdout.decode()
                        envelope = json.loads(stdout)
                        if envelope.get("is_error"):
                            raise ValueError("cli_failed")
                        raw = envelope["result"]
                        result.usage = envelope.get("usage")
                        result.cost = envelope.get("total_cost_usd")
                        models = envelope.get("modelUsage", {})
                        result.model = (
                            next(iter(models)) if len(models) == 1 else config.model
                        )
                        raw = json.loads(raw)
                finally:
                    await environment.close()
        result.raw = raw
        result.answers = normalize(
            raw["answers"], questions, probabilistic=config.engine == "jev"
        )
    except Exception as exc:
        # Never persist provider exception bodies: these can contain credentials.
        result.raw = raw
        result.error = error_code(exc)
        if identity and result.error in {"authentication_error", "connection_error"}:
            record_connection(
                config, config_dir, result.error, person_id, identity=identity
            )
    return result


def error_code(exc: Exception) -> str:
    if getattr(exc, "status_code", None) in {401, 403}:
        return "authentication_error"
    if isinstance(exc, httpx.HTTPStatusError):
        return (
            "authentication_error"
            if exc.response.status_code in {401, 403}
            else "connection_error"
        )
    if isinstance(exc, (httpx.RequestError, TimeoutError)):
        return "connection_error"
    return (
        "invalid_response"
        if isinstance(exc, (ValueError, KeyError, TypeError))
        else "evaluation_failed"
    )
