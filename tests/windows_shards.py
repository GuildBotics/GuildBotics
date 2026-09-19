"""Duration-informed partition for the required Windows pytest jobs."""

from __future__ import annotations

from typing import Literal

WindowsShard = Literal["git-contracts", "remainder"]
WINDOWS_SHARDS: tuple[WindowsShard, ...] = ("git-contracts", "remainder")

# These files formed the long tail in the unsharded Windows duration report.
# They keep real Git semantics and run together on a separate runner. Everything
# else, including every new or unclassified test, belongs to ``remainder``.
_GIT_CONTRACT_PATHS = frozenset(
    {
        "tests/guildbotics/capabilities/test_member_git.py",
        "tests/guildbotics/sync/test_activation.py",
        "tests/guildbotics/sync/test_commit_boundary.py",
        "tests/guildbotics/sync/test_enrollment.py",
        "tests/guildbotics/sync/test_local_repository.py",
        "tests/guildbotics/sync/test_manager.py",
        "tests/guildbotics/sync/test_rejections.py",
        "tests/guildbotics/utils/test_git_tool.py",
    }
)


def windows_shard_for_nodeid(nodeid: str) -> WindowsShard:
    """Assign every pytest node ID to exactly one required Windows shard."""
    path = nodeid.partition("::")[0].replace("\\", "/")
    return "git-contracts" if path in _GIT_CONTRACT_PATHS else "remainder"


def verify_windows_shards(nodeids: list[str]) -> dict[WindowsShard, int]:
    """Prove the shard union equals the collection and has no duplicates."""
    assignments = {
        shard: {
            nodeid for nodeid in nodeids if windows_shard_for_nodeid(nodeid) == shard
        }
        for shard in WINDOWS_SHARDS
    }
    union = set().union(*assignments.values())
    assigned_count = sum(len(nodes) for nodes in assignments.values())
    if union != set(nodeids) or assigned_count != len(nodeids):
        raise ValueError("Windows pytest shards must cover every node ID exactly once.")
    return {shard: len(assignments[shard]) for shard in WINDOWS_SHARDS}
