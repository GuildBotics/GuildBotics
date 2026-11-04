from datetime import UTC, datetime, timedelta
from guildbotics.runtime.chat_service import message_position

EPOCH = datetime(2026, 10, 1, tzinfo=UTC)


def at(seconds):
    return EPOCH + timedelta(seconds=seconds)


def position(seconds):
    return message_position(at(float(seconds)), str(seconds))
