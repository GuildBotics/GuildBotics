import importlib
from typing import Any, Optional, Type, cast


def load_class(module_and_cls: str) -> Type[Any]:
    """
    Load a class from a module given its full path.

    Args:
        module_and_cls (str): The full path to the class,
            e.g., "module.submodule.ClassName".

    Returns:
        Type[Any]: The class object.

    Raises:
        ImportError: If the module or class cannot be found.
    """
    module_path, cls_name = module_and_cls.rsplit(".", 1)
    try:
        module_obj = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        raise ImportError(f"Module '{module_path}' could not be imported") from e
    if not hasattr(module_obj, cls_name):
        raise ImportError(f"Class '{cls_name}' not found in module '{module_path}'")
    return cast(Type[Any], getattr(module_obj, cls_name))


def load_function(module_and_func: str) -> Any:
    """
    Load a function from a module given its full path.

    Args:
        module_and_func (str): The full path to the function,
            e.g., "module.submodule.function_name".

    Returns:
        Any: The function object.

    Raises:
        ImportError: If the module or function cannot be found.
    """
    module_path, func_name = module_and_func.rsplit(".", 1)
    try:
        module_obj = importlib.import_module(module_path)
    except ModuleNotFoundError as e:
        raise ImportError(f"Module '{module_path}' could not be imported") from e
    if not hasattr(module_obj, func_name):
        raise ImportError(f"Function '{func_name}' not found in module '{module_path}'")
    return cast(Any, getattr(module_obj, func_name))


def instantiate_class(
    module_and_cls: str, expected_type: Optional[Type[Any]] = None, **kwargs
) -> Any:
    """
    Instantiate a class from a module given its full path and parameters,
    and optionally verify its type.

    Args:
        module_and_cls (str): The full path to the class,
            e.g., "module.submodule.ClassName".
        expected_type (Optional[Type[Any]]): If provided, the created instance
            must be an instance of this type.
        **kwargs: Keyword arguments to pass to the class constructor.

    Returns:
        Any: An instance of the class.

    Raises:
        ImportError: If the module or class cannot be found.
        TypeError: If instantiation fails due to incorrect arguments or if the
            resulting instance is not of expected_type.
    """
    cls_obj = load_class(module_and_cls)
    try:
        instance = cls_obj(**kwargs)
    except TypeError as e:
        raise TypeError(
            f"Failed to instantiate class '{module_and_cls}' with arguments {kwargs}"
        ) from e

    if expected_type is not None and not isinstance(instance, expected_type):
        raise TypeError(
            f"Expected instance of type {expected_type.__name__}, got {type(instance).__name__}"
        )
    return instance
