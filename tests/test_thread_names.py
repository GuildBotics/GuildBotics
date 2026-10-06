"""Keep every product-owned thread visible to the teardown isolation guard."""

import ast
from pathlib import Path

import pytest

import guildbotics


def _thread_names(source: str) -> list[tuple[int, bool]]:
    tree = ast.parse(source)
    modules = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "threading"
    }
    constructors = {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "threading"
        for alias in node.names
        if alias.name == "Thread"
    }

    def is_thread(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in constructors) or (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
            and node.attr == "Thread"
        )

    initializers: set[int] = set()
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or not any(
            is_thread(base) for base in node.bases
        ):
            continue
        calls = [
            call
            for method in node.body
            if isinstance(method, ast.FunctionDef) and method.name == "__init__"
            for call in ast.walk(method)
            if isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "__init__"
            and (
                is_thread(call.func.value)
                or (
                    isinstance(call.func.value, ast.Call)
                    and isinstance(call.func.value.func, ast.Name)
                    and call.func.value.func.id == "super"
                )
            )
        ]
        initializers.update(id(call) for call in calls)
        if not calls:
            # An inherited or opaque initializer cannot guarantee a prefix.
            names.append((node.lineno, False))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not is_thread(node.func) and id(node) not in initializers:
            continue
        name = next((kw.value for kw in node.keywords if kw.arg == "name"), None)
        # Require a literal prefix even when the suffix identifies a member.
        if isinstance(name, ast.JoinedStr):
            name = name.values[0] if name.values else None
        prefixed = (
            isinstance(name, ast.Constant)
            and isinstance(name.value, str)
            and name.value.startswith("guildbotics-")
        )
        names.append((node.lineno, prefixed))
    return names


def test_all_product_threads_have_the_isolation_prefix() -> None:
    package = Path(guildbotics.__file__).parent
    offenders = []
    for path in sorted(package.rglob("*.py")):
        offenders.extend(
            f"{path.relative_to(package)}:{line}"
            for line, prefixed in _thread_names(path.read_text(encoding="utf-8"))
            if not prefixed
        )
    assert offenders == [], f"Threads invisible to the isolation guard: {offenders}"


@pytest.mark.parametrize(
    "constructor",
    [
        "import threading\nthreading.Thread({name})",
        "import threading as th\nth.Thread({name})",
        "from threading import Thread\nThread({name})",
        "from threading import Thread as Worker\nWorker({name})",
        "import threading\nclass Worker(threading.Thread):\n"
        "    def __init__(self):\n        super().__init__({name})",
        "from threading import Thread as Base\nclass Worker(Base):\n"
        "    def __init__(self):\n        Base.__init__(self, {name})",
    ],
)
@pytest.mark.parametrize(
    ("name", "prefixed"),
    [
        ("", False),
        ('name="worker"', False),
        ("name=member_id", False),
        ('name="guildbotics-worker"', True),
        ('name=f"guildbotics-scheduler-{member_id}"', True),
        ('name=f"{member_id}-guildbotics-worker"', False),
    ],
)
def test_thread_population_includes_aliases_and_subclasses(
    constructor: str, name: str, prefixed: bool
) -> None:
    assert [
        result for _line, result in _thread_names(constructor.format(name=name))
    ] == [prefixed]


@pytest.mark.parametrize(
    "body",
    ["pass", "def __init__(self):\n        initialize(self)"],
)
def test_thread_subclasses_must_initialize_an_explicit_name(body: str) -> None:
    source = f"import threading\nclass Worker(threading.Thread):\n    {body}"
    assert _thread_names(source) == [(2, False)]
