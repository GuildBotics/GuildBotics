import pytest
from guildbotics.utils.person_id import is_valid_person_id, validate_person_id

_UNSAFE_DIRECTORY_NAMES = [
    "/private/keys",
    "../../.ssh",
    "a/b",
    "a\\b",
    "C:\\keys",
    "a\n",
    "",
    ".",
    "..",
]
_RESERVED_IDS = ["con", "nul", "aux", "prn", "com1", "lpt1"]


@pytest.mark.parametrize("value", _UNSAFE_DIRECTORY_NAMES + _RESERVED_IDS)
def test_person_id_and_stored_directory_input_classes(value):
    assert not is_valid_person_id(value)
    with pytest.raises(ValueError):
        validate_person_id(value)
