from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol

from guildbotics.utils.memory_backend import (
    JsonMap,
    MemoryContext,
    MemoryForgetRequest,
    MemoryForgetResult,
    MemoryItem,
    MemoryQuery,
    MemoryUpdate,
    MemoryWriteResult,
    is_expired_retention,
    write_memory_forget_trace,
    write_memory_recall_final_trace,
    write_memory_recall_raw_trace,
)


class CogneeAdapter(Protocol):
    def recall(
        self, *, query_text: str, dataset_name: str, top_k: int
    ) -> list[Any]: ...

    def remember(
        self,
        *,
        content: str,
        dataset_name: str,
        item_id: str,
        data_id: uuid.UUID,
        metadata: JsonMap,
    ) -> Any: ...

    def forget(
        self,
        *,
        data_id: uuid.UUID,
        dataset_name: str,
    ) -> Any: ...


_COGNEE_LOOP: asyncio.AbstractEventLoop | None = None
_COGNEE_LOOP_THREAD: threading.Thread | None = None
_COGNEE_LOOP_LOCK = threading.Lock()


class DefaultCogneeAdapter:
    def __init__(self) -> None:
        configure_cognee_environment_from_guildbotics_keys()
        if importlib.util.find_spec("cognee") is None:
            raise RuntimeError("cognee package is not installed")

    def recall(self, *, query_text: str, dataset_name: str, top_k: int) -> list[Any]:
        async def _recall() -> list[Any]:
            if not await _cognee_dataset_exists(dataset_name):
                return []

            import cognee

            results = await cognee.recall(
                query_text=query_text,
                datasets=[dataset_name],
                top_k=top_k,
                only_context=True,
            )
            return results if isinstance(results, list) else [results]

        return _run_async(_recall())

    def remember(
        self,
        *,
        content: str,
        dataset_name: str,
        item_id: str,
        data_id: uuid.UUID,
        metadata: JsonMap,
    ) -> Any:
        async def _remember() -> Any:
            import cognee

            await _setup_cognee()

            data: Any = content
            try:
                from cognee.tasks.ingestion.data_item import DataItem
            except Exception:
                pass
            else:
                data = DataItem(
                    data=content,
                    label=item_id,
                    data_id=data_id,
                    external_metadata=metadata,
                )

            return await cognee.remember(
                data,
                dataset_name=dataset_name,
                node_set=[dataset_name, item_id],
                self_improvement=False,
            )

        return _run_async(_remember())

    def forget(
        self,
        *,
        data_id: uuid.UUID,
        dataset_name: str,
    ) -> Any:
        async def _forget() -> Any:
            import cognee

            await _setup_cognee()
            return await cognee.forget(data_id=data_id, dataset=dataset_name)

        return _run_async(_forget())


class CogneeMemoryBackend:
    def __init__(
        self, person_id: str, adapter: CogneeAdapter | None = None, top_k: int = 5
    ) -> None:
        self.person_id = person_id
        self.dataset_name = dataset_name_for_person(person_id)
        self.adapter = adapter or DefaultCogneeAdapter()
        self.top_k = top_k

    def recall(self, query: MemoryQuery) -> MemoryContext:
        query_text = _query_text(query)
        raw_results = self.adapter.recall(
            query_text=query_text,
            dataset_name=self.dataset_name,
            top_k=_recall_top_k(self.top_k),
        )
        query_payload = {**query.trace_payload(), "dataset": self.dataset_name}
        write_memory_recall_raw_trace(
            {
                "backend": "cognee",
                "person_id": query.person_id,
                "status": "ok",
                "error": {},
                "query": query_payload,
                "backend_request": {
                    "dataset_name": self.dataset_name,
                    "top_k": self.top_k,
                    "backend_top_k": _recall_top_k(self.top_k),
                    "query_text_excerpt": query_text[:1000],
                },
                "raw_count": len(raw_results),
                "raw_results": [
                    _raw_recall_trace_item(raw_result, index, self.dataset_name)
                    for index, raw_result in enumerate(raw_results)
                ],
            }
        )
        candidates: list[dict[str, Any]] = []
        for raw_index, raw_result in enumerate(raw_results):
            candidates.extend(
                _recall_candidates(
                    raw_result=raw_result,
                    raw_index=raw_index,
                    dataset_name=self.dataset_name,
                )
            )
        self._forget_expired_latest_candidates(query, candidates)
        items = _latest_items_by_id(
            candidate["item"]
            for candidate in candidates
            if candidate["decision"] == "kept"
        )[: self.top_k]
        write_memory_recall_final_trace(
            {
                "backend": "cognee",
                "person_id": query.person_id,
                "status": "ok",
                "error": {},
                "query": query_payload,
                "filter_policy": {
                    "drop_empty_content": True,
                    "drop_score_lte_zero": False,
                },
                "candidates": [
                    _final_recall_trace_candidate(candidate)
                    for candidate in candidates
                ],
                "hits": [
                    {
                        "id": item.id,
                        "title": item.title,
                        "score": item.score,
                    }
                    for item in items
                ],
            }
        )
        return MemoryContext(
            backend="cognee",
            person_id=query.person_id,
            query=query_payload,
            items=items,
        )

    def remember(self, update: MemoryUpdate) -> MemoryWriteResult:
        person_id = str(update.scope.get("person_id", self.person_id)).strip()
        item_id = _topic_id(update.topic_id or update.title)
        title = update.title.strip() or item_id.replace("-", " ").title()
        if not update.should_update or not update.memory.strip():
            return MemoryWriteResult(
                changed=False,
                backend="cognee",
                person_id=person_id,
                item_id=item_id,
                title=title,
                source=update.source,
                scope=update.scope,
                metadata={"dataset": self.dataset_name, **update.metadata},
                retention=update.retention,
            )

        metadata = {
            "guildbotics_memory_id": item_id,
            "title": title,
            "summary": update.summary.strip(),
            "source": update.source,
            "scope": update.scope,
            "metadata": update.metadata,
            "retention": update.retention,
            "dataset": self.dataset_name,
        }
        raw_result = self.adapter.remember(
            content=_memory_document(item_id, title, update, self.dataset_name),
            dataset_name=self.dataset_name,
            item_id=item_id,
            data_id=memory_data_id(self.dataset_name, item_id),
            metadata=metadata,
        )
        raw_status = str(_raw_attr(raw_result, "status", "") or "")
        error = _cognee_write_error(raw_result)
        result_metadata = {
            "dataset": self.dataset_name,
            **update.metadata,
            "raw_status": raw_status,
        }
        return MemoryWriteResult(
            changed=not error,
            backend="cognee",
            status="failed" if error else "ok",
            error=error,
            reference="" if error else _result_reference(raw_result),
            person_id=person_id,
            item_id=item_id,
            title=title,
            source=update.source,
            scope=update.scope,
            metadata=result_metadata,
            retention=update.retention,
        )

    def forget(self, request: MemoryForgetRequest) -> MemoryForgetResult:
        data_id = memory_data_id(self.dataset_name, request.item_id)
        try:
            raw_result = self.adapter.forget(
                data_id=data_id,
                dataset_name=self.dataset_name,
            )
        except Exception as exc:
            return MemoryForgetResult(
                changed=False,
                backend="cognee",
                status="failed",
                error={"type": exc.__class__.__name__, "message": str(exc)},
                person_id=request.person_id,
                item_id=request.item_id,
                source=request.source,
                scope=request.scope,
                metadata={
                    "dataset": self.dataset_name,
                    "data_id": str(data_id),
                    **request.metadata,
                    "reason": request.reason,
                },
            )
        status = str(_raw_attr(raw_result, "status", "") or "").strip().lower()
        error = (
            {}
            if status in {"", "success", "completed"}
            else {
                "type": "CogneeForgetError",
                "message": f"cognee forget returned status={status}",
            }
        )
        return MemoryForgetResult(
            changed=not error,
            backend="cognee",
            status="failed" if error else "ok",
            error=error,
            reference=str(_raw_attr(raw_result, "data_id", "") or data_id),
            person_id=request.person_id,
            item_id=request.item_id,
            source=request.source,
            scope=request.scope,
            metadata={
                "dataset": self.dataset_name,
                "data_id": str(data_id),
                "raw_status": status,
                **request.metadata,
                "reason": request.reason,
            },
        )

    def _forget_expired_latest_candidates(
        self, query: MemoryQuery, candidates: list[dict[str, Any]]
    ) -> None:
        for candidate in _latest_candidates_by_id(candidates):
            if "expired_retention" not in candidate["drop_reasons"]:
                continue
            item = candidate["item"]
            result = self.forget(
                MemoryForgetRequest(
                    person_id=query.person_id,
                    item_id=item.id,
                    reason="memory retention expired",
                    source=item.source or query.source,
                    scope=item.scope or query.scope,
                    metadata={
                        "operation": "recall_expiry",
                        "expires_at": str(item.retention.get("expires_at", "")),
                    },
                )
            )
            write_memory_forget_trace(result)


class FakeMemoryBackend:
    _items_by_dataset: ClassVar[dict[str, list[MemoryItem]]] = {}

    def __init__(self, person_id: str) -> None:
        self.person_id = person_id
        self.dataset_name = dataset_name_for_person(person_id)

    def recall(self, query: MemoryQuery) -> MemoryContext:
        items = [
            item
            for item in self._items_by_dataset.get(self.dataset_name, [])
            if _fake_matches(query, item) and not _is_inactive_retention(item.retention)
            and not is_expired_retention(item.retention)
        ]
        return MemoryContext(
            backend="fake",
            person_id=query.person_id,
            query={**query.trace_payload(), "dataset": self.dataset_name},
            items=items,
        )

    def remember(self, update: MemoryUpdate) -> MemoryWriteResult:
        item_id = _topic_id(update.topic_id or update.title)
        title = update.title.strip() or item_id.replace("-", " ").title()
        person_id = str(update.scope.get("person_id", self.person_id)).strip()
        if not update.should_update or not update.memory.strip():
            return MemoryWriteResult(
                changed=False,
                backend="fake",
                person_id=person_id,
                item_id=item_id,
                title=title,
                source=update.source,
                scope=update.scope,
                metadata={"dataset": self.dataset_name, **update.metadata},
                retention=update.retention,
            )

        item = MemoryItem(
            id=item_id,
            title=title,
            summary=update.summary.strip(),
            path=f"cognee://{self.dataset_name}/{item_id}",
            content=update.memory.strip(),
            score=1.0,
            match_reason="Matched fake backend memory.",
            source=update.source,
            scope=update.scope,
            metadata={"dataset": self.dataset_name, **update.metadata},
            retention=update.retention,
        )
        items = self._items_by_dataset.setdefault(self.dataset_name, [])
        items[:] = [existing for existing in items if existing.id != item_id]
        items.append(item)
        return MemoryWriteResult(
            changed=True,
            backend="fake",
            reference=f"fake://{self.dataset_name}/{item_id}",
            person_id=person_id,
            item_id=item_id,
            title=title,
            source=update.source,
            scope=update.scope,
            metadata=item.metadata,
            retention=update.retention,
        )

    def forget(self, request: MemoryForgetRequest) -> MemoryForgetResult:
        items = self._items_by_dataset.setdefault(self.dataset_name, [])
        before = len(items)
        items[:] = [item for item in items if item.id != request.item_id]
        return MemoryForgetResult(
            changed=len(items) != before,
            backend="fake",
            reference=f"fake://{self.dataset_name}/{request.item_id}",
            person_id=request.person_id,
            item_id=request.item_id,
            source=request.source,
            scope=request.scope,
            metadata={"dataset": self.dataset_name, **request.metadata},
        )


def dataset_name_for_person(person_id: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z_-]+", "-", person_id.strip()).strip("-")
    return f"guildbotics:person:{normalized or 'unknown'}"


def memory_data_id(dataset_name: str, item_id: str) -> uuid.UUID:
    return uuid.uuid5(
        uuid.UUID("8be1d7e6-8c43-4a12-9a18-fc821c0f89be"),
        f"{dataset_name}:{item_id}",
    )


def configure_cognee_environment_from_guildbotics_keys() -> None:
    """Map GuildBotics API key env vars to Cognee's LLM/embedding env vars."""
    system_root = Path(
        os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", "~/.cognee/system")
    ).expanduser()
    logs_dir = Path(os.environ.setdefault("COGNEE_LOGS_DIR", "~/.cognee/logs")).expanduser()
    (system_root / "databases").mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    if llm_api_key := _env_value("LLM_API_KEY"):
        os.environ.setdefault("LLM_API_KEY", llm_api_key)
        _set_env_default("llm_api_key", llm_api_key)
        return
    if openai_api_key := _env_value("OPENAI_API_KEY"):
        os.environ.setdefault("LLM_PROVIDER", "openai")
        _set_env_default("llm_provider", "openai")
        os.environ["LLM_API_KEY"] = openai_api_key
        _set_env("llm_api_key", openai_api_key)
        os.environ.setdefault("EMBEDDING_PROVIDER", "openai")
        _set_env_default("embedding_provider", "openai")
        os.environ.setdefault("EMBEDDING_API_KEY", openai_api_key)
        _set_env_default("embedding_api_key", openai_api_key)
        return
    if google_api_key := _env_value("GOOGLE_API_KEY"):
        os.environ.setdefault("LLM_PROVIDER", "gemini")
        _set_env_default("llm_provider", "gemini")
        os.environ.setdefault("LLM_MODEL", "gemini/gemini-flash-latest")
        _set_env_default("llm_model", "gemini/gemini-flash-latest")
        os.environ["LLM_API_KEY"] = google_api_key
        _set_env("llm_api_key", google_api_key)
        os.environ.setdefault("EMBEDDING_PROVIDER", "gemini")
        _set_env_default("embedding_provider", "gemini")
        os.environ.setdefault("EMBEDDING_MODEL", "gemini/gemini-embedding-001")
        _set_env_default("embedding_model", "gemini/gemini-embedding-001")
        os.environ.setdefault("EMBEDDING_API_KEY", google_api_key)
        _set_env_default("embedding_api_key", google_api_key)
        return
    if anthropic_api_key := _env_value("ANTHROPIC_API_KEY"):
        os.environ.setdefault("LLM_PROVIDER", "anthropic")
        _set_env_default("llm_provider", "anthropic")
        os.environ["LLM_API_KEY"] = anthropic_api_key
        _set_env("llm_api_key", anthropic_api_key)


def _env_value(key: str) -> str:
    return os.getenv(key) or os.getenv(key.lower()) or ""


def _set_env(key: str, value: str) -> None:
    os.environ[key] = value


def _set_env_default(key: str, value: str) -> None:
    os.environ.setdefault(key, value)


def _run_async(coro: Any) -> Any:
    return asyncio.run_coroutine_threadsafe(coro, _get_cognee_loop()).result()


def _get_cognee_loop() -> asyncio.AbstractEventLoop:
    global _COGNEE_LOOP, _COGNEE_LOOP_THREAD

    with _COGNEE_LOOP_LOCK:
        if _COGNEE_LOOP is not None and _COGNEE_LOOP.is_running():
            return _COGNEE_LOOP

        loop = asyncio.new_event_loop()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(
            target=run_loop,
            name="guildbotics-cognee-event-loop",
            daemon=True,
        )
        thread.start()
        _COGNEE_LOOP = loop
        _COGNEE_LOOP_THREAD = thread
        return loop


async def _setup_cognee() -> None:
    from cognee.modules.engine.operations.setup import setup

    await setup()


async def _cognee_dataset_exists(dataset_name: str) -> bool:
    await _setup_cognee()

    from cognee.modules.data.methods.get_authorized_existing_datasets import (
        get_authorized_existing_datasets,
    )
    from cognee.modules.users.methods import get_default_user

    user = await get_default_user()
    datasets = await get_authorized_existing_datasets([dataset_name], "read", user)
    return bool(datasets)


def _query_text(query: MemoryQuery) -> str:
    parts = [
        "Recall GuildBotics personal memory relevant to this chat context.",
        "Prefer current/open facts for decisions, but include relevant transition memories as history.",
        "Do not treat superseded/resolved/rejected history as current policy.",
        f"Person: {query.person_id}",
        f"Thread topic: {query.thread_topic}",
        f"Latest focus: {query.latest_focus}",
        "Transcript:",
        query.transcript[:4000],
    ]
    return "\n".join(part for part in parts if part)


def _recall_top_k(top_k: int) -> int:
    return max(top_k, top_k * 3)


def _memory_document(
    item_id: str, title: str, update: MemoryUpdate, dataset_name: str
) -> str:
    updated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    header = {
        "memory_id": item_id,
        "title": title,
        "summary": update.summary.strip(),
        "source": update.source,
        "scope": update.scope,
        "metadata": update.metadata,
        "retention": update.retention,
        "dataset": dataset_name,
        "updated_at": updated_at,
    }
    lines = [
        f"guildbotics_memory_id: {item_id}",
        f"guildbotics_title: {title}",
        f"guildbotics_summary: {update.summary.strip()}",
        f"guildbotics_source: {json.dumps(update.source, ensure_ascii=False, sort_keys=True)}",
        f"guildbotics_scope: {json.dumps(update.scope, ensure_ascii=False, sort_keys=True)}",
        f"guildbotics_metadata: {json.dumps(update.metadata, ensure_ascii=False, sort_keys=True)}",
        f"guildbotics_retention: {json.dumps(update.retention, ensure_ascii=False, sort_keys=True)}",
        f"guildbotics_dataset: {dataset_name}",
        f"guildbotics_updated_at: {updated_at}",
        "",
        json.dumps({"guildbotics_memory": header}, ensure_ascii=False, sort_keys=True),
        "",
        update.memory.strip(),
    ]
    return "\n".join(lines)


def _memory_item_from_content(
    *,
    content: str,
    raw_result: Any,
    dataset_name: str,
    fallback_index: int,
    parsed_header: JsonMap | None = None,
) -> MemoryItem:
    header = parsed_header if parsed_header is not None else _parse_header(content)
    item_id = str(header.get("id", "")).strip() or _content_id(content, fallback_index)
    metadata = {
        "dataset": dataset_name,
        "backend_item_id": item_id,
        "raw_result_type": type(raw_result).__name__,
    }
    if updated_at := str(header.get("updated_at", "")).strip():
        metadata["updated_at"] = updated_at
    raw_dataset_id = _raw_attr(raw_result, "dataset_id", "")
    if raw_dataset_id:
        metadata["dataset_id"] = str(raw_dataset_id)
    raw_dataset_name = _raw_attr(raw_result, "dataset_name", "")
    if raw_dataset_name:
        metadata["dataset_name"] = str(raw_dataset_name)
    metadata.update(header.get("metadata", {}))
    return MemoryItem(
        id=item_id,
        title=str(header.get("title", "")).strip() or item_id.replace("-", " ").title(),
        summary=str(header.get("summary", "")).strip(),
        path=f"cognee://{dataset_name}/{item_id}",
        content=content,
        score=_result_score(raw_result),
        match_reason="Returned by Cognee recall.",
        source=header.get("source", {}),
        scope=header.get("scope", {}),
        metadata=metadata,
        retention=header.get("retention", {}),
    )


def _recall_candidates(
    *, raw_result: Any, raw_index: int, dataset_name: str
) -> list[dict[str, Any]]:
    raw_text = _result_text(raw_result)
    strict_header_validation = "__node_content_start__" in raw_text
    candidates: list[dict[str, Any]] = []
    for node_index, node_content in enumerate(_node_contents_from_raw_result(raw_result)):
        header = _parse_header(node_content)
        if not _has_valid_memory_header(header, strict=strict_header_validation):
            continue
        fallback_index = (raw_index * 1000) + node_index
        item = _memory_item_from_content(
            content=node_content,
            raw_result=raw_result,
            dataset_name=dataset_name,
            fallback_index=fallback_index,
            parsed_header=header,
        )
        drop_reasons = []
        if not node_content.strip():
            drop_reasons.append("empty_content")
        if _is_inactive_retention(item.retention):
            drop_reasons.append("inactive_retention")
        if is_expired_retention(item.retention):
            drop_reasons.append("expired_retention")
        candidates.append(
            {
                "item": item,
                "decision": "dropped" if drop_reasons else "kept",
                "drop_reasons": drop_reasons,
            }
        )
    return candidates


def _recall_candidate(
    raw_result: Any, fallback_index: int, dataset_name: str
) -> dict[str, Any]:
    # Backward-compatible helper for tests and single-content parsing.
    content = _result_text(raw_result)
    item = _memory_item_from_content(
        content=content,
        raw_result=raw_result,
        dataset_name=dataset_name,
        fallback_index=fallback_index,
    )
    drop_reasons = []
    if not content.strip():
        drop_reasons.append("empty_content")
    if _is_inactive_retention(item.retention):
        drop_reasons.append("inactive_retention")
    if is_expired_retention(item.retention):
        drop_reasons.append("expired_retention")
    return {
        "item": item,
        "decision": "dropped" if drop_reasons else "kept",
        "drop_reasons": drop_reasons,
    }


def _raw_recall_trace_item(
    raw_result: Any, index: int, dataset_name: str
) -> dict[str, Any]:
    content = _result_text(raw_result)
    strict_header_validation = "__node_content_start__" in content
    node_contents = _node_contents_from_raw_result(raw_result)
    parsed_nodes = []
    skipped_node_count = 0
    for node_index, node_content in enumerate(node_contents):
        header = _parse_header(node_content)
        if not _has_valid_memory_header(header, strict=strict_header_validation):
            skipped_node_count += 1
            continue
        parsed_nodes.append(
            _memory_item_from_content(
                content=node_content,
                raw_result=raw_result,
                dataset_name=dataset_name,
                fallback_index=(index * 1000) + node_index,
                parsed_header=header,
            )
        )
    item = parsed_nodes[0] if parsed_nodes else _memory_item_from_content(
        content=content,
        raw_result=raw_result,
        dataset_name=dataset_name,
        fallback_index=index,
    )
    return {
        "index": index,
        "raw_type": type(raw_result).__name__,
        "score": _result_score(raw_result),
        "dataset_id": str(_raw_attr(raw_result, "dataset_id", "") or ""),
        "dataset_name": str(_raw_attr(raw_result, "dataset_name", "") or ""),
        "content_excerpt": content[:1000],
        "node_count": len(node_contents),
        "skipped_node_count": skipped_node_count,
        "parsed_nodes": [
            {
                "id": node.id,
                "title": node.title,
                "summary": node.summary,
                "source": node.source,
                "scope": node.scope,
                "metadata": node.metadata,
                "retention": node.retention,
            }
            for node in parsed_nodes
        ],
        "parsed": {
            "id": item.id,
            "title": item.title,
            "summary": item.summary,
            "source": item.source,
            "scope": item.scope,
            "metadata": item.metadata,
            "retention": item.retention,
        },
    }


def _final_recall_trace_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    item = candidate["item"]
    return {
        "id": item.id,
        "title": item.title,
        "score": item.score,
        "decision": candidate["decision"],
        "drop_reasons": candidate["drop_reasons"],
        "kept": candidate["decision"] == "kept",
        "retention": item.retention,
    }


def _parse_header(content: str) -> JsonMap:
    focus_content = _content_after_node_marker(content)
    parsed = _parse_structured_header(focus_content)
    for line in focus_content.splitlines()[:40]:
        key, separator, value = line.partition(":")
        if not separator or not key.startswith("guildbotics_"):
            continue
        name = key.removeprefix("guildbotics_")
        stripped = value.strip()
        if name == "memory_id":
            normalized_id = _extract_memory_id(stripped)
            if normalized_id:
                parsed.setdefault("id", normalized_id)
        elif name in {"source", "scope", "metadata", "retention"}:
            parsed.setdefault(name, _json_map(stripped))
        elif name in {"title", "summary"}:
            parsed.setdefault(name, stripped)
        elif name == "updated_at":
            parsed.setdefault("updated_at", stripped)
    return parsed


def _content_after_node_marker(content: str) -> str:
    marker = "__node_content_start__"
    if marker not in content:
        return content
    return content.split(marker, 1)[1].strip()


def _node_contents_from_raw_result(raw_result: Any) -> list[str]:
    content = _result_text(raw_result)
    marker = "__node_content_start__"
    if marker not in content:
        return [content]
    parts = [part.strip() for part in content.split(marker)[1:]]
    node_contents = [part for part in parts if part]
    return node_contents or [content]


def _parse_structured_header(content: str) -> JsonMap:
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        memory = parsed.get("guildbotics_memory")
        if not isinstance(memory, dict):
            continue
        return _normalize_structured_header(memory)
    return {}


def _normalize_structured_header(memory: JsonMap) -> JsonMap:
    header: JsonMap = {}
    item_id = _extract_memory_id(str(memory.get("memory_id", "")).strip())
    if item_id:
        header["id"] = item_id
    title = str(memory.get("title", "")).strip()
    if title:
        header["title"] = title
    summary = str(memory.get("summary", "")).strip()
    if summary:
        header["summary"] = summary
    updated_at = str(memory.get("updated_at", "")).strip()
    if updated_at:
        header["updated_at"] = updated_at
    for key in ("source", "scope", "metadata", "retention"):
        value = memory.get(key)
        if isinstance(value, dict):
            header[key] = dict(value)
    return header


def _json_map(value: str) -> JsonMap:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_memory_id(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    # Handle collapsed lines such as:
    # guildbotics_memory_id: xxx guildbotics_title: ...
    text = re.split(r"\s+guildbotics_[a-z_]+:\s*", text, maxsplit=1)[0].strip()
    token = text.split()[0] if text else ""
    if not token:
        return ""
    return token


def _has_valid_memory_header(header: JsonMap, *, strict: bool) -> bool:
    if not _extract_memory_id(str(header.get("id", ""))):
        return False
    if not strict:
        return True
    if str(header.get("updated_at", "")).strip():
        return True
    for key in ("source", "scope", "metadata", "retention"):
        value = header.get(key)
        if isinstance(value, dict) and value:
            return True
    return False


def _result_text(raw_result: Any) -> str:
    payload = _raw_attr(raw_result, "search_result", raw_result)
    if isinstance(payload, dict):
        for key in ("text", "content", "answer", "result", "search_result"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if isinstance(payload, list):
        return "\n".join(_result_text(item) for item in payload)
    return str(payload)


def _result_score(raw_result: Any) -> float:
    score = _raw_attr(raw_result, "score", None)
    if score is None and isinstance(raw_result, dict):
        score = raw_result.get("score")
    try:
        return float(score)
    except (TypeError, ValueError):
        return 0.0


def _raw_attr(raw_result: Any, name: str, default: Any = None) -> Any:
    if isinstance(raw_result, dict):
        return raw_result.get(name, default)
    return getattr(raw_result, name, default)


def _latest_items_by_id(items: Any) -> list[MemoryItem]:
    latest: dict[str, MemoryItem] = {}
    for item in items:
        existing = latest.get(item.id)
        if existing is None or _updated_at(item) >= _updated_at(existing):
            latest[item.id] = item
    return list(latest.values())


def _latest_candidates_by_id(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        item = candidate["item"]
        existing = latest.get(item.id)
        if existing is None or _updated_at(item) >= _updated_at(existing["item"]):
            latest[item.id] = candidate
    return list(latest.values())


def _updated_at(item: MemoryItem) -> str:
    return str(item.metadata.get("updated_at", ""))


def _result_reference(raw_result: Any) -> str:
    for name in ("pipeline_run_id", "content_hash", "dataset_id", "status"):
        value = _raw_attr(raw_result, name, "")
        if value:
            return str(value)
    if raw_result:
        return type(raw_result).__name__
    return ""


def _cognee_write_error(raw_result: Any) -> JsonMap:
    raw_status = str(_raw_attr(raw_result, "status", "") or "").strip().lower()
    if raw_status and raw_status not in {"completed", "session_stored"}:
        message = str(_raw_attr(raw_result, "error", "") or "").strip()
        return {
            "type": "CogneeRememberError",
            "message": message or f"cognee remember returned status={raw_status}",
        }
    return {}


def _content_id(content: str, fallback_index: int) -> str:
    digest = hashlib.sha1(content.encode("utf-8")).hexdigest()[:12]
    return f"cognee-result-{fallback_index}-{digest}"


def _fake_matches(query: MemoryQuery, item: MemoryItem) -> bool:
    haystack = " ".join(
        [query.thread_topic, query.latest_focus, query.transcript]
    ).casefold()
    needles = [item.id, item.title, item.summary]
    return any(needle and needle.casefold() in haystack for needle in needles)


def _topic_id(value: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "-", value.lower()).strip("-")
    if normalized:
        return normalized[:80].strip("-") or "topic"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:12]


def _is_inactive_retention(retention: JsonMap) -> bool:
    status = str(retention.get("status", "")).strip().lower()
    return status in {
        "superseded",
        "resolved",
        "archived",
        "do_not_recall",
        "forgotten",
    }
