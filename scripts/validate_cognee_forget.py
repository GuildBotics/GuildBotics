from __future__ import annotations

import argparse
import asyncio
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from guildbotics.utils.cognee_memory_backend import (
    configure_cognee_environment_from_guildbotics_keys,
    memory_data_id,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Validate Cognee remember/recall/forget behavior for a single "
            "GuildBotics memory item."
        )
    )
    parser.add_argument(
        "--dataset",
        default=f"guildbotics:validation:forget:{int(time.time())}",
        help="Temporary Cognee dataset name to use.",
    )
    parser.add_argument(
        "--keep-system-root",
        action="store_true",
        help="Use the existing Cognee SYSTEM_ROOT_DIRECTORY instead of a temporary one.",
    )
    args = parser.parse_args()

    load_dotenv()

    if not args.keep_system_root:
        root = Path(tempfile.mkdtemp(prefix="guildbotics-cognee-forget-"))
        os.environ["SYSTEM_ROOT_DIRECTORY"] = str(root / "system")
        os.environ["COGNEE_LOGS_DIR"] = str(root / "logs")
        _set_env("system_root_directory", str(root / "system"))
        _set_env("cognee_logs_dir", str(root / "logs"))
        os.environ.setdefault("CACHING", "false")
        _set_env("caching", "false")
    os.environ["LOG_LEVEL"] = "error"
    _set_env("log_level", "error")
    _normalize_env_key("OPENAI_API_KEY", "openai_api_key")
    _normalize_env_key("GOOGLE_API_KEY", "google_api_key")
    _normalize_env_key("ANTHROPIC_API_KEY", "anthropic_api_key")

    configure_cognee_environment_from_guildbotics_keys()
    _normalize_env_key("LLM_API_KEY", "llm_api_key")
    _normalize_env_key("LLM_PROVIDER", "llm_provider")
    _normalize_env_key("LLM_MODEL", "llm_model")
    _normalize_env_key("EMBEDDING_API_KEY", "embedding_api_key")
    _normalize_env_key("EMBEDDING_PROVIDER", "embedding_provider")
    _normalize_env_key("EMBEDDING_MODEL", "embedding_model")
    asyncio.run(_validate(args.dataset))


async def _validate(dataset_name: str) -> None:
    import cognee
    from cognee.tasks.ingestion.data_item import DataItem

    target_item_id = "guildbotics-forget-target"
    other_item_id = "guildbotics-forget-control"
    target_data_id = _memory_data_id(dataset_name, target_item_id)
    other_data_id = _memory_data_id(dataset_name, other_item_id)

    target_text = _memory_text(
        target_item_id,
        "GuildBotics Forget Target",
        "The target memory says FocusFlow notification default is weak.",
    )
    other_text = _memory_text(
        other_item_id,
        "GuildBotics Forget Control",
        "The control memory says FocusFlow CTA is today focus plan.",
    )

    remember_target = await cognee.remember(
        DataItem(
            data=target_text,
            label=target_item_id,
            data_id=target_data_id,
            external_metadata={"guildbotics_memory_id": target_item_id},
        ),
        dataset_name=dataset_name,
        node_set=[dataset_name, target_item_id],
        self_improvement=False,
    )
    remember_other = await cognee.remember(
        DataItem(
            data=other_text,
            label=other_item_id,
            data_id=other_data_id,
            external_metadata={"guildbotics_memory_id": other_item_id},
        ),
        dataset_name=dataset_name,
        node_set=[dataset_name, other_item_id],
        self_improvement=False,
    )

    before_forget = await _recall(cognee, dataset_name, "FocusFlow notification default")
    forget_result = await cognee.forget(data_id=target_data_id, dataset=dataset_name)
    after_target_query = await _recall(
        cognee, dataset_name, "FocusFlow notification default"
    )
    after_control_query = await _recall(cognee, dataset_name, "FocusFlow CTA")
    cleanup_result = await cognee.forget(dataset=dataset_name)

    report = {
        "dataset": dataset_name,
        "target_item_id": target_item_id,
        "target_data_id": str(target_data_id),
        "control_item_id": other_item_id,
        "control_data_id": str(other_data_id),
        "remember_target": _jsonable(remember_target),
        "remember_control": _jsonable(remember_other),
        "before_forget": _summarize_results(before_forget),
        "forget_result": _jsonable(forget_result),
        "after_target_query": _summarize_results(after_target_query),
        "after_control_query": _summarize_results(after_control_query),
        "cleanup_result": _jsonable(cleanup_result),
        "checks": {
            "target_present_before_forget": _contains(before_forget, target_item_id),
            "target_absent_after_forget": not _contains(
                after_target_query, target_item_id
            ),
            "control_still_recallable_after_target_forget": _contains(
                after_control_query, other_item_id
            ),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if not all(report["checks"].values()):
        raise SystemExit(1)


async def _recall(cognee: Any, dataset_name: str, query: str) -> list[Any]:
    results = await cognee.recall(
        query_text=query,
        datasets=[dataset_name],
        top_k=5,
        only_context=True,
    )
    return results if isinstance(results, list) else [results]


def _memory_data_id(dataset_name: str, item_id: str) -> uuid.UUID:
    return memory_data_id(dataset_name, item_id)


def _normalize_env_key(upper_key: str, lower_key: str) -> None:
    upper_value = os.getenv(upper_key)
    lower_value = os.getenv(lower_key)
    if upper_value and not lower_value:
        os.environ[lower_key] = upper_value
        return
    if lower_value and not upper_value:
        os.environ[upper_key] = lower_value


def _set_env(key: str, value: str) -> None:
    os.environ[key] = value


def _memory_text(item_id: str, title: str, body: str) -> str:
    return "\n".join(
        [
            f"guildbotics_memory_id: {item_id}",
            f"guildbotics_title: {title}",
            "",
            body,
        ]
    )


def _summarize_results(results: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "type": type(result).__name__,
            "text_excerpt": _result_text(result)[:500],
            "dataset_id": str(_raw_attr(result, "dataset_id", "") or ""),
            "dataset_name": str(_raw_attr(result, "dataset_name", "") or ""),
        }
        for result in results
    ]


def _contains(results: list[Any], item_id: str) -> bool:
    return any(item_id in _result_text(result) for result in results)


def _result_text(raw_result: Any) -> str:
    payload = _raw_attr(raw_result, "search_result", raw_result)
    if isinstance(payload, dict):
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if isinstance(payload, list):
        return "\n".join(_result_text(item) for item in payload)
    return str(payload)


def _raw_attr(raw_result: Any, name: str, default: Any = None) -> Any:
    if isinstance(raw_result, dict):
        return raw_result.get(name, default)
    return getattr(raw_result, name, default)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
    except TypeError:
        if isinstance(value, dict):
            return {str(key): _jsonable(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [_jsonable(item) for item in value]
        return repr(value)
    return value


if __name__ == "__main__":
    main()
