import pytest

from guildbotics.utils.text_utils import get_json_str


def test_get_json_str_with_fenced_json_block():
    """Extracts JSON from a fenced ```json code block and trims it."""
    raw = (
        "Here is output before.\n"
        "```json\n"
        "{ \n  \"a\": 1, \n  \"b\": [1, 2] \n}\n"
        "```\n"
        "And some text after.\n"
    )
    out = get_json_str(raw)
    assert out == "{ \n  \"a\": 1, \n  \"b\": [1, 2] \n}"


def test_get_json_str_without_fence_with_noise():
    """Falls back to first '{' and last '}' and trims surroundings."""
    raw = "noise start... { \"x\": 42 } more noise here"
    out = get_json_str(raw)
    assert out == '{ "x": 42 }'


def test_get_json_str_empty_string():
    """Empty input returns empty string after strip."""
    assert get_json_str("") == ""


def test_get_json_str_no_braces_returns_trimmed_original():
    """No braces and no fenced block → returns stripped original."""
    raw = "   no json here, only text   "
    out = get_json_str(raw)
    assert out == "no json here, only text"


def test_get_json_str_boundary_first_to_last_brace():
    """Confirms extraction spans from first '{' to last '}' when multiple blocks exist."""
    raw = "pre { \"a\": 1 } mid {\"b\": 2} post"
    out = get_json_str(raw)
    # By design, fallback selects from the first '{' to the last '}'
    assert out == '{ "a": 1 } mid {"b": 2}'


def test_get_json_str_fenced_non_json_language_falls_back():
    """Non-json fenced block should not match the json fence regex and should fall back."""
    raw = (
        "before text\n"
        "```txt\n"
        "{\n  \"k\": \"v\"\n}\n"
        "```\n"
        "after text\n"
    )
    out = get_json_str(raw)
    assert out == '{\n  "k": "v"\n}'

