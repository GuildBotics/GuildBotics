import pytest

from guildbotics.setup.setup_service import (
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
        "intelligence_revision",
        "intelligence_scope",
        "intelligence_read",
        "intelligence_write",
        "intelligence_mapping",
        "intelligence_yaml",
        "slot_mapping",
        "intelligence_roots",
        "command_discovery",
        "runtime_commands",
    ],
)
def test_member_identifiers_cannot_escape_any_person_path(tmp_path, value, entry):
    from guildbotics.app_api.intelligences import (
        IntelligenceConfigService,
        intelligence_config_dir,
    )
    from guildbotics.app_api.runtime import _command_roots
    from guildbotics.commands.discovery import iter_candidate_paths
    from guildbotics.utils.fileio import (
        get_intelligence_roots,
        load_person_slot_mapping,
    )

    # Old directory names are addressable only to repair an existing config.
    if entry == "original" and value in {"con", "nul", "aux", "prn", "com1", "lpt1"}:
        assert (
            PersonUpdateInput(
                **{**data_for(tmp_path), "person_id": "aiko"}, original_person_id=value
            ).original_person_id
            == value
        )
        return
    data = {**data_for(tmp_path), "person_id": value}
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
        elif entry == "setup_dir":
            _person_config_dir(tmp_path, value)
        elif entry == "intelligence_revision":
            intelligence_config_dir(value)
        elif entry == "intelligence_scope":
            IntelligenceConfigService()._scope_dir(tmp_path, value)
        elif entry == "intelligence_read":
            IntelligenceConfigService().read_config(
                config_dir=tmp_path, person_id=value
            )
        elif entry == "intelligence_write":
            from guildbotics.app_api.models import IntelligenceConfigUpdateRequest

            # IO must validate even when its input model has been bypassed.
            request = IntelligenceConfigUpdateRequest.model_construct(
                config_dir=tmp_path, person_id=value, inherit_team_defaults=True
            )
            IntelligenceConfigService().update_config(request)
        elif entry == "intelligence_mapping":
            IntelligenceConfigService()._read_merged_mapping(
                tmp_path, value, "model_mapping.yml"
            )
        elif entry == "intelligence_yaml":
            IntelligenceConfigService()._read_scoped_yaml(
                tmp_path, value, "models/default.yml"
            )
        elif entry == "slot_mapping":
            load_person_slot_mapping(value, "intelligences/model_mapping.yml")
        elif entry == "intelligence_roots":
            get_intelligence_roots(tmp_path, value, "models")
        elif entry == "command_discovery":
            list(iter_candidate_paths("example", "en", value))
        elif entry == "runtime_commands":
            _command_roots(value)


def data_for(config_dir):
    return {
        "config_dir": config_dir,
        "person_type": "agent",
        "person_id": "aiko",
        "person_name": "Aiko",
        "is_active": True,
        "github_username": "aiko",
        "git_email": "aiko@example.com",
    }


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
    from guildbotics.utils.person_id import (
        MemberConfigError,
        validate_member_directory_name,
        validate_person_id,
    )

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
