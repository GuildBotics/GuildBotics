from __future__ import annotations

from typing import Any, Sequence

from pydantic import BaseModel

from guildbotics.entities.team import Person
from guildbotics.intelligences.functions import to_text
from guildbotics.runtime.context import Context
from guildbotics.utils.fileio import get_prompt_path, load_markdown_with_frontmatter
from guildbotics.utils.text_utils import get_body_from_prompt


class CustomCommandError(RuntimeError):
    """Base error raised when a custom command cannot be executed."""


class PersonSelectionRequiredError(CustomCommandError):
    """Raised when no person could be inferred for a command."""

    def __init__(self, available: Sequence[str]):
        super().__init__("Person selection required.")
        self.available = list(available)


class PersonNotFoundError(CustomCommandError):
    """Raised when the requested person is not part of the team."""

    def __init__(self, identifier: str, available: Sequence[str]):
        super().__init__(f"Person '{identifier}' not found.")
        self.identifier = identifier
        self.available = list(available)


async def run_custom_command(
    base_context: Context,
    command_name: str,
    command_args: Sequence[str],
    stdin_text: str,
    person_identifier: str | None = None,
) -> str:
    """Execute a custom prompt command and return the rendered output."""
    person = _resolve_person(base_context.team.members, person_identifier)
    context = base_context.clone_for(person)

    prompt_output = await _execute_prompt_command(
        context,
        command_name,
        command_args,
        stdin_text,
    )
    return prompt_output


def _resolve_person(members: Sequence[Person], identifier: str | None) -> Person:
    if identifier is None:
        active_members = [member for member in members if member.is_active]
        if len(active_members) == 1:
            return active_members[0]
        available = _list_person_labels(members)
        raise PersonSelectionRequiredError(available)

    person = _find_person(members, identifier)
    if person is None:
        available = _list_person_labels(members)
        raise PersonNotFoundError(identifier, available)
    return person


def _find_person(members: Sequence[Person], identifier: str) -> Person | None:
    lower_identifier = identifier.casefold()
    for member in members:
        if member.person_id.casefold() == lower_identifier:
            return member
    for member in members:
        if member.name.casefold() == lower_identifier:
            return member
    return None


def _list_person_labels(members: Sequence[Person]) -> list[str]:
    labels: list[str] = []
    for member in members:
        label = member.person_id
        if member.name and member.name.casefold() != member.person_id.casefold():
            label = f"{label} ({member.name})"
        labels.append(label)
    return sorted(labels)


def _compose_prompt_message(prompt_body: str, stdin_text: str) -> str:
    prompt_part = prompt_body.strip("\r\n")
    stdin_part = stdin_text.strip("\r\n")
    if prompt_part and stdin_part:
        return f"{prompt_part}\n\n{stdin_part}"
    return prompt_part or stdin_part


def _stringify_output(output: Any) -> str:
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, BaseModel):
        return to_text(output)
    if isinstance(output, dict):
        return to_text(output)
    if isinstance(output, list):
        if output and isinstance(output[0], (BaseModel, dict)):
            return to_text(output)
        return "\n".join(str(item) for item in output)
    return str(output)


async def _execute_prompt_command(
    context: Context,
    command_name: str,
    command_args: Sequence[str],
    stdin_text: str,
) -> str:
    prompt_path = get_prompt_path(
        command_name,
        context.team.project.get_language_code(),
        context.person.person_id,
    )
    if not prompt_path.exists():
        raise CustomCommandError(
            f"Prompt '{command_name}' not found for {context.person.person_id}."
        )

    prompt_config = load_markdown_with_frontmatter(prompt_path)
    prompt_body = get_body_from_prompt(prompt_config, list(command_args))
    message = _compose_prompt_message(prompt_body, stdin_text)
    if not message.strip():
        raise CustomCommandError(
            "Provide input via stdin or ensure the prompt body is not empty."
        )

    brain_name = prompt_config.get("brain") or "default"
    try:
        brain = context.get_brain(brain_name)
    except Exception as exc:  # pragma: no cover - defensive guard
        raise CustomCommandError(
            f"Failed to load brain '{brain_name}' for person '{context.person.person_id}'."
        ) from exc

    try:
        output = await brain.run(message=message)
    except Exception as exc:  # pragma: no cover - propagate as driver error
        raise CustomCommandError(
            f"Custom command '{command_name}' execution failed: {exc}"
        ) from exc

    return _stringify_output(output)
