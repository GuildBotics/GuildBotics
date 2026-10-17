"""Every text subprocess declares its encoding or explains its locale contract."""

import ast
import subprocess
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Native console tools use the local code page; UTF-8 protocols do not.
LOCALE_OUTPUT = {
    "guildbotics/environment/toolchain.py:device_nameservers": "PowerShell prints ASCII addresses and console-code-page diagnostics.",
    "guildbotics/hub/connection.py:_scan_host_keys": "SSH diagnostics use the client locale; keys are read from a UTF-8 file.",
    "guildbotics/hub/connection.py:_known_host_keys": "ssh-keygen prints ASCII key material with native comments and diagnostics.",
    "guildbotics/hub/connection.py:_run": "OpenSSH tools use the client locale; Hub CLI output uses the byte stream.",
    "guildbotics/utils/os_language.py:_macos_ui_language_tag": "macOS defaults prints ASCII language tags in its native locale.",
    "guildbotics/utils/processes.py:launching_app_name": "macOS ps prints native process paths in its native locale.",
}


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
    locale_calls = Counter()
    unclassified = []
    for relative in listed.stdout.decode("utf-8").splitlines():
        path = ROOT / "guildbotics" / relative
        for owner, line, keywords in _text_calls(path.read_text(encoding="utf-8")):
            key = f"guildbotics/{relative}:{owner}"
            encoding = keywords.get("encoding")
            if isinstance(encoding, ast.Constant) and isinstance(encoding.value, str):
                assert encoding.value, f"Empty subprocess encoding: {key}:{line}"
            elif key in LOCALE_OUTPUT:
                assert LOCALE_OUTPUT[key].strip()
                locale_calls[key] += 1
            else:
                unclassified.append(f"{key}:{line}")

    assert not unclassified, (
        f"Declare an encoding or explain the locale: {unclassified}"
    )
    assert locale_calls == Counter(dict.fromkeys(LOCALE_OUTPUT, 1)), (
        "Locale exceptions must each name exactly one current subprocess call."
    )


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
