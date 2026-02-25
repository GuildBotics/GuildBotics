from __future__ import annotations

from guildbotics.templates.commands.workflows.chat.policies.models import (
    PolicyDecision,
    PolicyInput,
)


class ShouldReactPolicy:
    """Rule-based policy for deciding whether a chat event should be handled."""

    def evaluate(self, input: PolicyInput) -> PolicyDecision:
        event = input.event
        if not event.is_message:
            return ignore("not_message")
        if event.is_edit_or_delete:
            return ignore("unsupported_message_subtype")
        if not event.is_in_subscribed_channel:
            return ignore("channel_not_subscribed")
        if input.state.already_processed:
            return ignore("already_processed")
        if event.is_from_self:
            return ignore("self_message")

        me = mentions_me(input)
        others = mentions_non_self_users(input)

        if event.is_bot_message:
            return ignore("bot_message_ignored_in_mvp")

        if input.thread_context.bot_auto_turn_count >= input.limits.max_bot_auto_turns:
            return react_only("bot_loop_limit", "hand")
        if input.thread_context.too_many_recent_bot_replies:
            return react_only("bot_reply_cooldown", "hourglass_flowing_sand")

        if me:
            return reply("explicit_mention")
        if others:
            return ignore("mentioned_other_agent_only")
        if input.thread_context.thread_claimed_by_other:
            return react_only("thread_claimed_by_other", "eyes")

        if is_thread_followup_for_me(input):
            if not input.state.response_expected:
                return ignore("thread_followup_no_response_expected")
            return reply("thread_followup")

        return ignore("no_trigger")


def should_react(input: PolicyInput) -> PolicyDecision:
    return ShouldReactPolicy().evaluate(input)


def mentions_me(input: PolicyInput) -> bool:
    return input.self_user_id in input.event.mentions


def mentions_non_self_users(input: PolicyInput) -> bool:
    return any(user_id != input.self_user_id for user_id in input.event.mentions)


def is_thread_followup_for_me(input: PolicyInput) -> bool:
    event = input.event
    if not event.is_thread_reply:
        return False
    if input.self_person_id not in input.thread_context.participants:
        return False
    if input.thread_context.last_bot_replier_id != input.self_person_id:
        return False
    return True


def reply(reason: str) -> PolicyDecision:
    return PolicyDecision(decision="reply", reason=reason, response_expected=True)


def react_only(reason: str, reaction: str) -> PolicyDecision:
    return PolicyDecision(
        decision="react_only",
        reason=reason,
        reaction=reaction,
        response_expected=False,
    )


def ignore(reason: str) -> PolicyDecision:
    return PolicyDecision(decision="ignore", reason=reason)
