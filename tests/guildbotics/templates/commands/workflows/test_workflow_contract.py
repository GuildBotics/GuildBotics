"""Contract tests for the shared workflow envelope text.

The workflow contract is the single source for everything the two workflow
prompts (``handle_github_ticket`` / ``handle_chat_event``) have in common:
execution-mode marker, isolated workspace, non-interactive question routing,
complete-or-fail, and the AgentResponse shape. Trigger-specific completion
command forms stay in each prompt.
"""

import i18n  # type: ignore

from guildbotics.utils.i18n_tool import set_language, t

CONTRACT_KEY = "commands.workflows.common.workflow_contract"

REQUIRED_TOKENS = (
    "guildbotics_execution_mode=workflow",
    "guildbotics member context --person aiko",
    "guildbotics member help",
    "--workspace-mode current",
    "AgentResponse",
    "asking",
    "blocked",
)


def _contract(language: str) -> str:
    previous_locale = i18n.get("locale")
    previous_fallback = i18n.get("fallback")
    set_language(language)
    try:
        return t(CONTRACT_KEY, person_id="aiko")
    finally:
        i18n.set("locale", previous_locale)
        i18n.set("fallback", previous_fallback)


def test_contract_resolves_with_required_tokens_in_both_languages():
    for language in ("en", "ja"):
        text = _contract(language)
        # A missing key makes i18n return the key itself.
        assert text != CONTRACT_KEY, language
        for token in REQUIRED_TOKENS:
            assert token in text, (language, token)


def test_contract_token_sets_match_across_languages():
    # Guards against one language drifting: every guardrail token present in one
    # language must be present in the other.
    english = _contract("en")
    japanese = _contract("ja")
    for token in REQUIRED_TOKENS:
        assert (token in english) == (token in japanese), token


def test_contract_excludes_completion_command_forms():
    # The exact completion command (task complete / chat complete with its
    # arguments) is a trigger-specific contract and belongs to each prompt.
    for language in ("en", "ja"):
        text = _contract(language)
        assert "task complete" not in text, language
        assert "chat complete" not in text, language


def test_contract_renders_all_placeholders():
    for language in ("en", "ja"):
        text = _contract(language)
        assert "%{" not in text, language
        assert "{person_id}" not in text, language
