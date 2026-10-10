"""Guard the boundaries of the ``guildbotics`` package that are not import
directions.

Which package may import which is the import-linter configuration in
``pyproject.toml``, checked by ``lint-imports``. What it does not check is
here: that each module a ``protected`` contract allows still imports what it
protects, and that a native provider's wire protocol stays out of the API
layer and the frontend.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import grimp
import pytest

import guildbotics

PACKAGE_ROOT = Path(guildbotics.__file__).parent
REPOSITORY_ROOT = PACKAGE_ROOT.parent
_PROTECTED = [
    contract
    for contract in tomllib.loads(
        (REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["tool"]["importlinter"]["contracts"]
    if contract["type"] == "protected"
]


@pytest.mark.parametrize(
    "contract", _PROTECTED, ids=[contract["id"] for contract in _PROTECTED]
)
def test_every_allowed_importer_imports_what_its_contract_protects(
    contract: dict,
) -> None:
    """A stale entry would quietly widen the exception it declares."""
    graph = grimp.build_graph("guildbotics", include_external_packages=True)
    protected = contract["protected_modules"]
    unused = [
        importer
        for importer in contract["allowed_importers"]
        if not any(
            imported == module or imported.startswith(f"{module}.")
            for imported in graph.find_modules_directly_imported_by(importer)
            for module in protected
        )
    ]
    assert unused == []


def test_native_provider_wire_protocol_does_not_leak_into_app_or_frontend() -> None:
    wire_tokens = (
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "execCommandApproval",
        "applyPatchApproval",
        '"workspaceWrite"',
        '"dangerFullAccess"',
    )
    roots = (PACKAGE_ROOT / "app_api", REPOSITORY_ROOT / "desktop/src")
    offenders: list[str] = []
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.suffix not in {".py", ".ts", ".tsx"}:
                continue
            contents = path.read_text(encoding="utf-8")
            offenders.extend(
                f"{path.relative_to(REPOSITORY_ROOT)}: {token}"
                for token in wire_tokens
                if token in contents
            )
    assert offenders == []
