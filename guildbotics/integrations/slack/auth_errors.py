"""Slack authentication error codes shared by Web API and Socket Mode."""

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
