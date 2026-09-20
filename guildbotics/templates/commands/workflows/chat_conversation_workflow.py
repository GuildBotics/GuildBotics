from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from guildbotics.capabilities.chat_batch import completed_chat_event_ids
from guildbotics.capabilities.chat_updates import (
    check_chat_updates,
    ensure_chat_current,
)
from guildbotics.capabilities.completion_retry import run_with_completion_retry
from guildbotics.capabilities.member_chat import MemberChatCapabilityService
from guildbotics.capabilities.task_runs import RunStore
from guildbotics.capabilities.workflow_completion_events import (
    record_chat_dispatch_abandoned,
    record_workflow_completed,
)
from guildbotics.capabilities.workflow_rate_limits import (
    WorkflowRateLimit,
    record_workflow_rate_limited,
    workflow_rate_limit_from_exception,
    workflow_rate_limit_notice_text,
)
from guildbotics.entities.message import Message
from guildbotics.integrations.chat_service import (
    ChatEvent,
    ChatService,
)
from guildbotics.integrations.chat_state_store import (
    ConversationStateStore,
    ThreadContextUnavailableError,
    ThreadConversationState,
    ThreadHandoffState,
    ThreadMessageState,
    ThreadSystemNoticeState,
)
from guildbotics.integrations.chat_workflow_status import (
    WORKFLOW_STATUS_KIND,
    is_suppressed_chat_event,
    workflow_status_metadata,
)
from guildbotics.integrations.file_chat_state_store import FileConversationStateStore
from guildbotics.intelligences.decisions.assessment import assess
from guildbotics.intelligences.decisions.models import Selection
from guildbotics.runtime.event_listener import IncomingChatEvent
from guildbotics.utils.fileio import (
    get_member_clone_path,
    get_workspace_config_dir,
    get_workspace_root,
    get_workspace_state_path,
)
from guildbotics.utils.i18n_tool import t

CHAT_MAX_ATTEMPTS_ENV = "GUILDBOTICS_CHAT_MAX_ATTEMPTS"
_MAX_THREAD_CONTEXT_MESSAGES = 100
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
    """Handle unread thread messages, starting from the queued incoming event."""
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
        except ThreadContextUnavailableError:
            # Not a workflow failure: the dispatcher keeps the event pending
            # and backs off until the provider can serve the thread again.
            raise
        except Exception as exc:
            retry_context = _read_retry_context_from_context(context)
            if retry_context.is_final_attempt:
                record_chat_dispatch_abandoned(
                    event_id=incoming.event.event_id,
                    run_id=retry_context.run_id,
                    attempt_count=retry_context.attempt_count,
                    max_attempts=retry_context.max_attempts,
                    error=str(exc),
                    error_category=(
                        "rate_limited"
                        if workflow_rate_limit_from_exception(exc) is not None
                        else "failed"
                    ),
                )
                _log(
                    context,
                    "error",
                    "chat event abandoned after final attempt: "
                    "channel=%s event=%s error=%s",
                    incoming.channel_id,
                    incoming.event.event_id,
                    exc,
                )
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

    workspace_root = get_workspace_root()
    task_run_root = get_workspace_state_path("task-runs", workspace_root=workspace_root)
    recovered = _recorded_chat_completion(retry_context.run_id, task_run_root)
    cached_thread_messages = state_store.load_thread_messages(
        service_name, person_id, channel_id, event.thread_ts
    )
    participation = _chat_participation(chat_participation)
    # One provider snapshot serves both the participation decision and the
    # agent prompt: a second fetch could fail and lose the very context the
    # decision was based on.
    snapshot_events, snapshot_complete = await _fetch_thread_events(
        context=context, chat_service=chat_service, event=event
    )
    batch_events = (
        _collect_batch_events(
            event=event,
            snapshot_events=snapshot_events,
            state_store=state_store,
            service_name=service_name,
            person_id=person_id,
            processed_event_ids=set(channel_state.processed_event_ids),
        )
        if recovered is None
        else [event]
    )
    thread_has_mentioned_self = _events_mention_user(
        snapshot_events, identity_user_id
    ) or _thread_has_mentioned_user(cached_thread_messages, identity_user_id)
    if not snapshot_complete and recovered is None:
        raise ThreadContextUnavailableError(
            "The current thread snapshot is unavailable; cached messages "
            "cannot establish whether a request has been corrected."
        )
    if recovered is None and all(
        _should_skip_event(
            participation=participation,
            mentions=list(item.mentions),
            latest_mentions_self=identity_user_id in item.mentions,
            thread_has_mentioned_self=thread_has_mentioned_self,
        )
        for item in batch_events
        if not item.is_from_user(identity_user_id)
    ):
        state_store.mark_processed_events(
            service_name,
            person_id,
            channel_id,
            [item.event_id for item in batch_events],
        )
        return

    # Persist the snapshot into the device-local cache so retries and later
    # events keep this context even when the provider becomes unavailable.
    cached_ts = {message.message_ts for message in cached_thread_messages}
    for thread_event in [*snapshot_events, *batch_events]:
        if not thread_event.message_ts or thread_event.message_ts in cached_ts:
            continue
        state_store.append_thread_message(
            service_name,
            person_id,
            channel_id,
            event.thread_ts,
            _event_to_thread_message(event.thread_ts, thread_event),
        )
        cached_ts.add(thread_event.message_ts)

    thread_messages = state_store.load_thread_messages(
        service_name, person_id, channel_id, event.thread_ts
    )
    member_workspace = _get_chat_workspace_path(context)
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
        live_thread=(snapshot_events, snapshot_complete),
        batch_events=batch_events,
    )

    invoke = getattr(context, "invoke", None)
    if not callable(invoke):
        raise RuntimeError("Invoker function is not set.")

    current_run_id = retry_context.run_id
    decision = None
    reaction_target = next(
        item.message_ts
        for item in reversed(batch_events)
        if not item.is_from_user(identity_user_id)
    )
    decision_state = {
        **prompt_payload,
        "member": {
            "person_id": person_id,
            "roles": _handoff_roles(context.person),
            "profile": getattr(context.person, "profile", {}),
        },
        "service": service_name,
        "channel_id": channel_id,
        "thread_ts": event.thread_ts,
        "event_ids": [item.event_id for item in batch_events],
        "reaction_target": reaction_target,
    }
    # Operational evidence changes during recovery; the conversation does not.
    input_hash = hashlib.sha256(
        json.dumps(decision_state, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()

    async def _invoke_chat_turn(run_id: str, _attempt: int) -> None:
        nonlocal current_run_id, decision
        current_run_id = run_id
        if _attempt == 1:
            # Persist membership before the agent can complete. Recovery uses
            # this exact input even if the thread advanced before restart.
            RunStore(task_run_root).append_evidence(
                run_id,
                "chat_batch",
                {
                    "event_ids": [item.event_id for item in batch_events],
                    "context_cursor": batch_events[-1].message_ts,
                    "person_id": person_id,
                    "service": service_name,
                    "channel_id": channel_id,
                    "thread_ts": event.thread_ts,
                    "self_user_id": identity_user_id,
                },
            )
        if decision is None:
            evidence = RunStore(task_run_root).evidence(run_id)
            saved = next(
                (
                    item["payload"]
                    for item in reversed(evidence)
                    if item["evidence_type"] == "chat_decision"
                ),
                None,
            )
            if (
                saved is not None
                and saved.get("input_hash") == input_hash
                and saved.get("route") != "agent"
            ):
                decision = Selection(
                    route=saved["route"],
                    reason=saved["reason"],
                    reaction=saved["reaction"],
                )
        if decision is None:
            config_dir = get_workspace_config_dir()
            decision, evaluation_id = await assess(
                {
                    **decision_state,
                    "run_id": run_id,
                    "previous_outcomes": RunStore(task_run_root).evidence(run_id),
                },
                None,
                config_dir=config_dir,
                person_id=person_id,
                logger=context.logger,
                brain_factory=context.brain_factory,
            )
            RunStore(task_run_root).append_evidence(
                run_id,
                "chat_decision",
                {
                    "evaluation_id": evaluation_id,
                    "input_hash": input_hash,
                    **decision.model_dump(),
                },
            )
        if decision.route != "agent":
            updates = check_chat_updates(person_id, run_id)
            # Check provider text as well as the receive queue: edits/cancellations
            # can change the same message without introducing a new batch ID.
            latest, complete = await _fetch_thread_events(
                context=context, chat_service=chat_service, event=event
            )
            if (
                updates["status"] != "up_to_date"
                or not complete
                or latest != snapshot_events
            ):
                raise ThreadContextUnavailableError(
                    "Chat changed during evaluation or reception is unavailable; reconsider the pending batch."
                )
            ensure_chat_current(person_id, run_id)
            store = RunStore(task_run_root)
            if decision.route == "reaction-only":
                existing = any(
                    item["evidence_type"] == "chat_reaction"
                    and item["payload"].get("message_ts") == reaction_target
                    and item["payload"].get("reaction") == decision.reaction
                    for item in store.evidence(run_id)
                )
                if not existing:
                    service = MemberChatCapabilityService(
                        context.person,
                        context.team,
                        context.logger,
                        chat_service,
                        service_name=service_name,
                    )
                    await service.add_reaction(
                        channel_id=channel_id,
                        channel_name=None,
                        message_ts=reaction_target,
                        reaction=decision.reaction,
                        run_id=run_id,
                    )
            else:
                store.append_evidence(
                    run_id,
                    "chat_noop",
                    {
                        "service": service_name,
                        "channel_id": channel_id,
                        "thread_ts": event.thread_ts,
                        "event_id": event.event_id,
                        "reason": decision.reason,
                        "noop": True,
                    },
                )
            ensure_chat_current(person_id, run_id)
            store.complete_run(
                run_id,
                "done",
                decision.reason,
                subject_type="chat",
                subject_id=f"{service_name}:{channel_id}:{event.thread_ts}:{event.event_id}",
                person_id=person_id,
            )
            return
        logical_attempt = retry_context.attempt_count + _attempt - 1
        execution_context = {
            "run_id": run_id,
            "workspace_data_root": str(workspace_root),
            "work_kind": "chat",
            "work_identity": ":".join(
                (
                    service_name,
                    identity_user_id,
                    channel_id,
                    event.thread_ts or event.message_ts,
                )
            ),
            "resume_policy": "auto",
            "context_cursor": batch_events[-1].message_ts,
            "event_id": event.event_id,
            "attempt": logical_attempt,
            "rebuild_context": json.dumps(
                prompt_payload["thread_context"], ensure_ascii=False, sort_keys=True
            ),
            "rebuild_context_complete": prompt_payload["thread_context_complete"],
            "continuation_input": t(
                "commands.workflows.common.agent_chat_continuation",
                run_id=run_id,
                event_id=event.event_id,
            ),
            "participant_labels": json.dumps(
                prompt_payload["participant_labels"],
                ensure_ascii=False,
                sort_keys=True,
            ),
        }
        await invoke(
            "functions/handle_chat_event",
            person_id=person_id,
            # The thread content is passed as named parameters below, so the
            # prompt must not also inherit `Context.pipe` as the user's message.
            message="",
            workflow_contract=t(
                "commands.workflows.common.workflow_contract",
                person_id=person_id,
            ),
            workflow_run_id=run_id,
            service_name=service_name,
            channel_id=channel_id,
            event_id=event.event_id,
            message_ts=reaction_target,
            thread_ts=event.thread_ts,
            previous_attempt_evidence=json.dumps(
                [
                    item
                    for item in RunStore(task_run_root).evidence(run_id)
                    if item["evidence_type"] != "chat_batch"
                ],
                ensure_ascii=False,
                sort_keys=True,
            ),
            unprocessed_messages=json.dumps(
                prompt_payload["unprocessed_messages"],
                ensure_ascii=False,
                sort_keys=True,
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
            agent_execution_context=execution_context,
            cwd=member_workspace,
        )

    # Retry the agent in-process until it records a terminal completion, then
    # escalate to the thread and stop. This is the single retry mechanism (the
    # ticket workflow uses the same helper); the outer pending queue is left only
    # as a crash-recovery net.
    try:
        if recovered is not None:
            record_workflow_completed(
                run_id=retry_context.run_id,
                attempt=retry_context.attempt_count,
                recovered=True,
            )
            _log(
                context,
                "info",
                "chat run already completed; skipping agent re-invocation: "
                "run=%s event=%s",
                retry_context.run_id,
                event.event_id,
            )
            completion, evidence = recovered
        else:
            (completion, evidence), _run_id = await run_with_completion_retry(
                invoke=_invoke_chat_turn,
                check_completion=lambda rid: _chat_run_status(
                    rid,
                    task_run_root,
                ),
                max_attempts=_IN_DISPATCH_COMPLETION_ATTEMPTS,
                run_id=retry_context.run_id or None,
                retry_invoke_exceptions=False,
            )
    except ThreadContextUnavailableError:
        raise
    except Exception as exc:
        if decision is not None and decision.route != "agent":
            # An unconfirmed fast path does not consume this input, including
            # on the dispatcher's final attempt. Once judgment delegates to the
            # agent, normal bounded retries and escalation apply.
            raise ThreadContextUnavailableError(
                "Chat judgment could not be completed; the batch remains pending."
            ) from exc
        rate_limit = workflow_rate_limit_from_exception(exc)
        if rate_limit is not None:
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
                retry_after_at=rate_limit.retry_after_at,
                retry_after_text=rate_limit.retry_after_text,
            )
            record_workflow_rate_limited(
                person_id=person_id,
                command="workflows/chat_conversation_workflow",
                run_id=run_id,
                source_event_id=event.event_id,
                retry_after=rate_limit,
                default_source="event_listener",
            )
            raise
        if retry_context.is_final_attempt:
            record_chat_dispatch_abandoned(
                event_id=event.event_id,
                run_id=retry_context.run_id,
                attempt_count=retry_context.attempt_count,
                max_attempts=retry_context.max_attempts,
                error=str(exc),
                error_category=(
                    "rate_limited"
                    if workflow_rate_limit_from_exception(exc) is not None
                    else "failed"
                ),
            )
            _log(
                context,
                "error",
                "chat event abandoned after final attempt: "
                "channel=%s event=%s error=%s",
                channel_id,
                event.event_id,
                exc,
            )
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
    _log(
        context,
        "info",
        "chat completion=%s evidence=%s channel=%s thread=%s event=%s",
        completion.status,
        completion.evidence_types,
        channel_id,
        event.thread_ts,
        event.event_id,
    )

    state_store.mark_processed_events(
        service_name,
        person_id,
        channel_id,
        [event.event_id, *completed_chat_event_ids(evidence, completion.status)],
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
        attempt_count=1,
        max_attempts=max_attempts,
        is_final_attempt=False,
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
    if isinstance(invocation, WorkflowInvocation):
        return invocation.payload.get("retry_context")
    return None


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
        return workflow_rate_limit_notice_text(
            WorkflowRateLimit(
                retry_after_at=retry_after_at, retry_after_text=retry_after_text
            )
        )
    return t("commands.workflows.chat_conversation_workflow.incomplete_escalation")


def _collect_batch_events(
    *,
    event: ChatEvent,
    snapshot_events: list[ChatEvent],
    state_store: ConversationStateStore,
    service_name: str,
    person_id: str,
    processed_event_ids: set[str],
) -> list[ChatEvent]:
    """Freeze unread input from the queue and the provider's current snapshot.

    The trigger is the oldest queued event. Older provider history is context,
    not new work (in particular after a receive reset). Pending events remain
    input even when the provider snapshot omits them. Latest provider text wins
    when the same event was edited since reception.
    """
    queued = state_store.load_pending_events(service_name, person_id, event.channel_id)
    events = {
        item.event_id: item
        for item in [event, *(pending.event for pending in queued), *snapshot_events]
        if item.channel_id == event.channel_id
        and item.thread_ts == event.thread_ts
        and _split_timestamp(item.message_ts) >= _split_timestamp(event.message_ts)
        and item.event_id not in processed_event_ids
        and not item.is_edit_or_delete
        and not is_suppressed_chat_event(item)
    }
    return sorted(events.values(), key=lambda item: _split_timestamp(item.message_ts))


async def _build_agent_prompt_payload(
    *,
    context: Any,
    chat_service: ChatService,
    event: ChatEvent,
    thread_messages: list[ThreadMessageState],
    self_user_id: str,
    thread_state: ThreadConversationState,
    chat_participation: str = "strict",
    live_thread: tuple[list[ChatEvent], bool] | None = None,
    batch_events: list[ChatEvent] | None = None,
) -> dict[str, Any]:
    batch_events = batch_events if batch_events is not None else [event]
    person_labels = await _chat_user_to_person_labels(context)
    author_labels = _build_author_labels(
        context, self_user_id, event, thread_messages[-20:], person_labels
    )

    previous_thread_context = {
        "thread_topic": thread_state.thread_topic,
        "latest_focus": thread_state.latest_focus,
        "handoffs": [_handoff_to_prompt_dict(item) for item in thread_state.handoffs],
    }
    cached_thread_context = [
        _message_to_prompt_dict(
            _to_prompt_message_from_state(
                message, self_user_id, author_labels, chat_service
            )
        )
        for message in thread_messages
    ]
    if live_thread is None:
        live_thread = await _fetch_thread_events(
            context=context, chat_service=chat_service, event=event
        )
    live_events, thread_context_complete = live_thread
    thread_context = _merge_thread_context(
        live_events,
        cached_thread_context=cached_thread_context,
        self_user_id=self_user_id,
        author_labels=author_labels,
        chat_service=chat_service,
        batch_events=batch_events,
    )
    return {
        "unprocessed_messages": [
            _message_to_prompt_dict(
                _to_prompt_message_from_event(
                    item, self_user_id, author_labels, chat_service
                )
            )
            for item in batch_events
        ],
        "participant_labels": author_labels,
        "handoff_candidates": _build_handoff_candidates(context, person_labels),
        "chat_participation": _chat_participation(chat_participation),
        "previous_thread_context": previous_thread_context,
        "thread_context": thread_context,
        "thread_context_complete": thread_context_complete,
    }


async def _fetch_thread_events(
    *,
    context: Any,
    chat_service: ChatService,
    event: ChatEvent,
) -> tuple[list[ChatEvent], bool]:
    """Fetch a provider snapshot once, retaining every unread message.

    Keep bounded history before the trigger and every message from the trigger
    onward. Bounding that second part could drop an unread request preceding
    its correction. One fetch serves participation and the agent input.
    """
    events: dict[str, ChatEvent] = {}
    cursor: str | None = None
    seen_cursors: set[str] = set()
    try:
        while True:
            page = await chat_service.list_thread_events(
                event.channel_id,
                thread_ts=event.thread_ts or event.message_ts,
                cursor=cursor,
                limit=_MAX_THREAD_CONTEXT_MESSAGES,
            )
            for thread_event in page.events:
                if thread_event.message_ts:
                    events[thread_event.message_ts] = thread_event
            events = dict(
                sorted(
                    events.items(),
                    key=lambda item: _split_timestamp(item[0]),
                )
            )
            # Bound history without truncating unread requests.
            previous = [
                ts
                for ts in events
                if _split_timestamp(ts) < _split_timestamp(event.message_ts)
            ]
            for ts in previous[:-_MAX_THREAD_CONTEXT_MESSAGES]:
                del events[ts]
            next_cursor = str(page.cursor or "")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise RuntimeError("Chat thread pagination cursor repeated.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
    except Exception as exc:
        _log(context, "warning", "live chat thread context unavailable: %s", exc)
        return list(events.values()), False
    return list(events.values()), True


def _events_mention_user(events: list[ChatEvent], user_id: str) -> bool:
    if not user_id:
        return False
    for thread_event in events:
        if user_id in set(thread_event.mentions):
            return True
        if user_id in _mentioned_user_ids_from_text(thread_event.text or ""):
            return True
    return False


def _event_to_thread_message(
    thread_ts: str, thread_event: ChatEvent
) -> ThreadMessageState:
    return ThreadMessageState(
        channel_id=thread_event.channel_id,
        thread_ts=thread_ts,
        message_ts=thread_event.message_ts,
        author_id=thread_event.author_id,
        text=thread_event.text,
        mentions=list(thread_event.mentions),
        is_bot_message=thread_event.is_bot_message,
    )


def _merge_thread_context(
    live_events: list[ChatEvent],
    *,
    cached_thread_context: list[dict[str, str]],
    self_user_id: str,
    author_labels: dict[str, str],
    chat_service: ChatService,
    batch_events: list[ChatEvent],
) -> list[dict[str, str]]:
    messages = {
        message.get("timestamp", ""): message
        for message in cached_thread_context[-_MAX_THREAD_CONTEXT_MESSAGES:]
        if message.get("timestamp")
    }
    for thread_event in live_events:
        prompt_message = _to_prompt_message_from_event(
            thread_event, self_user_id, author_labels, chat_service
        )
        messages[prompt_message.timestamp] = _message_to_prompt_dict(prompt_message)
    batch_timestamps = {item.message_ts for item in batch_events}
    cutoff = _split_timestamp(batch_events[-1].message_ts)
    return _bounded_thread_context(
        {
            timestamp: message
            for timestamp, message in messages.items()
            if timestamp not in batch_timestamps
            and _split_timestamp(timestamp) < cutoff
        }
    )


def _bounded_thread_context(
    messages: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    ordered = sorted(messages.values(), key=lambda message: _timestamp_key(message))
    return ordered[-_MAX_THREAD_CONTEXT_MESSAGES:]


def _timestamp_key(message: dict[str, str]) -> tuple[int, ...]:
    return _split_timestamp(message.get("timestamp", ""))


def _split_timestamp(timestamp: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in timestamp.split("."))
    except ValueError:
        return (0,)


def _recorded_chat_completion(
    run_id: str, task_run_root: Path
) -> tuple[Any, list[dict[str, Any]]] | None:
    """Return the completion a re-dispatched run already recorded, if any.

    A crash between the agent recording `chat complete` and the dispatcher
    marking the event processed leaves a completed run in the pending queue.
    Re-running the agent for it would repeat external actions (replies,
    reactions), so such a run must resume from its recorded evidence instead.
    """
    if not run_id:
        return None
    try:
        return _chat_run_status(run_id, task_run_root)
    except Exception:
        return None


def _chat_run_status(
    run_id: str, task_run_root: Path
) -> tuple[Any, list[dict[str, Any]]]:
    store = RunStore(task_run_root)
    return store.status(run_id), store.evidence(run_id)


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


def _get_chat_workspace_path(context: Any) -> Path | None:
    person_id = str(getattr(getattr(context, "person", None), "person_id", "")).strip()
    if not person_id:
        return None
    path = get_member_clone_path(person_id)
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
        "timestamp": message.timestamp,
    }


def _log(context: Any, level: str, msg: str, *args: Any) -> None:
    logger = getattr(context, "logger", None)
    if logger is None:
        return
    try:
        getattr(logger, level)(msg, *args)
    except Exception:
        return


def _read_incoming_event_from_context(context: Any) -> IncomingChatEvent | None:
    shared_state = getattr(context, "shared_state", None)
    if not isinstance(shared_state, dict):
        return None

    from guildbotics.runtime.workflow_invocation import (
        WORKFLOW_INVOCATION_KEY,
        WorkflowInvocation,
    )

    invocation = shared_state.get(WORKFLOW_INVOCATION_KEY)
    if isinstance(invocation, WorkflowInvocation) and invocation.trigger_type == "chat":
        return IncomingChatEvent.from_shared_state(invocation.payload)
    return None
