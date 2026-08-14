"""One-shot migration from a source-checkout workspace to a dedicated root."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from guildbotics.utils.fileio import load_yaml_dict, save_yaml_file
from guildbotics.utils.workspace_state import write_active_workspace

_STATE_DIRS = ("documents", "chat_state", "task-runs")
_LOCAL_DIRS = (
    ("run", ("run",)),
    ("clones", ("workspaces",)),
    ("agent-runtime", ("agent-runtime",)),
    ("work/command-authoring", ("command-authoring",)),
    ("work/troubleshooting", ("troubleshooting",)),
)


@dataclass(frozen=True)
class WorkspaceMigrationResult:
    source: Path
    destination: Path
    copied: list[str]
    skipped: list[str]
    notices: list[str] = field(default_factory=list)


def migrate_workspace(
    source: Path,
    destination: Path,
    *,
    activate: bool = True,
) -> WorkspaceMigrationResult:
    """Copy ``source/.guildbotics`` into a dedicated workspace root.

    The source checkout is not modified. Existing destination data is left
    untouched and never overwritten. When ``source`` and ``destination`` are
    the same directory, an already-dedicated workspace root is upgraded in
    place instead: the old ``data/`` layout is reclassified into ``state/``
    and ``local/`` (leaving ``data/`` untouched) and the old secrets index is
    converted.
    """
    source = source.expanduser().resolve(strict=False)
    destination = destination.expanduser().resolve(strict=False)
    in_place = source == destination
    source_guild = source / ".guildbotics"
    dest_guild = destination / ".guildbotics"
    if not source_guild.is_dir():
        raise FileNotFoundError(f"source workspace not found: {source_guild}")
    if not in_place and dest_guild.exists() and any(dest_guild.iterdir()):
        raise FileExistsError(f"destination already has workspace data: {dest_guild}")
    destination.mkdir(parents=True, exist_ok=True)
    dest_guild.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    skipped: list[str] = []
    if not in_place:
        _copy_tree(source_guild / "config", dest_guild / "config", copied, skipped)
    data_root = source_guild / "data"
    if data_root.is_dir():
        _reclassify_data(data_root, dest_guild, copied, skipped)
    elif not in_place:
        for name in ("state", "local"):
            _copy_tree(source_guild / name, dest_guild / name, copied, skipped)

    _split_chat_cache(dest_guild)
    _scrub_pending_events(dest_guild)
    _convert_secrets_index(dest_guild)
    _convert_diagnostics_domain_events(dest_guild)
    notices = _build_notices(source)
    if activate:
        write_active_workspace(destination)
    return WorkspaceMigrationResult(
        source=source,
        destination=destination,
        copied=copied,
        skipped=skipped,
        notices=notices,
    )


def _reclassify_data(
    data_root: Path,
    dest_guild: Path,
    copied: list[str],
    skipped: list[str],
) -> None:
    for name in _STATE_DIRS:
        _copy_tree(data_root / name, dest_guild / "state" / name, copied, skipped)
    for dest_rel, sources in _LOCAL_DIRS:
        for source_name in sources:
            _copy_tree(
                data_root / source_name,
                dest_guild / "local" / dest_rel,
                copied,
                skipped,
            )


def _convert_secrets_index(dest_guild: Path) -> None:
    """Rewrite an old list-format ``secrets.yml`` into the generation schema.

    The secret values themselves already live in this machine's OS keychain
    under the preserved ``store_id``; only the index format changed. The
    device-local generation record is created as well, so this device counts
    as holding every migrated generation.
    """
    from guildbotics.utils.secret_store import KeyringSecretStore, utc_now_iso

    index_file = dest_guild / "config" / "secrets.yml"
    if not index_file.is_file():
        return
    data = load_yaml_dict(index_file)
    keys = data.get("keys")
    if not isinstance(keys, list):
        # Already the generation schema: the copied local index (if any)
        # records what this device really holds and must not be overwritten.
        return
    now = utc_now_iso()
    save_yaml_file(
        index_file,
        {
            "store_id": str(data.get("store_id", "")),
            "keys": {
                str(key): {"generation": 1, "updated_at": now}
                for key in keys
                if str(key)
            },
        },
    )
    KeyringSecretStore(dest_guild / "config").adopt_shared_generations()


def _convert_diagnostics_domain_events(dest_guild: Path) -> None:
    """Extract domain events from the copied diagnostics log into the shared
    ``state/events/`` store, so pre-migration history stays synchronizable."""
    # Local import: the event schema is owned by observability, and this
    # one-shot converter is its only consumer inside utils.
    from guildbotics.observability.activity_event_store import (
        ActivityEventStore,
        is_domain_activity_event,
    )

    diagnostics_file = dest_guild / "local" / "run" / "diagnostics.jsonl"
    if not diagnostics_file.is_file():
        return
    store = ActivityEventStore(dest_guild / "state" / "events")
    with diagnostics_file.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or record.get("kind") != "event":
                continue
            if not is_domain_activity_event(str(record.get("type") or "")):
                continue
            store.record(record)


def _scrub_pending_events(dest_guild: Path) -> None:
    """Re-serialize copied pending events through the store.

    Old pending files may carry metadata written before the minimal-payload
    allowlist existed; rewriting them keeps only the fields the shared
    ``state/chat_state/`` schema permits.
    """
    # Local import: the pending-event schema is owned by the chat state store.
    from guildbotics.integrations.file_chat_state_store import (
        FileConversationStateStore,
    )

    state_root = dest_guild / "state" / "chat_state"
    if not state_root.is_dir():
        return
    store = FileConversationStateStore(
        base_dir=state_root, cache_dir=dest_guild / "local" / "chat-cache"
    )
    for pending_file in state_root.glob("*/*/pending_events/*.json"):
        service = pending_file.parents[2].name
        person_id = pending_file.parents[1].name
        channel_id = pending_file.stem
        events = store.load_pending_events(service, person_id, channel_id)
        pending_file.unlink()
        for pending in events:
            store.save_pending_event(service, person_id, channel_id, pending)


def _build_notices(source: Path) -> list[str]:
    env_file = source / ".env"
    if not env_file.is_file():
        return []
    return [
        "The source workspace has a .env file. GuildBotics no longer reads it: "
        f"import its secrets with `guildbotics secrets import {env_file}` and "
        "move non-secret settings (LOG_LEVEL, AGNO_DEBUG) to the Debug "
        "settings in Desktop."
    ]


def _split_chat_cache(dest_guild: Path) -> None:
    state_root = dest_guild / "state" / "chat_state"
    cache_root = dest_guild / "local" / "chat-cache"
    if not state_root.is_dir():
        return
    for thread_file in state_root.glob("*/*/threads/*/*.json"):
        try:
            payload = json.loads(thread_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or "messages" not in payload:
            continue
        messages = payload.pop("messages")
        thread_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        rel = thread_file.relative_to(state_root)
        cache_file = cache_root / rel
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(
            json.dumps(
                {"messages": messages},
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )


def _copy_tree(
    source: Path, destination: Path, copied: list[str], skipped: list[str]
) -> None:
    """Copy ``source`` into ``destination`` without ever overwriting.

    Existing directories are merged entry by entry so a partially populated
    destination (e.g. a workspace the new layout already wrote to) still
    receives every missing item; existing files are always kept.
    """
    if not source.exists():
        return
    if source.is_file():
        if destination.exists():
            skipped.append(str(destination))
            return
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(str(destination))
        return
    if not destination.exists():
        shutil.copytree(source, destination)
        copied.append(str(destination))
        return
    for child in sorted(source.iterdir()):
        _copy_tree(child, destination / child.name, copied, skipped)
