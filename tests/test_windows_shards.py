from tests.windows_shards import verify_windows_shards, windows_shard_for_nodeid


def test_real_git_contracts_have_their_own_windows_shard() -> None:
    assert (
        windows_shard_for_nodeid(
            "tests/guildbotics/sync/test_manager.py::test_saved_state_reaches_the_hub"
        )
        == "git-contracts"
    )


def test_unclassified_tests_enter_the_remainder_shard() -> None:
    assert (
        windows_shard_for_nodeid("tests/new_area/test_new_contract.py::test_new")
        == "remainder"
    )


def test_windows_shards_cover_each_node_once() -> None:
    nodeids = [
        "tests/guildbotics/sync/test_manager.py::test_sync",
        "tests/guildbotics/utils/test_fileio.py::test_write",
        "tests/new_area/test_new_contract.py::test_new",
    ]

    assert verify_windows_shards(nodeids) == {
        "git-contracts": 1,
        "remainder": 2,
    }
