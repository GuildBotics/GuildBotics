from guildbotics.entities import Person, Role
from guildbotics.loader.yaml.yaml_role_loader import YamlRoleLoader


def test_extract_roles_from_profile_reads_explicit_roles_only(monkeypatch):
    loader = YamlRoleLoader(language_code="ja")
    monkeypatch.setattr(
        loader,
        "load_all",
        lambda: {
            "architect": Role(
                id="architect",
                summary="Architecture",
                description="Design boundaries",
            )
        },
    )
    person = Person(
        person_id="alice",
        name="Alice",
        profile={
            "roles": {"architect": {}},
            "character": {"archetype": "strategic"},
        },
    )

    loader.extract_roles_from_profile(person)

    assert set(person.roles) == {"architect"}
    assert person.roles["architect"].summary == "Architecture"
