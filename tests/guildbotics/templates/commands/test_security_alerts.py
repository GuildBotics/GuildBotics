"""The alert command formats provider data; only the host authorizes reads."""

import json
import shlex
from types import SimpleNamespace

import pytest
from markdown_it import MarkdownIt

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
        SimpleNamespace(
            person=SimpleNamespace(person_id="aiko"),
            get_code_hosting_service=lambda: service,
        ),
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
        assert t("commands.repository.security_alerts.empty", state="open") in empty
        page.update(items=[{"id": "opaque", "state": "open"}])
        result = json.loads(
            await module.main(context, "org/subgroup/repo", alert="42", output="json")
        )
        assert result["alerts"][0]["patched_version"] is None
        assert result["alerts"][0]["identifiers"] == []
        text = await module.main(context, "org/subgroup/repo", alert="42")
        assert t("commands.repository.security_alerts.no_patch") in text
        assert t("commands.repository.security_alerts.fields.manifest_path") not in text
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


@pytest.mark.asyncio
async def test_descriptions_only_in_detail_and_cannot_take_over_metadata(command):
    module, context, page, _ = command
    description = "### Impact\nAn attacker can do X.\n\n### Patches\nUpgrade to 2.\n\n### Workarounds\nNone."
    page["items"] = [
        {
            "id": "opaque",
            "state": "open",
            "package": "demo",
            "severity": "high",
            "summary": "A summary",
            "description": description,
            "affected_versions": "< 2",
            "patched_version": "2",
        }
    ]
    listing = await module.main(context, "org/repo")
    assert "## opaque · demo · high" in listing
    assert "### Impact" not in listing and "### Workarounds" not in listing
    detail = await module.main(context, "org/repo", alert="opaque")
    tokens = MarkdownIt().parse(detail)
    assert [
        (token.tag, token.level) for token in tokens if token.type == "heading_open"
    ] == [("h1", 0), ("h2", 0), ("h3", 1), ("h3", 1), ("h3", 1)]
    assert detail.index(
        t("commands.repository.security_alerts.fields.patched_version")
    ) < detail.index("> ### Impact")
    structured = json.loads(await module.main(context, "org/repo", output="json"))
    assert structured["alerts"][0]["description"] == description


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "id",
        "package",
        "severity",
        "summary",
        "url",
        "ecosystem",
        "manifest_path",
        "affected_versions",
        "patched_version",
        "created_at",
        "updated_at",
    ],
)
async def test_scalar_metadata_never_creates_markdown_structure(command, field):
    module, context, page, _ = command
    page["items"] = [
        {
            "id": "opaque",
            "state": "open",
            field: "value\n\n# Injected heading\n<h1>HTML heading</h1>",
        }
    ]
    text = await module.main(context, "org/repo")
    tokens = MarkdownIt().parse(text)
    assert [token.tag for token in tokens if token.type == "heading_open"] == [
        "h1",
        "h2",
    ]
    assert "value # Injected heading <h1>HTML heading</h1>" in text


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [False, True])
async def test_heading_inputs_and_identifiers_stay_on_one_line(command, detail):
    module, context, page, _ = command
    value = "org/demo.repo\n\n# heading\n> quote\n- item"
    normalized = "org/demo.repo # heading > quote - item"
    page["items"] = [
        {
            "id": "opaque",
            "state": "open",
            "identifiers": [{"type": value, "value": value}],
        }
    ]
    text = await module.main(context, value, alert=value if detail else "")
    assert normalized in text.splitlines()[0]
    label = t("commands.repository.security_alerts.fields.identifiers")
    assert f"- **{label}**: {normalized}: {normalized}" in text.splitlines()
    assert [
        token.tag for token in MarkdownIt().parse(text) if token.type == "heading_open"
    ] == ["h1", "h2"]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ja"])
@pytest.mark.parametrize("alert", ["", "41"])
async def test_python_command_preserves_readable_scalar_values(
    command, monkeypatch, tmp_path, language, alert
):
    from guildbotics.commands import python_command
    from guildbotics.commands.models import CommandSpec

    module, context, page, _ = command
    values = {
        "url": "https://github.com/org/repo/security/dependabot/41",
        "package": "lodash.merge",
        "ecosystem": "npm",
        "manifest_path": "src/package-lock.json",
        "severity": "high",
        "summary": "A **bold** summary & a <tag>",
        "affected_versions": ">= 4.0.0, < 4.17.21",
        "patched_version": "4.17.21",
        "created_at": "2026-09-30T12:00:00Z",
        "updated_at": "2026-09-30T13:00:00Z",
    }
    page["items"] = [
        {
            "id": "41",
            "state": "open",
            **values,
            "identifiers": [
                {"type": "GHSA", "value": "GHSA-abcd-1234-efgh"},
                {"type": "CVE", "value": "CVE-2024-1234"},
            ],
        }
    ]
    context.pipe = ""
    monkeypatch.setattr(python_command, "_load_python_module", lambda _: module)
    spec = CommandSpec(
        name="repository/security_alerts",
        base_dir=tmp_path,
        cwd=tmp_path,
        command_class=python_command.PythonCommand,
        path=tmp_path / "security_alerts.py",
        params={"repo": "org/repo", "alert": alert},
    )
    previous = get_language()
    set_language(language)
    try:
        outcome = await python_command.PythonCommand(context, spec, tmp_path).run()
        lines = outcome.text_output.splitlines()
        assert "## 41 · lodash.merge · high" in lines
        for field, value in values.items():
            label = t(f"commands.repository.security_alerts.fields.{field}")
            assert f"- **{label}**: {value}" in lines
        label = t("commands.repository.security_alerts.fields.identifiers")
        assert f"- **{label}**: GHSA: GHSA-abcd-1234-efgh, CVE: CVE-2024-1234" in lines
        assert outcome.result == outcome.text_output
    finally:
        set_language(previous)


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "ja"])
async def test_empty_page_names_filter_and_continuation_is_executable(
    command, language
):
    module, context, page, _ = command
    page["continuation"] = "opaque+cursor=="
    previous = get_language()
    set_language(language)
    try:
        text = await module.main(
            context, "org/subgroup/repo", state="resolved", page_size="7"
        )
        assert t("commands.repository.security_alerts.empty", state="resolved") in text
        assert "resolved" in text.splitlines()[0]
        fence = next(
            token for token in MarkdownIt().parse(text) if token.type == "fence"
        )
        arguments = shlex.split(fence.content)
        assert arguments[:5] == [
            "guildbotics",
            "run",
            "repository/security_alerts",
            "--person",
            "aiko",
        ]
        assert dict(argument.split("=", 1) for argument in arguments[5:]) == {
            "repo": "org/subgroup/repo",
            "state": "resolved",
            "page_size": "7",
            "continuation": "opaque+cursor==",
            "output": "markdown",
        }
    finally:
        set_language(previous)
