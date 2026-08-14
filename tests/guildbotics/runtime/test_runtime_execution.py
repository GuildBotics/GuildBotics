from guildbotics.runtime.execution import (
    ExecutionPlacement,
    resolve_execution_placement,
)


def test_omitted_target_resolves_to_local() -> None:
    assert resolve_execution_placement(None) == ExecutionPlacement.local()
    assert resolve_execution_placement("") == ExecutionPlacement.local()
    assert resolve_execution_placement("   ") == ExecutionPlacement.local()


def test_target_device_resolves_to_remote() -> None:
    placement = resolve_execution_placement("device-1")
    assert placement == ExecutionPlacement.remote("device-1")
    assert placement.kind == "remote"
    assert placement.device_id == "device-1"
