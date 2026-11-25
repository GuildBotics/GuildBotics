"""Keep the native provider wire protocol out of the frontend."""

from pathlib import Path

import pytest

pytestmark = pytest.mark.repo_policy

ROOT = Path(__file__).resolve().parents[2]


def test_native_provider_wire_protocol_does_not_leak_into_frontend() -> None:
    wire_tokens = (
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "execCommandApproval",
        "applyPatchApproval",
        '"workspaceWrite"',
        '"dangerFullAccess"',
    )
    offenders: list[str] = []
    for path in sorted((ROOT / "desktop/src").rglob("*")):
        if path.suffix not in {".ts", ".tsx"}:
            continue
        contents = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.relative_to(ROOT)}: {token}"
            for token in wire_tokens
            if token in contents
        )
    assert offenders == []
