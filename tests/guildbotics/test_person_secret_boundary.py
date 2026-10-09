"""Every path to a member's secret keys goes through ``person_secret_env_keys``.

A member's keys are derived from its ID, so any code can spell them. Code that
spells them for itself also decides for itself what to do while two stored
members derive the same keys -- and the decision #752 made (do not read,
delete or move them; refuse the team) holds only where the entry is used.

So the population is the code that reads, writes, deletes or moves secrets,
found three ways in every module of the package:

- every function that acquires the secret store,
- every string that names a member secret beyond the bare suffix
  (``"_SLACK_BOT_TOKEN"``, ``f"{x}_SLACK_BOT_TOKEN"``), or matches keys by one,
- every reconstruction of the prefix recipe (``.replace("-", "_").upper()``)
  or import of the entry's private parts.

Each finding either goes through the entry or is classified below with the
reason it is not a member's key. A bare suffix (``"SLACK_BOT_TOKEN"``) cannot
name a member's key on its own -- it selects one from the entry's answer, or
names the workspace-wide token -- unless it is used to match keys. Prose (a
string with whitespace, such as a log message) names no key.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import guildbotics
from guildbotics.utils.person_id import person_secret_env_keys

PACKAGE = Path(guildbotics.__file__).parent
ENTRY_MODULE = "utils/person_id.py"
SUFFIXES = tuple(person_secret_env_keys(PACKAGE / "missing", "x"))
NAMES_SUFFIX = re.compile(rf"(?<![A-Z0-9])({'|'.join(SUFFIXES)})(?![A-Z0-9_])")

STORE_ACQUISITIONS = {
    "resolve_secret_store",
    "KeyringSecretStore",
    "workspace_secret_store",
}
#: Calls that reach a member's keys only through the entry.
ENTRY_CALLS = {
    "person_secret_env_keys",
    "ambiguous_person_env_keys",
    "to_person_env_key",
    "_owned_secrets",
    "_target_env_keys",
}
MATCHERS = {"endswith", "startswith", "match", "fullmatch", "search"}

#: Functions that acquire the secret store without going through the entry,
#: and why the keys they touch are not a member's.
STORE_ELSEWHERE = {
    ("app_api/verify.py", "VerifyService._check_llm_provider"): (
        "the LLM provider API key named by the model config"
    ),
    ("app_api/workspace_secrets.py", "WorkspaceSecretService._store"): (
        "Hub transfers of keys the user picks from the secrets index"
    ),
    ("cli/secrets.py", "_SecretsContext.store"): (
        "`guildbotics secrets`: the user names each key explicitly"
    ),
    (
        "setup/setup_service.py",
        "SimpleProjectSetupService.read_project_config",
    ): ("LLM provider API keys named by the project setup"),
    ("setup/setup_service.py", "SimpleProjectSetupService.update_project"): (
        "LLM provider API keys named by the project setup"
    ),
    ("setup/setup_service.py", "SimpleProjectSetupService.write_project"): (
        "LLM provider API keys named by the project setup"
    ),
    ("intelligences/brains/jev.py", "credential"): "the workspace's Jev key",
    ("intelligences/decisions/preparation.py", "save_credential"): (
        "the workspace's Jev key"
    ),
    ("observability/diagnostics_events.py", "load_required_io_redaction_values"): (
        "reads every stored value only to mask it in records"
    ),
    ("utils/env_loader.py", "workspace_secret_store"): (
        "returns the store; each caller is an acquisition of its own"
    ),
    ("utils/secret_store.py", "resolve_secret_store"): (
        "returns the store; each caller is an acquisition of its own"
    ),
    ("utils/shared_redaction.py", "workspace_secret_values"): (
        "reads the key index only to mask values in shared records"
    ),
}

#: Strings that name or match member secret keys outside the entry, and why.
SUFFIX_ELSEWHERE = {
    ("app_api/api.py", "create_app.config_member_avatar_slack", "_SLACK_BOT_TOKEN"): (
        "falls back to any member's bot token to read a public Slack profile; "
        "ambiguous keys are excluded from the candidates first"
    ),
    ("utils/secret_store.py", "<module>", "_GITHUB_PRIVATE_KEY"): (
        "keeps every key of this kind out of the environment; reads none"
    ),
}


def _qualnames(tree: ast.AST) -> dict[ast.AST, str]:
    names: dict[ast.AST, str] = {}

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            names[child] = inner or "<module>"
            visit(child, inner)

    visit(tree, "")
    return names


def _called(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
    return None


def _is_prefix_recipe(node: ast.AST) -> bool:
    """``.replace("-", "_")`` and ``.upper()`` chained, in either order."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    inner = node.func.value
    pair = {_called(node), _called(inner)}
    replace = node if _called(node) == "replace" else inner
    return (
        pair == {"replace", "upper"}
        and isinstance(replace, ast.Call)
        and [a.value for a in replace.args if isinstance(a, ast.Constant)] == ["-", "_"]
    )


def findings(source: str, module: str) -> dict[str, set]:
    """Classify one module's secret access the way the tests below require."""
    tree = ast.parse(source)
    scope = _qualnames(tree)
    parent = {
        child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)
    }
    docstrings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    acquiring, entering = set(), set()
    suffix_strings, recipes = set(), set()
    for node in ast.walk(tree):
        name = _called(node)
        if name in STORE_ACQUISITIONS:
            acquiring.add(scope[node])
        if name in ENTRY_CALLS:
            entering.add(scope[node])
        if _is_prefix_recipe(node):
            recipes.add((module, scope[node]))
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "guildbotics.utils.person_id"
        ):
            recipes |= {
                (module, alias.name)
                for alias in node.names
                if alias.name.startswith("_")
            }
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node not in docstrings
            and not any(character.isspace() for character in node.value)
            and NAMES_SUFFIX.search(node.value.upper())
        ):
            owner = parent.get(node)
            if isinstance(owner, ast.JoinedStr):
                owner = parent.get(owner)
            matched = _called(owner) in MATCHERS or (
                isinstance(owner, ast.Compare)
                and owner.left is node
                and isinstance(owner.ops[0], (ast.In, ast.NotIn))
            )
            if node.value.upper() not in SUFFIXES or matched:
                suffix_strings.add((module, scope[node], node.value))
    return {
        "store": {(module, name) for name in acquiring - entering},
        "suffix": suffix_strings,
        "recipe": recipes,
    }


def _package_findings() -> dict[str, set]:
    found: dict[str, set] = {"store": set(), "suffix": set(), "recipe": set()}
    for path in sorted(PACKAGE.rglob("*.py")):
        module = path.relative_to(PACKAGE).as_posix()
        if module == ENTRY_MODULE:
            continue
        for kind, items in findings(path.read_text(encoding="utf-8"), module).items():
            found[kind] |= items
    return found


PACKAGE_FINDINGS = _package_findings()


def test_store_access_reaches_member_keys_only_through_the_entry():
    assert PACKAGE_FINDINGS["store"] == set(STORE_ELSEWHERE)


def test_member_key_strings_are_spelled_only_by_the_entry():
    assert PACKAGE_FINDINGS["suffix"] == set(SUFFIX_ELSEWHERE)


def test_prefix_recipe_and_private_parts_stay_in_the_entry():
    assert PACKAGE_FINDINGS["recipe"] == set()


def test_paths_that_bypass_the_entry_are_found():
    bypass = """
def read(config_dir, person_id):
    return resolve_secret_store(config_dir).get(f"{person_id}_SLACK_BOT_TOKEN")

def match(env):
    return [key for key in env if key.endswith("SLACK_BOT_TOKEN")]

def contains(key):
    return "GITHUB_ACCESS_TOKEN" in key

def recipe(person_id, suffix):
    return f"{person_id.replace('-', '_').upper()}_{suffix}"

from guildbotics.utils.person_id import _keys
"""
    assert findings(bypass, "new.py") == {
        "store": {("new.py", "read")},
        "suffix": {
            ("new.py", "read", "_SLACK_BOT_TOKEN"),
            ("new.py", "match", "SLACK_BOT_TOKEN"),
            ("new.py", "contains", "GITHUB_ACCESS_TOKEN"),
        },
        "recipe": {("new.py", "recipe"), ("new.py", "_keys")},
    }


def test_selecting_from_the_entry_is_not_a_bypass():
    through = """
def read(config_dir, person, keys):
    owned = _owned_secrets(config_dir, person.person_id)
    token = owned.get("SLACK_BOT_TOKEN")
    return token, person.get_secret("slack_app_token"), keys["GITHUB_PRIVATE_KEY"]

def classify(key):
    return key == "github_private_key", log("the SLACK_APP_TOKEN is invalid")
"""
    assert findings(through, "new.py") == {
        "store": set(),
        "suffix": set(),
        "recipe": set(),
    }
