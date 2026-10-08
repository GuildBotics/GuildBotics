"""Read why each workspace key was last refused from this device's diagnostics.

Every inference call made with a workspace key (an LLM provider's, Jev's)
ends its span naming the key's service and provider and, when the provider
refused it, why. The latest such call per key is its state: a refusal shows
until a call of the same key succeeds.
"""

from datetime import datetime
from typing import Any

from guildbotics.app_api.models import InferenceFailuresResponse, InferenceFailureStatus
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.timestamps import parse_iso_datetime

#: The services whose calls use a workspace key.
KEY_SERVICES = frozenset({"llm", "jev"})


def inference_key(record: dict[str, Any]) -> tuple[str, str] | None:
    """The ``(service, provider)`` whose key an inference call's span used,
    or None for any other record."""
    if record.get("kind") != "event" or record.get("type") not in {
        "span.failed",
        "span.finished",
    }:
        return None
    attributes = record.get("attributes")
    if not isinstance(attributes, dict):
        return None
    service = attributes.get("credential.provider")
    if service not in KEY_SERVICES:
        return None
    return str(service), str(attributes.get("llm.provider") or "")


def latest_inference_failures(
    store: DiagnosticsStore | None,
) -> InferenceFailuresResponse:
    """The refusal each key's latest call ended with, if it was refused.

    A call that failed without the provider saying why (cancelled) tells
    nothing about the key, so it does not replace the previous outcome.
    """
    if store is None:
        return InferenceFailuresResponse()
    records, _ = store.records_after(
        None, includes=lambda item: inference_key(item) is not None
    )
    latest: dict[tuple[str, str], tuple[datetime, InferenceFailureStatus | None]] = {}
    for record in records:
        key = inference_key(record)
        timestamp = str(record.get("timestamp") or "")
        when = parse_iso_datetime(timestamp)
        attributes = record["attributes"]
        category = attributes.get("error.category")
        failed = record.get("type") == "span.failed"
        if key is None or when is None or (failed and not category):
            continue
        previous = latest.get(key)
        if previous is None or when >= previous[0]:
            latest[key] = (
                when,
                InferenceFailureStatus(
                    category=category,
                    timestamp=timestamp,
                    status_code=attributes.get("error.status_code"),
                    response=str(attributes.get("error.response") or ""),
                )
                if failed
                else None,
            )
    llm = {
        provider: failure
        for (service, provider), (_, failure) in latest.items()
        if service == "llm" and provider and failure
    }
    jev = latest.get(("jev", ""), (None, None))[1]
    return InferenceFailuresResponse(llm=llm, jev=jev)
