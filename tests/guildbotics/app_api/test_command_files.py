"""Unit tests for the shared command file service."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from guildbotics.app_api.command_files import (
    CommandFileService,
    decode_file_id,
    encode_file_id,
    file_revision,
)
from guildbotics.app_api.errors import AppApiError
from guildbotics.commands import discovery
from guildbotics.utils import fileio


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    config_dir = tmp_path / "config"
    template_dir = tmp_path / "templates"
    (config_dir / "commands").mkdir(parents=True)
    (template_dir / "commands").mkdir(parents=True)
    monkeypatch.setenv("GUILDBOTICS_CONFIG_DIR", str(config_dir))
    monkeypatch.setattr(fileio, "get_template_path", lambda: template_dir)
    monkeypatch.setattr(discovery, "get_template_path", lambda: template_dir)
    return SimpleNamespace(
        config=config_dir,
        template=template_dir,
        commands=config_dir / "commands",
    )


def _service(language: str = "en") -> CommandFileService:
    return CommandFileService(language)


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_file_id_roundtrip() -> None:
    assert decode_file_id(encode_file_id("workflows/review.yaml")) == (
        "workflows/review.yaml"
    )


def test_list_read_and_empty(env: SimpleNamespace) -> None:
    assert _service().list_files().files == []

    _write(env.commands / "greet.md", "---\nname: Greet\n---\nHi\n")
    listing = _service().list_files()

    assert [file.command for file in listing.files] == ["greet"]
    detail = _service().read_file(listing.files[0].id)
    assert detail.content == "---\nname: Greet\n---\nHi\n"
    assert detail.label == "Greet"


def test_list_excludes_member_and_sidecar(env: SimpleNamespace) -> None:
    _write(env.commands / "shared.md", "---\nname: Shared\n---\nBody\n")
    _write(
        env.config / "team/members/bot/commands/member.md",
        "---\nname: Member\n---\nBody\n",
    )
    _write(env.commands / "shared.metadata.yml", "name: Sidecar\n")

    commands = {file.command for file in _service().list_files().files}

    assert commands == {"shared"}


def test_create_each_format(env: SimpleNamespace) -> None:
    for command, file_format, suffix in [
        ("md_cmd", "markdown", ".md"),
        ("py_cmd", "python", ".py"),
        ("yaml_cmd", "yaml", ".yaml"),
    ]:
        detail = _service().create_file(command, file_format)  # type: ignore[arg-type]
        assert detail.command == command
        assert (env.commands / f"{command}{suffix}").is_file()
        assert detail.format == file_format


def test_create_python_template_accepts_context(env: SimpleNamespace) -> None:
    # The default Python template must accept `context` like the built-in
    # command templates, so the runtime passes the Context to it.
    _service().create_file("job", "python")
    assert "def main(context)" in (env.commands / "job.py").read_text(encoding="utf-8")


def test_create_nested_command(env: SimpleNamespace) -> None:
    detail = _service().create_file("workflows/review", "yaml")
    assert (env.commands / "workflows/review.yaml").is_file()
    assert detail.command == "workflows/review"


def test_create_shell_is_executable(env: SimpleNamespace) -> None:
    _service().create_file("deploy", "shell")
    mode = stat.S_IMODE((env.commands / "deploy.sh").stat().st_mode)
    assert mode == 0o755


@pytest.mark.parametrize(
    "name",
    ["/abs", "..", "a/../b", "back\\slash", "has.dot", "trailing/", "with.md"],
)
def test_create_rejects_invalid_names(env: SimpleNamespace, name: str) -> None:
    with pytest.raises(AppApiError) as exc:
        _service().create_file(name, "markdown")
    assert exc.value.code == "command_file_invalid_name"


def test_create_duplicate_rejected(env: SimpleNamespace) -> None:
    _write(env.commands / "dup.md", "---\nname: Dup\n---\nBody\n")
    with pytest.raises(AppApiError) as exc:
        _service().create_file("dup", "markdown")
    assert exc.value.code == "command_file_exists"


def test_create_rejects_different_extension_duplicate(
    env: SimpleNamespace,
) -> None:
    # A same-named file in any extension is the same logical command, so a
    # different-extension create is rejected as a duplicate before writing.
    _write(env.commands / "task.md", "---\nname: Task\n---\nBody\n")
    with pytest.raises(AppApiError) as exc:
        _service().create_file("task", "yaml")
    assert exc.value.code == "command_file_exists"
    assert not (env.commands / "task.yaml").exists()


def test_create_rejected_when_localized_template_shadows(
    env: SimpleNamespace,
) -> None:
    _write(env.template / "commands/review.ja.md", "---\nname: Tpl\n---\nBody\n")
    with pytest.raises(AppApiError) as exc:
        _service("ja").create_file("review", "markdown")
    assert exc.value.code == "command_file_shadowed"
    assert exc.value.context["shadow_source"] == "template"
    assert not (env.commands / "review.md").exists()


def test_create_allowed_when_overriding_unsuffixed_template(
    env: SimpleNamespace,
) -> None:
    _write(env.template / "commands/review.md", "---\nname: Tpl\n---\nBody\n")
    detail = _service("ja").create_file("review", "markdown")
    assert (env.commands / "review.md").is_file()
    assert detail.command == "review"


def test_update_revision_conflict(env: SimpleNamespace) -> None:
    _write(env.commands / "greet.md", "---\nname: Greet\n---\nHi\n")
    service = _service()
    detail = service.read_file(service.list_files().files[0].id)

    with pytest.raises(AppApiError) as exc:
        service.update_file(detail.id, "changed", "deadbeef")
    assert exc.value.code == "command_file_changed"


def test_update_success_changes_revision(env: SimpleNamespace) -> None:
    _write(env.commands / "greet.md", "---\nname: Greet\n---\nHi\n")
    service = _service()
    detail = service.read_file(service.list_files().files[0].id)

    updated = service.update_file(
        detail.id, "---\nname: Greet\n---\nHello\n", detail.revision
    )

    assert updated.revision != detail.revision
    assert (env.commands / "greet.md").read_text(encoding="utf-8") == (
        "---\nname: Greet\n---\nHello\n"
    )


def test_update_rejects_invalid_source(env: SimpleNamespace) -> None:
    _write(env.commands / "task.py", "def main(context):\n    return ''\n")
    service = _service()
    detail = service.read_file(service.list_files().files[0].id)

    with pytest.raises(AppApiError) as exc:
        service.update_file(detail.id, "def main(:\n    pass", detail.revision)
    assert exc.value.code == "command_file_invalid_source"
    # The invalid content is not written.
    assert "def main(context)" in (env.commands / "task.py").read_text(encoding="utf-8")


def test_update_rejects_oversized_content(env: SimpleNamespace) -> None:
    _write(env.commands / "greet.md", "---\nname: Greet\n---\nHi\n")
    service = _service()
    detail = service.read_file(service.list_files().files[0].id)

    with pytest.raises(AppApiError) as exc:
        service.update_file(detail.id, "x" * (1024 * 1024 + 1), detail.revision)
    assert exc.value.code == "command_file_too_large"


def test_update_preserves_mode(env: SimpleNamespace) -> None:
    path = _write(env.commands / "deploy.sh", "#!/usr/bin/env bash\necho hi\n")
    os.chmod(path, 0o755)
    service = _service()
    detail = service.read_file(service.list_files().files[0].id)

    service.update_file(
        detail.id, "#!/usr/bin/env bash\nset -e\necho bye\n", detail.revision
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o755


def _revision_of(path: Path) -> str:
    return file_revision(path.read_bytes())


def test_delete_removes_file_and_sidecars_of_every_locale(
    env: SimpleNamespace,
) -> None:
    greet = _write(env.commands / "greet.md", "---\nname: Greet\n---\nHi\n")
    _write(env.commands / "greet.metadata.yml", "description: Greeting\n")
    # A sidecar for another language must go too: a recreated command would
    # otherwise inherit stale metadata.
    _write(env.commands / "greet.metadata.ja.yml", "description: あいさつ\n")
    _write(env.commands / "keep.md", "---\nname: Keep\n---\nHi\n")
    _write(env.commands / "keep.metadata.yml", "description: Keep\n")
    service = _service("en")
    file_id = next(
        file.id for file in service.list_files().files if file.command == "greet"
    )

    remaining = service.delete_file(file_id, _revision_of(greet))

    assert [file.command for file in remaining.files] == ["keep"]
    assert not (env.commands / "greet.md").exists()
    assert not (env.commands / "greet.metadata.yml").exists()
    assert not (env.commands / "greet.metadata.ja.yml").exists()
    # Another command's files are untouched.
    assert (env.commands / "keep.md").exists()
    assert (env.commands / "keep.metadata.yml").exists()


def test_delete_keeps_sidecar_used_by_a_localized_sibling(env: SimpleNamespace) -> None:
    greet = _write(env.commands / "greet.md", "---\nname: Greet\n---\nHi\n")
    _write(env.commands / "greet.ja.md", "---\nname: 挨拶\n---\nこんにちは\n")
    _write(env.commands / "greet.metadata.yml", "description: Greeting\n")

    _service("en").delete_file(encode_file_id("greet.md"), _revision_of(greet))

    assert not (env.commands / "greet.md").exists()
    # The Japanese sibling now resolves the command and still needs the sidecar.
    assert (env.commands / "greet.metadata.yml").exists()


def test_delete_revision_conflict_keeps_the_file(env: SimpleNamespace) -> None:
    path = _write(env.commands / "greet.md", "---\nname: Greet\n---\nHi\n")
    service = _service()
    stale = _revision_of(path)
    # Another process rewrites the file after the editor loaded it.
    _write(env.commands / "greet.md", "---\nname: Greet\n---\nEdited elsewhere\n")

    with pytest.raises(AppApiError) as exc:
        service.delete_file(encode_file_id("greet.md"), stale)

    assert exc.value.code == "command_file_changed"
    assert exc.value.context["current_revision"] == _revision_of(path)
    assert path.exists()


def test_delete_unknown_id_is_not_found(env: SimpleNamespace) -> None:
    with pytest.raises(AppApiError) as exc:
        _service().delete_file(encode_file_id("missing.md"), "any")
    assert exc.value.code == "command_file_not_found"


def test_delete_shadowed_file_rejected(env: SimpleNamespace) -> None:
    _write(env.commands / "greet.md", "---\nname: Md\n---\nBody\n")
    shadowed = _write(env.commands / "greet.yaml", "commands: []\n")

    with pytest.raises(AppApiError) as exc:
        _service().delete_file(encode_file_id("greet.yaml"), _revision_of(shadowed))
    assert exc.value.code == "command_file_not_found"
    assert (env.commands / "greet.yaml").exists()


def test_read_unknown_id_is_not_found(env: SimpleNamespace) -> None:
    with pytest.raises(AppApiError) as exc:
        _service().read_file(encode_file_id("../escape.md"))
    assert exc.value.code == "command_file_not_found"


def test_read_absolute_path_id_rejected(env: SimpleNamespace) -> None:
    with pytest.raises(AppApiError) as exc:
        _service().read_file(encode_file_id("/etc/passwd"))
    assert exc.value.code == "command_file_not_found"


def test_read_metadata_sidecar_rejected(env: SimpleNamespace) -> None:
    _write(env.commands / "report.py", "def main(context):\n    return None\n")
    _write(env.commands / "report.metadata.yml", "name: Report\n")

    with pytest.raises(AppApiError) as exc:
        _service().read_file(encode_file_id("report.metadata.yml"))
    assert exc.value.code == "command_file_not_found"


def test_read_shadowed_file_rejected(env: SimpleNamespace) -> None:
    # greet.md wins over greet.yaml, so the .yaml is not editable via a crafted
    # id even though it exists under the shared root.
    _write(env.commands / "greet.md", "---\nname: Md\n---\nBody\n")
    _write(env.commands / "greet.yaml", "commands: []\n")

    with pytest.raises(AppApiError) as exc:
        _service().read_file(encode_file_id("greet.yaml"))
    assert exc.value.code == "command_file_not_found"


def test_read_inactive_locale_file_rejected(env: SimpleNamespace) -> None:
    _write(env.commands / "only.fr.md", "---\nname: FR\n---\nBody\n")

    with pytest.raises(AppApiError) as exc:
        _service("en").read_file(encode_file_id("only.fr.md"))
    assert exc.value.code == "command_file_not_found"


def test_update_shadowed_file_rejected(env: SimpleNamespace) -> None:
    _write(env.commands / "greet.md", "---\nname: Md\n---\nBody\n")
    _write(env.commands / "greet.yaml", "commands: []\n")

    with pytest.raises(AppApiError) as exc:
        _service().update_file(encode_file_id("greet.yaml"), "commands: []\n", "any")
    assert exc.value.code == "command_file_not_found"


def test_symlink_escape_rejected(env: SimpleNamespace, tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside.md", "---\nname: Outside\n---\nBody\n")
    link = env.commands / "escape.md"
    link.symlink_to(outside)

    with pytest.raises(AppApiError) as exc:
        _service().read_file(encode_file_id("escape.md"))
    assert exc.value.code == "command_file_not_found"


def test_atomic_write_leaves_no_temp_files(env: SimpleNamespace) -> None:
    _write(env.commands / "greet.md", "---\nname: Greet\n---\nHi\n")
    service = _service()
    detail = service.read_file(service.list_files().files[0].id)
    service.update_file(detail.id, "---\nname: Greet\n---\nX\n", detail.revision)

    leftovers = [p.name for p in env.commands.iterdir() if p.name != "greet.md"]
    assert leftovers == []
