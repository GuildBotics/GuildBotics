"""Every text subprocess in the Python guest declares its encoding."""

import ast
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _text_calls(source: str):
    tree = ast.parse(source)
    parents = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    imports = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update((item.asname or item.name, item.name) for item in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            imports.update(
                (item.asname or item.name, f"subprocess.{item.name}")
                for item in node.names
            )
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = ast.unparse(node.func).split(".")
        name[0] = imports.get(name[0], name[0])
        if ".".join(name) not in {"subprocess.run", "subprocess.Popen"}:
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords}
        if not any(
            name in keywords and ast.literal_eval(keywords[name])
            for name in ("text", "universal_newlines", "encoding", "errors")
        ):
            continue
        owners = []
        parent = parents.get(node)
        while parent is not None:
            if isinstance(
                parent, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
            ):
                owners.append(parent.name)
            parent = parents.get(parent)
        yield ".".join(reversed(owners)), node.lineno, keywords


def test_every_text_subprocess_has_an_encoding_contract() -> None:
    # Include new source files, without reading ignored generated files.
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"],
        cwd=ROOT / "guildbotics",
        capture_output=True,
        check=True,
    )
    unclassified = []
    for relative in listed.stdout.decode("utf-8").splitlines():
        path = ROOT / "guildbotics" / relative
        for owner, line, keywords in _text_calls(path.read_text(encoding="utf-8")):
            key = f"guildbotics/{relative}:{owner}"
            encoding = keywords.get("encoding")
            if isinstance(encoding, ast.Constant) and isinstance(encoding.value, str):
                assert encoding.value, f"Empty subprocess encoding: {key}:{line}"
            else:
                unclassified.append(f"{key}:{line}")

    assert not unclassified, f"Declare an encoding: {unclassified}"


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess; subprocess.run([], text=True)",
        "import subprocess as sp; sp.Popen([], universal_newlines=True)",
        "from subprocess import run as launch; launch([], encoding='utf-8')",
        "from subprocess import Popen; Popen([], errors='replace')",
    ],
)
def test_the_inventory_recognizes_text_mode_and_import_aliases(source: str) -> None:
    assert len(list(_text_calls(source))) == 1
