"""Compatibility shim for renamed Slack socket listener module.

Use `guildbotics.integrations.slack.slack_socket_listener` instead.
"""

from guildbotics.integrations.slack.slack_socket_listener import SlackSocketEventListener

__all__ = ["SlackSocketEventListener"]

