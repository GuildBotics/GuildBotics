from __future__ import annotations

import json
import os
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guildbotics.capabilities.completion_retry import (
    CLI_AGENT_CONVERSATION_FILE_ENV,
    find_cli_agent_execution_error,
    run_with_completion_retry,
)
from guildbotics.capabilities.task_runs import RUN_ENV, RunStore
from guildbotics.entities.message import Message
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatService,
)
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    ThreadConversationState,
    ThreadHandoffState,
    ThreadMessageState,
    ThreadSystemNoticeState,
)
from guildbotics.integrations.chat_workflow_status import (
    WORKFLOW_STATUS_KIND,
    workflow_status_metadata,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.observability.diagnostics_events import record_correlated_event
from guildbotics.runtime.event_listener import (
    INCOMING_CHAT_EVENT_KEY,
    IncomingChatEvent,
)
from guildbotics.utils.fileio import (
    GUILDBOTICS_DATA_DIR,
    get_workspace_data_root,
)
from guildbotics.utils.i18n_tool import t

CHAT_PARTICIPANT_LABELS_ENV = "GUILDBOTICS_CHAT_PARTICIPANT_LABELS"
CHAT_MAX_ATTEMPTS_ENV = "GUILDBOTICS_CHAT_MAX_ATTEMPTS"
_SLACK_MENTION_RE = re.compile(r"<@([^>|]+)(?:\|[^>]+)?>")
_MAX_HANDOFF_TEXT_LENGTH = 240
_DEFAULT_MAX_ATTEMPTS = 5
_IN_DISPATCH_COMPLETION_ATTEMPTS = 2


@dataclass(frozen=True)
class RetryContext:
    attempt_count: int
    max_attempts: int
    is_final_attempt: bool
    run_id: str = ""


def _max_agent_attempts() -> int:
    """Number of times one chat event may be (re)dispatched before escalating.

    A turn that leaves no terminal completion record is retried so a slow or
    multi-turn AI CLI tool can finish; the budget bounds that so a permanently
    failing turn cannot loop forever.
    """
    raw = os.getenv(CHAT_MAX_ATTEMPTS_ENV, "").strip()
    try:
        return max(1, int(raw)) if raw else _DEFAULT_MAX_ATTEMPTS
    except ValueError:
        return _DEFAULT_MAX_ATTEMPTS


async def _escalate_incomplete(
    *,
    chat_service: ChatService,
    state_store: ConversationStateStore,
    service_name: str,
    person_id: str,
    channel_id: str,
    thread_ts: str,
    source_event_id: str,
    run_id: str,
    thread_state: ThreadConversationState,
    reason: str = "failed",
    retry_after_at: str = "",
    retry_after_text: str = "",
) -> bool:
    """Post a thread reply when the agent could not complete within the budget."""
    if _has_system_notice(thread_state, WORKFLOW_STATUS_KIND, source_event_id):
        return False
    message = _workflow_status_notice_text(reason, retry_after_at, retry_after_text)
    try:
        result = await chat_service.post_message(
            channel_id,
            message,
            thread_ts=thread_ts,
            metadata=workflow_status_metadata(
                reason=reason,
                person_id=person_id,
                source_event_id=source_event_id,
                run_id=run_id,
                retry_after_at=retry_after_at,
                retry_after_text=retry_after_text,
            ),
        )
    except Exception:
        return False
    thread_state.system_notices.append(
        ThreadSystemNoticeState(
            kind=WORKFLOW_STATUS_KIND,
            reason=reason,
            person_id=person_id,
            source_event_id=source_event_id,
            message_ts=result.message_ts,
            run_id=run_id,
            retry_after_at=retry_after_at,
            retry_after_text=retry_after_text,
            recorded_at=datetime.now(UTC).isoformat(),
        )
    )
    state_store.save_thread_state(
        service_name,
        person_id,
        channel_id,
        thread_ts,
        thread_state,
    )
    return True


async def main(
    context: Any,
    chat_service: ChatService | None = None,
    state_store: ConversationStateStore | None = None,
) -> None:
    """React to one incoming chat event provided via Context.shared_state."""
    chat_service = chat_service or context.get_chat_service()
    state_store = state_store or FileConversationStateStore()
    incoming = _read_incoming_event_from_context(context)
    if incoming is not None:
        try:
            identity = await chat_service.get_bot_identity()
            await _handle_event(
                context=context,
                chat_service=chat_service,
                state_store=state_store,
                service_name=incoming.service_name,
                channel_id=incoming.channel_id,
                identity_user_id=identity.user_id,
                event=incoming.event,
                chat_participation=incoming.chat_participation,
            )
        except Exception:
            if _read_retry_context_from_context(context).is_final_attempt:
                state_store.mark_processed_event(
                    incoming.service_name,
                    context.person.person_id,
                    incoming.channel_id,
                    incoming.event.event_id,
                )
                return
            raise


async def _handle_event(
    *,
    context: Any,
    chat_service: ChatService,
    state_store: ConversationStateStore,
    service_name: str,
    channel_id: str,
    identity_user_id: str,
    event: ChatEvent,
    chat_participation: str = "strict",
) -> None:
    person_id = context.person.person_id
    thread_state = state_store.load_thread_state(
        service_name, person_id, channel_id, event.thread_ts
    )
    channel_state = state_store.load_channel_cursor(service_name, person_id, channel_id)
    already_processed = event.event_id in set(channel_state.processed_event_ids)
    retry_context = _read_retry_context_from_context(context)
    if event.is_edit_or_delete:
        if not already_processed:
            state_store.mark_processed_event(
                service_name, person_id, channel_id, event.event_id
            )
        return
    if already_processed:
        return
    if event.is_from_user(identity_user_id):
        state_store.mark_processed_event(
            service_name, person_id, channel_id, event.event_id
        )
        return

    thread_messages = state_store.load_thread_messages(
        service_name, person_id, channel_id, event.thread_ts
    )
    latest_mentions_self = identity_user_id in set(event.mentions)
    thread_has_mentioned_self = _thread_has_mentioned_user(
        thread_messages, identity_user_id
    )
    participation = _chat_participation(chat_participation)
    if _should_skip_event(
        participation=participation,
        mentions=list(event.mentions),
        latest_mentions_self=latest_mentions_self,
        thread_has_mentioned_self=thread_has_mentioned_self,
    ):
        state_store.mark_processed_event(
            service_name, person_id, channel_id, event.event_id
        )
        return

    if not already_processed:
        state_store.append_thread_message(
            service_name,
            person_id,
            channel_id,
            event.thread_ts,
            ThreadMessageState(
                channel_id=event.channel_id,
                thread_ts=event.thread_ts,
                message_ts=event.message_ts,
                author_id=event.author_id,
                text=event.text,
                mentions=list(event.mentions),
                is_bot_message=event.is_bot_message,
            ),
        )

    thread_messages = state_store.load_thread_messages(
        service_name, person_id, channel_id, event.thread_ts
    )
    workspace_data_root = get_workspace_data_root()
    member_workspace = _get_chat_workspace_path(context, workspace_data_root)
    if member_workspace is None:
        raise RuntimeError("Member workspace path could not be resolved.")
    prompt_payload = await _build_agent_prompt_payload(
        context=context,
        chat_service=chat_service,
        event=event,
        thread_messages=thread_messages,
        self_user_id=identity_user_id,
        thread_state=thread_state,
        chat_participation=participation,
    )

    invoke = getattr(context, "invoke", None)
    if not callable(invoke):
        raise RuntimeError("Invoker function is not set.")

    current_run_id = retry_context.run_id

    async def _invoke_chat_turn(run_id: str, _attempt: int) -> None:
        nonlocal current_run_id
        current_run_id = run_id
        # Pin this dispatch's agent conversation to a per-run file so retries
        # resume the same conversation by id (not whatever last ran in this cwd).
        # Ensure the parent exists so the agent can write the file even when the
        # first attempt records no evidence (task-runs not created yet).
        conversation_file = (
            workspace_data_root / "task-runs" / f"{run_id}.agy-conversation"
        )
        conversation_file.parent.mkdir(parents=True, exist_ok=True)
        cli_agent_env = {
            RUN_ENV: run_id,
            GUILDBOTICS_DATA_DIR: str(workspace_data_root),
            CHAT_PARTICIPANT_LABELS_ENV: json.dumps(
                prompt_payload["participant_labels"],
                ensure_ascii=False,
                sort_keys=True,
            ),
            CLI_AGENT_CONVERSATION_FILE_ENV: str(conversation_file),
        }
        await invoke(
            "functions/handle_chat_event",
            person_id=person_id,
            workflow_contract=t(
                "commands.workflows.common.workflow_contract",
                person_id=person_id,
            ),
            workflow_run_id=run_id,
            service_name=service_name,
            channel_id=channel_id,
            event_id=event.event_id,
            message_ts=event.message_ts,
            thread_ts=event.thread_ts,
            latest_message=json.dumps(
                prompt_payload["latest_message"], ensure_ascii=False, sort_keys=True
            ),
            participant_labels=json.dumps(
                prompt_payload["participant_labels"],
                ensure_ascii=False,
                sort_keys=True,
            ),
            previous_thread_context=json.dumps(
                prompt_payload["previous_thread_context"],
                ensure_ascii=False,
                sort_keys=True,
            ),
            handoff_candidates=json.dumps(
                prompt_payload["handoff_candidates"], ensure_ascii=False, sort_keys=True
            ),
            chat_participation=prompt_payload["chat_participation"],
            language=getattr(context, "language_name", ""),
            member_workspace=str(member_workspace),
            cli_agent_env=cli_agent_env,
            cwd=member_workspace,
        )

    # Retry the agent in-process until it records a terminal completion, then
    # escalate to the thread and stop. This is the single retry mechanism (the
    # ticket workflow uses the same helper); the outer pending queue is left only
    # as a crash-recovery net.
    try:
        (completion, evidence), _run_id = await run_with_completion_retry(
            invoke=_invoke_chat_turn,
            check_completion=lambda rid: _chat_run_status(
                rid, workspace_data_root / "task-runs", member_workspace
            ),
            max_attempts=_IN_DISPATCH_COMPLETION_ATTEMPTS,
            run_id=retry_context.run_id or None,
            retry_invoke_exceptions=False,
        )
    except Exception as exc:
        rate_limit_error = find_cli_agent_execution_error(exc, category="rate_limited")
        if rate_limit_error is not None:
            details = getattr(rate_limit_error, "details", {})
            retry_after_at = str(details.get("retry_after_at", "") or "")
            retry_after_text = str(details.get("retry_after_text", "") or "")
            run_id = current_run_id
            await _escalate_incomplete(
                chat_service=chat_service,
                state_store=state_store,
                service_name=service_name,
                person_id=person_id,
                channel_id=channel_id,
                thread_ts=event.thread_ts,
                source_event_id=event.event_id,
                run_id=run_id,
                thread_state=thread_state,
                reason="rate_limited",
                retry_after_at=retry_after_at,
                retry_after_text=retry_after_text,
            )
            _record_rate_limited(
                person_id=person_id,
                source_event_id=event.event_id,
                run_id=run_id,
                retry_after_at=retry_after_at,
                retry_after_text=retry_after_text,
            )
            state_store.mark_processed_event(
                service_name, person_id, channel_id, event.event_id
            )
            return
        if retry_context.is_final_attempt:
            await _escalate_incomplete(
                chat_service=chat_service,
                state_store=state_store,
                service_name=service_name,
                person_id=person_id,
                channel_id=channel_id,
                thread_ts=event.thread_ts,
                source_event_id=event.event_id,
                run_id=retry_context.run_id,
                thread_state=thread_state,
                reason="failed",
            )
            state_store.mark_processed_event(
                service_name, person_id, channel_id, event.event_id
            )
            return
        raise
    if hasattr(context, "logger"):
        with suppress(Exception):
            context.logger.info(
                "chat completion=%s evidence=%s channel=%s thread=%s event=%s",
                completion.status,
                completion.evidence_types,
                channel_id,
                event.thread_ts,
                event.event_id,
            )

    state_store.mark_processed_event(
        service_name, person_id, channel_id, event.event_id
    )
    posted = _latest_chat_post_evidence(evidence)
    mentioned_user_ids: list[str] = []
    if posted is not None:
        payload = posted.get("payload", {})
        text = str(payload.get("text", "")).strip()
        message_ts = str(payload.get("message_ts", "")).strip()
        thread_ts = str(payload.get("thread_ts", event.thread_ts)).strip()
        mentioned_user_ids = _mentioned_user_ids_from_text(text)
        if text and message_ts:
            state_store.append_thread_message(
                service_name,
                person_id,
                channel_id,
                thread_ts,
                ThreadMessageState(
                    channel_id=channel_id,
                    thread_ts=thread_ts,
                    message_ts=message_ts,
                    author_id=identity_user_id,
                    text=text,
                    mentions=mentioned_user_ids,
                    is_bot_message=True,
                ),
            )
            _record_handoffs(
                context=context,
                thread_state=thread_state,
                participant_labels=prompt_payload["participant_labels"],
                mentioned_user_ids=mentioned_user_ids,
                source_person_id=person_id,
                message_ts=message_ts,
                text=text,
            )
    # Only record the member as a thread participant when it took a visible
    # action (reply/post/reaction). noop / blocked completions leave no Slack
    # trace, so marking the member as a participant would wrongly bias future
    # follow-up decisions toward treating the thread as one it joined.
    reacted = any(record.get("evidence_type") == "chat_reaction" for record in evidence)
    if posted is not None or reacted:
        thread_state.participants.add(person_id)
        state_store.save_thread_state(
            service_name,
            person_id,
            channel_id,
            event.thread_ts,
            thread_state,
        )


def _read_retry_context_from_context(context: Any) -> RetryContext:
    raw = _read_retry_context_payload(context)
    max_attempts = _max_agent_attempts()
    if isinstance(raw, dict):
        attempt_count = _positive_int(raw.get("attempt_count"), 1)
        max_attempts = max(1, _positive_int(raw.get("max_attempts"), max_attempts))
        return RetryContext(
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            is_final_attempt=bool(
                raw.get("is_final_attempt", attempt_count >= max_attempts)
            ),
            run_id=str(raw.get("run_id", "") or ""),
        )
    return RetryContext(
        attempt_count=max_attempts,
        max_attempts=max_attempts,
        is_final_attempt=True,
    )


def _read_retry_context_payload(context: Any) -> object:
    shared_state = getattr(context, "shared_state", None)
    if not isinstance(shared_state, dict):
        return None
    from guildbotics.runtime.workflow_invocation import (
        WORKFLOW_INVOCATION_KEY,
        WorkflowInvocation,
    )

    invocation = shared_state.get(WORKFLOW_INVOCATION_KEY)
    if isinstance(invocation, dict):
        payload = invocation.get("payload")
        return payload.get("retry_context") if isinstance(payload, dict) else None
    if isinstance(invocation, WorkflowInvocation):
        return invocation.payload.get("retry_context")
    return shared_state.get("retry_context")


def _positive_int(value: object, default: int) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return max(1, parsed)


def _has_system_notice(
    thread_state: ThreadConversationState, kind: str, source_event_id: str
) -> bool:
    return any(
        notice.kind == kind and notice.source_event_id == source_event_id
        for notice in thread_state.system_notices
    )


def _workflow_status_notice_text(
    reason: str, retry_after_at: str = "", retry_after_text: str = ""
) -> str:
    if reason == "rate_limited":
        retry_after = retry_after_text or retry_after_at
        if retry_after:
            return t(
                "commands.workflows.chat_conversation_workflow.rate_limited_escalation_with_reset",
                retry_after=retry_after,
            )
        return t(
            "commands.workflows.chat_conversation_workflow.rate_limited_escalation"
        )
    return t("commands.workflows.chat_conversation_workflow.incomplete_escalation")


def _record_rate_limited(
    *,
    person_id: str,
    source_event_id: str,
    run_id: str,
    retry_after_at: str,
    retry_after_text: str,
) -> None:
    record_correlated_event(
        event_type="workflow.rate_limited",
        default_source="event_listener",
        person_id=person_id,
        command="workflows/chat_conversation_workflow",
        attributes={
            "error.category": "rate_limited",
            "rate_limit.retry_after_at": retry_after_at,
            "rate_limit.retry_after_text": retry_after_text,
        },
        payload={
            "category": "rate_limited",
            "retry_after_at": retry_after_at,
            "retry_after_text": retry_after_text,
            "source_event_id": source_event_id,
            "run_id": run_id,
        },
    )


async def _build_agent_prompt_payload(
    *,
    context: Any,
    chat_service: ChatService,
    event: ChatEvent,
    thread_messages: list[ThreadMessageState],
    self_user_id: str,
    thread_state: ThreadConversationState,
    chat_participation: str = "strict",
) -> dict[str, Any]:
    person_labels = await _chat_user_to_person_labels(context)
    author_labels = _build_author_labels(
        context, self_user_id, event, thread_messages[-20:], person_labels
    )
    prompt_latest_message = _to_prompt_message_from_event(
        event, self_user_id, author_labels, chat_service
    )

    previous_thread_context = {
        "thread_topic": thread_state.thread_topic,
        "latest_focus": thread_state.latest_focus,
        "handoffs": [_handoff_to_prompt_dict(item) for item in thread_state.handoffs],
    }
    return {
        "latest_message": _message_to_prompt_dict(prompt_latest_message),
        "participant_labels": author_labels,
        "handoff_candidates": _build_handoff_candidates(context, person_labels),
        "chat_participation": _chat_participation(chat_participation),
        "previous_thread_context": previous_thread_context,
    }


def _chat_run_status(
    run_id: str, task_run_root: Path, member_workspace: Path
) -> tuple[Any, list[dict[str, Any]]]:
    first_error: Exception | None = None
    stores = [
        RunStore(task_run_root),
        RunStore(member_workspace / ".guildbotics-data" / "task-runs"),
        RunStore(member_workspace / ".guildbotics" / "data" / "task-runs"),
    ]
    for store in stores:
        try:
            return store.status(run_id), store.evidence(run_id)
        except Exception as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error
    raise RuntimeError(f"Chat run '{run_id}' was not found.")


def _latest_chat_post_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    for record in reversed(evidence):
        if record.get("evidence_type") in {"chat_reply", "chat_post"}:
            return record
    return None


def _mentioned_user_ids_from_text(text: str) -> list[str]:
    return list(
        dict.fromkeys(match.group(1) for match in _SLACK_MENTION_RE.finditer(text))
    )


def _record_handoffs(
    *,
    context: Any,
    thread_state: ThreadConversationState,
    participant_labels: dict[str, str],
    mentioned_user_ids: list[str],
    source_person_id: str,
    message_ts: str,
    text: str,
) -> None:
    if not mentioned_user_ids:
        return
    roles_by_person = _roles_by_person(context)
    existing = {
        (handoff.person_id, handoff.message_ts) for handoff in thread_state.handoffs
    }
    for user_id in mentioned_user_ids:
        person_id = participant_labels.get(user_id, "")
        if (
            not person_id
            or person_id == source_person_id
            or person_id not in roles_by_person
        ):
            continue
        key = (person_id, message_ts)
        if key in existing:
            continue
        thread_state.handoffs.append(
            ThreadHandoffState(
                person_id=person_id,
                roles=roles_by_person.get(person_id, []),
                message_ts=message_ts,
                text=_truncate_handoff_text(text),
                thread_topic=thread_state.thread_topic,
                latest_focus=thread_state.latest_focus,
            )
        )
        existing.add(key)


def _roles_by_person(context: Any) -> dict[str, list[str]]:
    team = getattr(context, "team", None)
    members = getattr(team, "members", []) if team is not None else []
    roles: dict[str, list[str]] = {}
    for member in members:
        person_id = str(getattr(member, "person_id", "")).strip()
        if not person_id:
            continue
        member_roles = getattr(member, "roles", {}) or {}
        roles[person_id] = [str(role_id) for role_id in member_roles]
    return roles


def _truncate_handoff_text(text: str) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= _MAX_HANDOFF_TEXT_LENGTH:
        return normalized
    return normalized[: _MAX_HANDOFF_TEXT_LENGTH - 1].rstrip() + "..."


def _handoff_to_prompt_dict(handoff: ThreadHandoffState) -> dict[str, Any]:
    return {
        "person_id": handoff.person_id,
        "roles": handoff.roles,
        "message_ts": handoff.message_ts,
        "text": handoff.text,
        "thread_topic": handoff.thread_topic,
        "latest_focus": handoff.latest_focus,
    }


def _thread_has_mentioned_user(
    thread_messages: list[ThreadMessageState], user_id: str
) -> bool:
    if not user_id:
        return False
    return any(user_id in set(message.mentions) for message in thread_messages)


def _should_skip_event(
    *,
    participation: str,
    mentions: list[str],
    latest_mentions_self: bool,
    thread_has_mentioned_self: bool,
) -> bool:
    if participation == "muted":
        return not latest_mentions_self
    if mentions and not latest_mentions_self:
        return True
    if participation == "social":
        return False
    return not latest_mentions_self and not thread_has_mentioned_self


def _chat_participation(value: Any) -> str:
    participation = str(value or "strict").strip().lower()
    if participation in {"strict", "social", "muted"}:
        return participation
    return "strict"


def _get_chat_workspace_path(context: Any, workspace_data_root: Path) -> Path | None:
    person_id = str(getattr(getattr(context, "person", None), "person_id", "")).strip()
    if not person_id:
        return None
    path = workspace_data_root / "workspaces" / person_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _build_author_labels(
    context: Any,
    self_user_id: str,
    event: ChatEvent | None,
    thread_messages: list[ThreadMessageState],
    person_labels: dict[str, str],
) -> dict[str, str]:
    ordered_ids: list[str] = []
    bot_ids: set[str] = set()

    def register(user_id: str | None, *, is_bot: bool) -> None:
        if not user_id:
            return
        if user_id not in ordered_ids:
            ordered_ids.append(user_id)
        if is_bot:
            bot_ids.add(user_id)

    for message in thread_messages:
        register(message.author_id, is_bot=message.is_bot_message)
        for mention in message.mentions:
            register(mention, is_bot=False)
    if event is not None:
        register(event.author_id, is_bot=event.is_bot_message)
        for mention in event.mentions:
            register(mention, is_bot=False)

    self_person_id = str(
        getattr(getattr(context, "person", None), "person_id", "")
    ).strip()
    if not self_person_id:
        self_person_id = "self"

    labels: dict[str, str] = {}
    if self_user_id:
        labels[self_user_id] = self_person_id

    agent_index = 1
    user_index = 1
    for user_id in ordered_ids:
        if user_id in labels:
            continue
        mapped_person_id = person_labels.get(user_id)
        if mapped_person_id:
            labels[user_id] = mapped_person_id
            continue
        if user_id in bot_ids:
            labels[user_id] = f"agent_{agent_index}"
            agent_index += 1
            continue
        labels[user_id] = f"user_{user_index}"
        user_index += 1
    for user_id, person_id in person_labels.items():
        if user_id not in labels:
            labels[user_id] = person_id
    return labels


def _build_handoff_candidates(
    context: Any, person_labels: dict[str, str]
) -> list[dict[str, Any]]:
    team = getattr(context, "team", None)
    members = getattr(team, "members", []) if team is not None else []
    self_person_id = str(
        getattr(getattr(context, "person", None), "person_id", "")
    ).strip()
    mentionable_person_ids = set(person_labels.values())
    candidates: list[dict[str, Any]] = []
    for member in members:
        person_id = str(getattr(member, "person_id", "")).strip()
        if (
            not person_id
            or person_id == self_person_id
            or person_id not in mentionable_person_ids
        ):
            continue
        roles = _handoff_roles(member)
        if not roles:
            continue
        candidates.append(
            {
                "person_id": person_id,
                "name": str(getattr(member, "name", "")).strip(),
                "mention": f"@{person_id}",
                "roles": roles,
            }
        )
    return candidates


def _handoff_roles(member: Any) -> dict[str, dict[str, str]]:
    raw_roles = getattr(member, "roles", {}) or {}
    if not isinstance(raw_roles, dict):
        return {}

    roles: dict[str, dict[str, str]] = {}
    for fallback_id, role in raw_roles.items():
        role_id = str(getattr(role, "id", fallback_id)).strip()
        if not role_id:
            continue
        role_info = {
            key: value
            for key, value in {
                "summary": str(getattr(role, "summary", "")).strip(),
                "description": str(getattr(role, "description", "")).strip(),
            }.items()
            if value
        }
        roles[role_id] = role_info
    return roles


async def _chat_user_to_person_labels(context: Any) -> dict[str, str]:
    team = getattr(context, "team", None)
    members = getattr(team, "members", []) if team is not None else []
    return await _runtime_chat_user_to_person_labels(context, members)


async def _runtime_chat_user_to_person_labels(
    context: Any,
    members: list[Any],
) -> dict[str, str]:
    clone_for = getattr(context, "clone_for", None)
    if not callable(clone_for):
        return {}

    runtime_labels: dict[str, str] = {}
    for member in members:
        person_id = str(getattr(member, "person_id", "")).strip()
        if not person_id or person_id in runtime_labels.values():
            continue
        slack_user_id = str(
            (getattr(member, "account_info", {}) or {}).get("slack_user_id", "")
        ).strip()
        if slack_user_id:
            runtime_labels[slack_user_id] = person_id
            continue
        try:
            member_context = clone_for(member)
        except Exception:
            continue
        try:
            get_chat_service = getattr(member_context, "get_chat_service", None)
            if not callable(get_chat_service):
                continue
            service = get_chat_service()
            get_bot_identity = getattr(service, "get_bot_identity", None)
            if not callable(get_bot_identity):
                continue
            identity = await get_bot_identity()
            user_id = str(getattr(identity, "user_id", "")).strip()
            if user_id:
                runtime_labels[user_id] = person_id
        except Exception:
            continue
        finally:
            close = getattr(member_context, "aclose", None)
            if callable(close):
                try:
                    result = close()
                    if hasattr(result, "__await__"):
                        await result
                except Exception:
                    pass
    return runtime_labels


def _to_prompt_message_from_state(
    message: ThreadMessageState,
    self_user_id: str,
    author_labels: dict[str, str],
    chat_service: ChatService,
) -> Message:
    return Message(
        content=chat_service.normalize_participant_text(message.text, author_labels),
        author=_resolve_author_label(
            message.author_id, message.is_bot_message, author_labels
        ),
        author_type=_to_author_type(
            message.is_bot_message, message.author_id, self_user_id
        ),
        timestamp=message.message_ts,
    )


def _to_prompt_message_from_event(
    event: ChatEvent,
    self_user_id: str,
    author_labels: dict[str, str],
    chat_service: ChatService,
) -> Message:
    return Message(
        content=chat_service.normalize_participant_text(event.text, author_labels),
        author=_resolve_author_label(
            event.author_id, event.is_bot_message, author_labels
        ),
        author_type=_to_author_type(
            event.is_bot_message, event.author_id, self_user_id
        ),
        timestamp=event.message_ts,
    )


def _to_author_type(
    is_bot_message: bool, author_id: str | None, self_user_id: str
) -> str:
    if is_bot_message and author_id == self_user_id:
        return Message.ASSISTANT
    return Message.USER


def _resolve_author_label(
    author_id: str | None,
    is_bot_message: bool,
    author_labels: dict[str, str],
) -> str:
    if author_id:
        label = author_labels.get(author_id)
        if label:
            return label
    return "agent" if is_bot_message else "user"


def _message_to_prompt_dict(message: Message) -> dict[str, str]:
    return {
        "content": message.content,
        "author": message.author,
        "author_type": message.author_type,
    }


def _log_info(context: Any, msg: str, *args: Any) -> None:
    logger = getattr(context, "logger", None)
    if logger is None:
        return
    try:
        logger.info(msg, *args)
    except Exception:
        return


def _read_incoming_event_from_context(context: Any) -> IncomingChatEvent | None:
    shared_state = getattr(context, "shared_state", None)
    if not isinstance(shared_state, dict):
        return None

    # Try reading from WORKFLOW_INVOCATION_KEY
    from guildbotics.runtime.workflow_invocation import (
        WORKFLOW_INVOCATION_KEY,
        WorkflowInvocation,
    )

    invocation = shared_state.get(WORKFLOW_INVOCATION_KEY)
    if invocation is not None:
        if isinstance(invocation, dict) and invocation.get("trigger_type") == "chat":
            return IncomingChatEvent.from_shared_state(invocation.get("payload"))
        elif (
            isinstance(invocation, WorkflowInvocation)
            and invocation.trigger_type == "chat"
        ):
            return IncomingChatEvent.from_shared_state(invocation.payload)

    # Fallback to INCOMING_CHAT_EVENT_KEY
    return IncomingChatEvent.from_shared_state(
        shared_state.get(INCOMING_CHAT_EVENT_KEY)
    )
