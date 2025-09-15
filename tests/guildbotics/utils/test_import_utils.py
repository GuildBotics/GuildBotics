"""Unit tests for guildbotics.utils.import_utils.

Covers normal paths and error scenarios for:
- load_class
- load_function
- instantiate_class (including expected_type mismatch)
"""

from __future__ import annotations

import os
import sys
import types
from typing import Iterator

import pytest

# Ensure project root is on sys.path for package imports when sandboxed
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from guildbotics.utils.import_utils import (
    instantiate_class,
    load_class,
    load_function,
)


@pytest.fixture()
def dummy_module() -> Iterator[str]:
    """Create and register a temporary dummy module for import tests.

    The fixture injects a package-like module `gb_test_mod` and a submodule
    `gb_test_mod.sub` into ``sys.modules`` with a dummy class and function.

    Yields:
        str: The fully-qualified module path of the submodule.
    """
    pkg_name = "gb_test_mod"
    sub_name = f"{pkg_name}.sub"

    pkg = types.ModuleType(pkg_name)
    # Mark as a package to be safe for dotted imports
    pkg.__path__ = []  # type: ignore[attr-defined]

    sub = types.ModuleType(sub_name)

    class DummyClass:
        """Simple dummy class for testing dynamic imports."""

        def __init__(self, value: int = 42) -> None:
            self.value = value

    def dummy_function(x: int, y: int) -> int:
        """Return a deterministic value for verification."""

        return x + y

    # Attach symbols to submodule
    setattr(sub, "DummyClass", DummyClass)
    setattr(sub, "dummy_function", dummy_function)

    # Wire module hierarchy and register
    setattr(pkg, "sub", sub)
    sys.modules[pkg_name] = pkg
    sys.modules[sub_name] = sub

    try:
        yield sub_name
    finally:
        # Cleanup to avoid leaking into other tests
        sys.modules.pop(sub_name, None)
        sys.modules.pop(pkg_name, None)


def test_load_class_success(dummy_module: str) -> None:
    """load_class returns the class object for a valid path."""

    cls = load_class(f"{dummy_module}.DummyClass")
    assert cls.__name__ == "DummyClass"


def test_load_class_missing_module() -> None:
    """load_class raises ImportError when module does not exist."""

    with pytest.raises(ImportError) as exc:
        load_class("no.such.module.DummyClass")
    assert "Module 'no.such.module' could not be imported" in str(exc.value)


def test_load_class_missing_symbol(dummy_module: str) -> None:
    """load_class raises ImportError when class is absent in module."""

    with pytest.raises(ImportError) as exc:
        load_class(f"{dummy_module}.MissingClass")
    assert "Class 'MissingClass' not found" in str(exc.value)


def test_load_function_success(dummy_module: str) -> None:
    """load_function returns the function object for a valid path."""

    func = load_function(f"{dummy_module}.dummy_function")
    assert callable(func)
    assert func(2, 3) == 5


def test_load_function_missing_module() -> None:
    """load_function raises ImportError when module does not exist."""

    with pytest.raises(ImportError) as exc:
        load_function("no.such.module.func")
    assert "Module 'no.such.module' could not be imported" in str(exc.value)


def test_load_function_missing_symbol(dummy_module: str) -> None:
    """load_function raises ImportError when function is absent in module."""

    with pytest.raises(ImportError) as exc:
        load_function(f"{dummy_module}.missing_function")
    assert "Function 'missing_function' not found" in str(exc.value)


def test_instantiate_class_success(dummy_module: str) -> None:
    """instantiate_class constructs the instance with provided kwargs."""

    obj = instantiate_class(f"{dummy_module}.DummyClass", value=7)
    assert type(obj).__name__ == "DummyClass"
    assert getattr(obj, "value") == 7


def test_instantiate_class_missing_module() -> None:
    """instantiate_class propagates ImportError for nonexistent module."""

    with pytest.raises(ImportError) as exc:
        instantiate_class("no.such.module.DummyClass")
    assert "Module 'no.such.module' could not be imported" in str(exc.value)


def test_instantiate_class_missing_symbol(dummy_module: str) -> None:
    """instantiate_class raises ImportError when class is missing."""

    with pytest.raises(ImportError) as exc:
        instantiate_class(f"{dummy_module}.MissingClass")
    assert "Class 'MissingClass' not found" in str(exc.value)


def test_instantiate_class_expected_type_mismatch(dummy_module: str) -> None:
    """instantiate_class raises TypeError when expected_type does not match."""

    with pytest.raises(TypeError) as exc:
        instantiate_class(f"{dummy_module}.DummyClass", expected_type=dict)
    assert "Expected instance of type dict" in str(exc.value)
