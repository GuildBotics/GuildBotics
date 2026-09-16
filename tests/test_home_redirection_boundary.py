"""Redirecting the home directory in a test must name both variables.

``Path.home()`` reads ``USERPROFILE`` on Windows and ``HOME`` everywhere else.
A test that sets only ``HOME`` therefore points the home somewhere on one
platform and silently keeps the suite's own on the other, so it asserts
something different depending on which machine runs it. The pairing is checked
over every test file rather than over the ones that happened to fail, because
the next one written is the one that would lose it.
"""

from __future__ import annotations

import re
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent

_HOME = re.compile(r'^(\s*)(\w+)\.setenv\("HOME", (.+)\)\s*$')


def test_every_home_redirection_sets_userprofile_with_the_same_value() -> None:
    unpaired: list[str] = []
    for path in sorted(TESTS_ROOT.rglob("*.py")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            match = _HOME.match(line)
            if match is None:
                continue
            _indent, name, value = match.groups()
            expected = f'{name}.setenv("USERPROFILE", {value})'
            following = lines[index + 1].strip() if index + 1 < len(lines) else ""
            if following != expected:
                relative = path.relative_to(TESTS_ROOT).as_posix()
                unpaired.append(f"{relative}:{index + 1}")

    assert unpaired == [], (
        "These lines redirect HOME without redirecting USERPROFILE to the same "
        f"value on the next line: {unpaired}"
    )
