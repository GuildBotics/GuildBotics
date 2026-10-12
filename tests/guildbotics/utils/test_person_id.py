import pytest

from guildbotics.app_api.intelligences import (
    IntelligenceConfigService,
    intelligence_config_dir,
)
from guildbotics.app_api.models import IntelligenceConfigUpdateRequest
from guildbotics.app_api.runtime import _command_roots
from guildbotics.commands.discovery import iter_candidate_paths
from guildbotics.entities.team import Person
from guildbotics.setup.setup_service import (
    PersonSetupInput,
    PersonUpdateInput,
    _person_config_dir,
    person_config_paths,
)
from guildbotics.utils.avatar import get_member_avatar_dir
from guildbotics.utils.fileio import (
    get_intelligence_roots,
    get_member_clone_path,
    get_person_config_path,
    load_person_slot_mapping,
)
from guildbotics.utils.person_id import (
    is_valid_person_id,
    validate_member_directory_name,
    validate_person_id,
)


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
def test_person_id_and_stored_directory_input_classes(tmp_path, value):
    assert not is_valid_person_id(value)
    with pytest.raises(ValueError):
        validate_person_id(value)
    if value in _RESERVED_IDS:
        assert validate_member_directory_name(value) == value
        config = PersonUpdateInput(**data_for(tmp_path), original_person_id=value)
        assert config.original_person_id == value
    else:
        with pytest.raises(ValueError):
            validate_member_directory_name(value)
        with pytest.raises(ValueError):
            PersonUpdateInput(**data_for(tmp_path), original_person_id=value)


_PERSON_PATH_ENTRIES = {
    "person": lambda root, value: Person(person_id=value, name="Aiko"),
    "new": lambda root, value: PersonSetupInput(**data_for(root, value)),
    "clone": lambda root, value: get_member_clone_path(value, root),
    "avatar": lambda root, value: get_member_avatar_dir(root, value),
    "config": lambda root, value: get_person_config_path(value, "person.yml"),
    "config_paths": lambda root, value: person_config_paths(value),
    "setup_dir": lambda root, value: _person_config_dir(root, value),
    "intelligence_revision": lambda root, value: intelligence_config_dir(value),
    "intelligence_scope": lambda root, value: IntelligenceConfigService()._scope_dir(
        root, value
    ),
    "intelligence_read": lambda root, value: IntelligenceConfigService().read_config(
        config_dir=root, person_id=value
    ),
    # IO must validate even when its input model has been bypassed.
    "intelligence_write": lambda root, value: IntelligenceConfigService().update_config(
        IntelligenceConfigUpdateRequest.model_construct(
            config_dir=root, person_id=value, inherit_team_defaults=True
        )
    ),
    "intelligence_mapping": lambda root, value: (
        IntelligenceConfigService()._read_merged_mapping(
            root, value, "model_mapping.yml"
        )
    ),
    "intelligence_yaml": lambda root, value: (
        IntelligenceConfigService()._read_scoped_yaml(root, value, "models/default.yml")
    ),
    "slot_mapping": lambda root, value: load_person_slot_mapping(
        value, "intelligences/model_mapping.yml"
    ),
    "intelligence_roots": lambda root, value: get_intelligence_roots(
        root, value, "models"
    ),
    "command_discovery": lambda root, value: list(
        iter_candidate_paths("example", "en", value)
    ),
    "runtime_commands": lambda root, value: _command_roots(value),
}


@pytest.mark.parametrize(
    "entry", _PERSON_PATH_ENTRIES.values(), ids=_PERSON_PATH_ENTRIES
)
@pytest.mark.parametrize("value", ["../../.ssh", "con"])
def test_member_identifiers_cannot_escape_any_person_path(tmp_path, value, entry):
    with pytest.raises(ValueError):
        entry(tmp_path, value)


def data_for(config_dir, person_id="aiko"):
    return dict(
        config_dir=config_dir,
        person_type="agent",
        person_id=person_id,
        person_name="Aiko",
        is_active=True,
    )


def test_valid_member_identifier_is_preserved(tmp_path):
    assert Person(person_id="aiko_1-2", name="Aiko").person_id == "aiko_1-2"
    assert (
        get_member_clone_path("aiko_1-2", tmp_path)
        == tmp_path / ".guildbotics/local/clones/aiko_1-2"
    )


@pytest.mark.parametrize("language", ["en", "ja"])
@pytest.mark.parametrize("shape", ["id", "directory", "fields", "yaml", "mismatch"])
def test_member_config_messages_use_localized_keys(tmp_path, language, shape):
    import i18n

    from guildbotics.loader.yaml.yaml_team_loader import YamlTeamLoader
    from guildbotics.utils.i18n_tool import t
    from guildbotics.utils.person_id import MemberConfigError

    previous = i18n.get("locale")
    i18n.set("locale", language)
    try:
        if shape in {"id", "directory"}:
            validate = (
                validate_person_id if shape == "id" else validate_member_directory_name
            )
            key = "id_requirement" if shape == "id" else "directory_name"
            with pytest.raises(ValueError) as error:
                validate("../keys")
            assert str(error.value) == t(f"member_config.{key}")
            return
        team = tmp_path / "team"
        member = team / "members/alice"
        member.mkdir(parents=True)
        (team / "project.yml").write_text(
            "name: Test\nlanguage: en\n", encoding="utf-8"
        )
        path = member / "person.yml"
        path.write_text(
            {
                "fields": "person_id: Alice\nname: Test\n",
                "yaml": "person_id: [",
                "mismatch": "person_id: bob\nname: Test\n",
            }[shape],
            encoding="utf-8",
        )
        with pytest.raises(MemberConfigError) as error:
            YamlTeamLoader(str(team)).load()
        reason = (
            t("member_config.invalid_fields", fields="person_id")
            if shape == "fields"
            else t(
                f"member_config.{'invalid_yaml' if shape == 'yaml' else 'directory_mismatch'}"
            )
        )
        assert error.value.filename == str(path)
        assert str(error.value) == t("member_config.invalid", path=path, reason=reason)
    finally:
        i18n.set("locale", previous)
