"""Slack Web API error codes, as Web API calls and Socket Mode read them."""

SLACK_AUTH_ERROR_CODES = frozenset(
    {
        "invalid_auth",
        "not_authed",
        "account_inactive",
        "token_revoked",
        "token_expired",
        "no_permission",
        "not_allowed_token_type",
    }
)


def is_slack_auth_error(code: str) -> bool:
    return code in SLACK_AUTH_ERROR_CODES


def slack_api_error(payload: object) -> str:
    """The error code a Web API response names, or ``""`` when the call succeeded."""
    if not isinstance(payload, dict):
        return "invalid_json"
    if payload.get("ok", False):
        return ""
    return str(payload.get("error", "unknown_error"))
