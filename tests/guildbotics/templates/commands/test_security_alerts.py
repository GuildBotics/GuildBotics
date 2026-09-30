"""The alert command formats provider data; only the host authorizes reads."""

import json
from types import SimpleNamespace

import pytest

from guildbotics.commands.discovery import (
    iter_command_candidate_names,
    resolve_command_path,
)
from guildbotics.commands.errors import CommandError
from guildbotics.commands.metadata import (
    load_command_metadata,
    parse_command_access,
    parse_command_arguments,
)
from guildbotics.commands.python_command import _load_python_module
from guildbotics.integrations.window import MemberCommandError, WindowCodeHostingService
from guildbotics.utils.fileio import get_template_path
from guildbotics.utils.i18n_tool import get_language, set_language, t


@pytest.fixture
def command(monkeypatch):
    module = _load_python_module(
        get_template_path() / "commands/repository/security_alerts.py"
    )
    calls = []
    page = {"items": [], "continuation": None}

    class Window:
        async def acall(self, name, **kwargs):
            calls.append((name, kwargs))
            return {"exit_code": 0, "stdout": json.dumps(page), "stderr": ""}

    service = WindowCodeHostingService(Window(), "aiko")
    return (
        module,
        SimpleNamespace(get_code_hosting_service=lambda: service),
        page,
        calls,
    )


@pytest.mark.asyncio
async def test_structured_alert_fields_and_next_page(command):
    module, context, page, calls = command
    page.update(
        items=[
            {
                "id": "alert:opaque",
                "state": "resolved",
                "url": "https://hosting.test/vulns/opaque",
                "package": "demo",
                "ecosystem": "pip",
                "manifest_path": "uv.lock",
                "severity": "high",
                "identifiers": [{"type": "OSV", "value": "OSV-example"}],
                "summary": "A summary",
                "description": "A description",
                "affected_versions": "< 2",
                "patched_version": "2",
                "created_at": "2026-01-01",
                "updated_at": "2026-09-30",
            }
        ],
        continuation="next-page",
    )
    result = json.loads(
        await module.main(context, "org/subgroup/repo", output="json", page_size="1")
    )
    assert result == {
        "repo": "org/subgroup/repo",
        "continuation": "next-page",
        "alerts": page["items"],
    }

    await module.main(
        context,
        "org/subgroup/repo",
        output="json",
        page_size="1",
        continuation=result["continuation"],
    )
    name, call = calls[-1]
    assert name == "member"
    assert call["stdin"] == ""
    assert call["arguments"] == [
        "repository",
        "read",
        "--person",
        "aiko",
        "--resource",
        "dependency_alerts",
        "--repo",
        "org/subgroup/repo",
        "--identifier",
        "",
        "--params",
        '{"state": "open", "page_size": 1}',
        "--continuation",
        "next-page",
        "--format",
        "json",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ja"])
async def test_empty_missing_fields_and_no_patch_are_localized(command, language):
    module, context, page, _ = command
    previous = get_language()
    set_language(language)
    try:
        empty = await module.main(context, "org/subgroup/repo")
        assert t("commands.repository.security_alerts.empty") in empty
        page.update(items=[{"id": "opaque", "state": "open"}])
        result = json.loads(
            await module.main(context, "org/subgroup/repo", alert="42", output="json")
        )
        assert result["alerts"][0]["patched_version"] is None
        assert result["alerts"][0]["identifiers"] == []
        text = await module.main(context, "org/subgroup/repo", alert="42")
        assert t("commands.repository.security_alerts.no_patch") in text
        assert t("commands.repository.security_alerts.fields.manifest_path") in text
        assert "commands.repository.security_alerts" not in text
    finally:
        set_language(previous)


@pytest.mark.asyncio
async def test_failed_read_is_not_an_empty_success(command, monkeypatch):
    module, context, _, _ = command

    class FailedWindow:
        async def acall(self, *_args, **_kwargs):
            return {"exit_code": 1, "stdout": "", "stderr": "access denied"}

    context.get_code_hosting_service = lambda: WindowCodeHostingService(
        FailedWindow(), "aiko"
    )
    with pytest.raises(MemberCommandError, match="access denied"):
        await module.main(context, "org/subgroup/repo")


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [{"page_size": "invalid"}, {"output": "csv"}])
async def test_bad_display_arguments_do_not_read(command, options):
    module, context, _, calls = command
    with pytest.raises(CommandError):
        await module.main(context, "org/subgroup/repo", **options)
    assert not calls


def test_command_is_discoverable_with_arguments_and_read_only_metadata():
    root = get_template_path() / "commands"
    name = "repository/security_alerts"
    assert name in iter_command_candidate_names([root], "en")
    path = resolve_command_path(name, "en")
    metadata = load_command_metadata(path, "en")
    assert parse_command_access(metadata).read_only
    arguments = {arg.name: arg for arg in parse_command_arguments(path, metadata, name)}
    assert arguments["repo"].required
    assert arguments["output"].default == "markdown"
    assert "continuation" in arguments


@pytest.mark.asyncio
async def test_python_command_output_is_json_including_empty_page(
    command, monkeypatch, tmp_path
):
    from guildbotics.commands import python_command
    from guildbotics.commands.models import CommandSpec

    module, context, _, _ = command
    context.pipe = ""
    monkeypatch.setattr(python_command, "_load_python_module", lambda _: module)
    spec = CommandSpec(
        name="repository/security_alerts",
        base_dir=tmp_path,
        cwd=tmp_path,
        command_class=python_command.PythonCommand,
        path=tmp_path / "security_alerts.py",
        params={"repo": "org/subgroup/repo", "output": "json"},
    )
    outcome = await python_command.PythonCommand(context, spec, tmp_path).run()
    assert json.loads(outcome.text_output) == {
        "repo": "org/subgroup/repo",
        "alerts": [],
        "continuation": None,
    }
    assert outcome.result == outcome.text_output
