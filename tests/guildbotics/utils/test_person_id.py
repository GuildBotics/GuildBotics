import pytest

from guildbotics.editions.simple.setup_service import (
    PersonSetupInput,
    PersonUpdateInput,
    _person_config_dir,
    person_config_paths,
)
from guildbotics.entities.team import Person
from guildbotics.utils.avatar import get_member_avatar_dir
from guildbotics.utils.fileio import get_member_clone_path, get_person_config_path


@pytest.mark.parametrize(
    "value",
    [
        "/private/keys",
        "../../.ssh",
        "a/b",
        "a\\b",
        "C:\\keys",
        "a\n",
        "",
        ".",
        "..",
        "con",
        "nul",
        "aux",
        "prn",
        "com1",
        "lpt1",
    ],
)
@pytest.mark.parametrize(
    "entry",
    [
        "person",
        "new",
        "original",
        "clone",
        "avatar",
        "config",
        "config_paths",
        "setup_dir",
    ],
)
def test_member_identifiers_cannot_escape_any_person_path(tmp_path, value, entry):
    data = dict(
        config_dir=tmp_path,
        person_type="agent",
        person_id=value,
        person_name="Aiko",
        is_active=True,
        github_username="aiko",
        git_email="aiko@example.com",
    )
    with pytest.raises(ValueError):
        if entry == "person":
            Person(person_id=value, name="Aiko")
        elif entry == "new":
            PersonSetupInput(**data)
        elif entry == "original":
            PersonUpdateInput(**{**data, "person_id": "aiko"}, original_person_id=value)
        elif entry == "clone":
            get_member_clone_path(value, tmp_path)
        elif entry == "avatar":
            get_member_avatar_dir(tmp_path, value)
        elif entry == "config":
            get_person_config_path(value, "person.yml")
        elif entry == "config_paths":
            person_config_paths(value)
        else:
            _person_config_dir(tmp_path, value)


def test_valid_member_identifier_is_preserved(tmp_path):
    assert Person(person_id="aiko_1-2", name="Aiko").person_id == "aiko_1-2"
    assert (
        get_member_clone_path("aiko_1-2", tmp_path)
        == tmp_path / ".guildbotics/local/clones/aiko_1-2"
    )
