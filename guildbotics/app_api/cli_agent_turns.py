"""Read the last CLI turn per member and tool from this device's diagnostics."""

from datetime import datetime

from guildbotics.app_api.models import CliAgentLastTurn
from guildbotics.observability.diagnostics_store import DiagnosticsStore
from guildbotics.utils.timestamps import parse_iso_datetime


def last_cli_agent_turns(store: DiagnosticsStore | None) -> list[CliAgentLastTurn]:
    """Aggregate the retained index, including rows outside its in-memory tail.

    Tool identity and model selection belong to the recorded turn. Current
    slot settings cannot reconstruct either fact after configuration changes.
    """
    if store is None:
        return []
    records, _ = store.records_after(
        None,
        includes=lambda item: (
            item.get("type") in {"span.finished", "span.failed"}
            and item.get("attributes", {}).get("agent.kind") == "cli_agent"
        ),
    )
    latest: dict[tuple[str, str], tuple[datetime, CliAgentLastTurn]] = {}
    for record in records:
        person_id = record.get("person_id", "")
        agent = record.get("attributes", {}).get("agent.adapter", "")
        payload = record.get("payload", {})
        specified = payload.get("model_specified")
        timestamp = record.get("timestamp", "")
        when = parse_iso_datetime(timestamp)
        if not person_id or not agent or type(specified) is not bool or when is None:
            continue
        key = (person_id, agent)
        previous = latest.get(key)
        if previous is None or when >= previous[0]:
            latest[key] = (
                when,
                CliAgentLastTurn(
                    person_id=person_id,
                    agent=agent,
                    model=payload.get("model", ""),
                    model_specified=specified,
                    timestamp=timestamp,
                ),
            )
    return [latest[key][1] for key in sorted(latest)]
