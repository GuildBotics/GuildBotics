"""Shared command file read/create/update service for the App API.

Desktop frontend never touches arbitrary local paths directly. This service
owns the editable root (the shared ``commands`` directory), the opaque file ID
scheme, revision hashing, atomic writes, path/symlink containment, and the
prospective-resolution shadow check used when creating a file. It converts
containment and persistence failures into :class:`AppApiError` with the shared
error codes. Command syntax is deliberately not a persistence concern: invalid
drafts remain saveable and are blocked by the execution-readiness guard.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from guildbotics.app_api.errors import AppApiError
from guildbotics.app_api.models import (
    CommandArgumentOption,
    CommandAuthoringChange,
    CommandFileDetail,
    CommandFileFormat,
    CommandFilesResponse,
    CommandFileSummary,
    CommandInputs,
    to_command_arguments,
    to_command_inputs,
)
from guildbotics.commands.discovery import (
    command_source,
    get_shared_commands_root,
    is_command_source_path,
    is_within,
    iter_effective_shared_commands,
    logical_command_name,
    resolve_command_path,
    resolve_prospective_shared_command,
)
from guildbotics.commands.errors import CommandError
from guildbotics.commands.formats import EXTENSION_BY_FORMAT, FORMAT_BY_EXTENSION
from guildbotics.commands.metadata import (
    default_command_label,
    load_command_metadata,
    parse_command_arguments,
    parse_command_input_policy,
)
from guildbotics.commands.registry import get_command_extensions
from guildbotics.commands.validation import (
    CommandValidationError,
    validate_generated_command_source,
)
from guildbotics.workspace.validation import MAX_SHARED_FILE_BYTES

#: Commands are shared config, so what they may hold is what the commit
#: boundary will carry. Derived rather than restated: a command saved above the
#: boundary's limit succeeds here and is then held back on every cycle, with
#: nothing the user can do about it from the screen that accepted it.
MAX_COMMAND_FILE_BYTES = MAX_SHARED_FILE_BYTES

_INITIAL_SOURCE: dict[CommandFileFormat, str] = {
    "markdown": "---\nname: New command\nbrain: none\ninputs:\n  message: hidden\n---\nTODO\n",
    "python": (
        'COMMAND_METADATA = {"inputs": {"message": "hidden"}}\n\n\n'
        'def main(context) -> str:\n    return ""\n'
    ),
    "shell": "#!/usr/bin/env bash\nset -euo pipefail\n",
    "yaml": "commands: []\n",
}

_STATUS_BY_CODE: dict[str, int] = {
    "command_file_invalid_name": 400,
    "command_file_invalid_source": 400,
    "command_file_unsupported_format": 400,
    "command_file_not_found": 404,
    "command_file_exists": 409,
    "command_file_changed": 409,
    "command_file_shadowed": 409,
    "command_file_too_large": 413,
}


def encode_file_id(relative_posix: str) -> str:
    """Encode a shared-root-relative POSIX path as an opaque URL-safe ID."""
    raw = base64.urlsafe_b64encode(relative_posix.encode("utf-8"))
    return raw.decode("ascii").rstrip("=")


def decode_file_id(file_id: str) -> str:
    """Decode an opaque file ID back into its relative POSIX path."""
    padding = "=" * (-len(file_id) % 4)
    try:
        return base64.urlsafe_b64decode(file_id + padding).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        raise _error(
            "command_file_not_found", f"Unknown command file: {file_id!r}."
        ) from exc


class CommandFileService:
    """Read, create and update files under the shared commands root."""

    def __init__(self, language_code: str) -> None:
        self._language = language_code
        self._root = get_shared_commands_root()

    def list_files(self) -> CommandFilesResponse:
        files = [
            self._summary(command, path)
            for command, path in iter_effective_shared_commands(self._language)
        ]
        files.sort(key=lambda summary: summary.command)
        return CommandFilesResponse(files=files)

    def read_file(self, file_id: str) -> CommandFileDetail:
        return self._detail(self.resolve_existing(file_id))

    def create_file(
        self,
        command: str,
        file_format: CommandFileFormat,
        content: str | None = None,
    ) -> CommandFileDetail:
        extension = EXTENSION_BY_FORMAT[file_format]
        relative = self._validate_command_name(command, extension)
        target = self._safe_target(relative)
        # Reject a duplicate logical command in any extension, not just the exact
        # target: a same-named file under a different extension is still the same
        # command and must not be created alongside it.
        for other in get_command_extensions():
            sibling = self._safe_target(f"{command}{other}")
            if sibling.exists() or sibling.is_symlink():
                raise _error(
                    "command_file_exists",
                    f"Command '{command}' already exists.",
                    {"relative_path": _relative_to(self._root, sibling)},
                )
        self._reject_shadowed_creation(command, target)
        source = _INITIAL_SOURCE[file_format] if content is None else content
        self._check_content(source)
        self._atomic_write(
            target,
            source,
            mode=0o755 if file_format == "shell" else 0o644,
        )
        return self._detail(target)

    def update_file(
        self, file_id: str, content: str, expected_revision: str
    ) -> CommandFileDetail:
        path = self.resolve_existing(file_id)
        self._check_content(content)
        current = file_revision(path.read_bytes())
        if current != expected_revision:
            raise _error(
                "command_file_changed",
                "The command file changed since it was loaded.",
                {"current_revision": current},
            )
        self._atomic_write(path, content)
        return self._detail(path)

    def delete_file(self, file_id: str, expected_revision: str) -> CommandFilesResponse:
        """Remove a shared command file and return the remaining list.

        Args:
            file_id: Opaque ID of the command file to remove.
            expected_revision: Revision the caller last loaded. A file changed
                since then is kept, so an edit made elsewhere is never
                discarded unseen.

        Returns:
            CommandFilesResponse: The command list after the deletion.

        Raises:
            AppApiError: If the file is unknown or changed since it was loaded.
        """
        path = self.resolve_existing(file_id)
        current = file_revision(path.read_bytes())
        if current != expected_revision:
            raise _error(
                "command_file_changed",
                "The command file changed since it was loaded.",
                {"current_revision": current},
            )
        path.unlink()
        return self.list_files()

    def apply_authoring_changes(
        self, changes: list[CommandAuthoringChange]
    ) -> list[CommandFileDetail]:
        """Validate and atomically apply a reviewed AI change set."""
        prepared: list[tuple[CommandAuthoringChange, Path, int]] = []
        commands: set[str] = set()
        targets: set[Path] = set()
        for change in changes:
            if change.command in commands:
                raise _error(
                    "command_file_exists",
                    f"Command '{change.command}' is proposed more than once.",
                )
            commands.add(change.command)
            extension = EXTENSION_BY_FORMAT[change.format]
            expected_relative = self._validate_command_name(change.command, extension)
            if change.relative_path != expected_relative:
                raise _error(
                    "command_file_invalid_name",
                    "The proposed command path does not match its name and format.",
                    {"relative_path": change.relative_path},
                )
            self._check_content(change.content)
            try:
                validate_generated_command_source(extension, change.content)
            except CommandValidationError as exc:
                raise _error(exc.code, str(exc), exc.context) from exc
            if change.operation == "update":
                target = self.resolve_existing(change.file_id)
                if _relative_to(self._root, target) != expected_relative:
                    raise _error(
                        "command_file_changed",
                        "The proposed update no longer targets the selected command.",
                    )
                current = target.read_bytes()
                if file_revision(current) != change.expected_revision:
                    raise _error(
                        "command_file_changed",
                        "The command file changed since the proposal was created.",
                        {"current_revision": file_revision(current)},
                    )
                mode = stat.S_IMODE(target.stat().st_mode)
            else:
                target = self._safe_target(expected_relative)
                for other in get_command_extensions():
                    sibling = self._safe_target(f"{change.command}{other}")
                    if sibling.exists() or sibling.is_symlink():
                        raise _error(
                            "command_file_exists",
                            f"Command '{change.command}' already exists.",
                            {"relative_path": _relative_to(self._root, sibling)},
                        )
                self._reject_shadowed_creation(change.command, target)
                mode = 0o755 if change.format == "shell" else 0o644
            resolved = target.resolve(strict=False)
            if resolved in targets:
                raise _error(
                    "command_file_exists",
                    f"Command '{change.command}' is proposed more than once.",
                )
            targets.add(resolved)
            prepared.append((change, target, mode))

        originals = {
            path: (path.read_bytes(), stat.S_IMODE(path.stat().st_mode))
            if path.exists()
            else None
            for _, path, _ in prepared
        }
        written: list[Path] = []
        try:
            for change, path, mode in prepared:
                self._atomic_write(path, change.content, mode)
                written.append(path)
        except BaseException:
            for path in reversed(written):
                original = originals[path]
                if original is None:
                    path.unlink(missing_ok=True)
                else:
                    data, mode = original
                    self._atomic_write_bytes(path, data, mode)
            raise
        return [self._detail(path) for _, path, _ in prepared]

    # -- internals -------------------------------------------------------

    def _reject_shadowed_creation(self, command: str, target: Path) -> None:
        resolved = resolve_prospective_shared_command(target, command, self._language)
        if resolved is not None and resolved.resolve(strict=False) == target.resolve(
            strict=False
        ):
            return
        source = command_source(resolved) if resolved is not None else "workspace"
        context = {"shadow_source": source}
        if resolved is not None:
            context["resolved_relative_path"] = _display_relative(resolved)
        raise _error(
            "command_file_shadowed",
            f"Command '{command}' would be shadowed by a higher-priority file.",
            context,
        )

    def _validate_command_name(self, command: str, extension: str) -> str:
        raw = command.strip()
        if not raw or raw.startswith("/") or "\\" in raw or "\x00" in raw:
            raise _invalid_name(command)
        segments = raw.split("/")
        for segment in segments:
            if not segment or "." in segment:
                raise _invalid_name(command)
        return f"{raw}{extension}"

    def _safe_target(self, relative: str) -> Path:
        candidate = self._root / relative
        root_resolved = self._root.resolve(strict=False)
        resolved = candidate.resolve(strict=False)
        if not is_within(resolved, root_resolved):
            raise _error(
                "command_file_not_found",
                f"Command path escapes the shared root: {relative!r}.",
            )
        return candidate

    def resolve_existing(self, file_id: str) -> Path:
        """Return the existing shared file for ``file_id`` or raise not-found."""
        relative = decode_file_id(file_id)
        relative_path = Path(relative)
        if (
            relative_path.is_absolute()
            or ".." in relative_path.parts
            or "\\" in relative
            or "\x00" in relative
        ):
            raise _error(
                "command_file_not_found", f"Unknown command file: {file_id!r}."
            )
        if not is_command_source_path(relative_path):
            raise _error(
                "command_file_not_found", f"Unknown command file: {file_id!r}."
            )
        target = self._safe_target(relative)
        if not target.is_file():
            raise _error(
                "command_file_not_found", f"Unknown command file: {file_id!r}."
            )
        # Only the effective shared file is editable: an inactive localized
        # sibling or a file shadowed by a higher-priority candidate is not shown
        # in the list and must not be read or written through a crafted id.
        command = logical_command_name(self._root, target, self._language)
        resolved = (
            resolve_command_path(command, self._language, person_id=None)
            if command
            else None
        )
        if resolved is None or resolved.resolve(strict=False) != target.resolve(
            strict=False
        ):
            raise _error(
                "command_file_not_found", f"Unknown command file: {file_id!r}."
            )
        return target

    def _check_content(self, content: str) -> None:
        if "\x00" in content:
            raise _error(
                "command_file_invalid_source",
                "Command source must not contain NUL bytes.",
                {"reason": "nul_byte"},
            )
        if len(content.encode("utf-8")) > MAX_COMMAND_FILE_BYTES:
            raise _error(
                "command_file_too_large",
                "Command source exceeds the maximum allowed size.",
                {"max_bytes": str(MAX_COMMAND_FILE_BYTES)},
            )

    def _atomic_write(self, path: Path, content: str, mode: int | None = None) -> None:
        self._atomic_write_bytes(path, content.encode("utf-8"), mode)

    def _atomic_write_bytes(
        self, path: Path, content: bytes, mode: int | None = None
    ) -> None:
        directory = path.parent
        directory.mkdir(parents=True, exist_ok=True)
        if mode is None and path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
        fd, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=".tmp-command-", suffix=path.suffix
        )
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if mode is not None:
                os.chmod(tmp_path, mode)
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    def _summary(self, command: str, path: Path) -> CommandFileSummary:
        metadata = load_command_metadata(path, self._language)
        relative = _relative_to(self._root, path)
        return CommandFileSummary(
            id=encode_file_id(relative),
            command=command,
            label=str(metadata.get("name") or default_command_label(command)),
            description=str(metadata.get("description", "")),
            relative_path=relative,
            format=FORMAT_BY_EXTENSION[path.suffix.lower()],
        )

    def _detail(self, path: Path) -> CommandFileDetail:
        command = logical_command_name(self._root, path, self._language)
        metadata = load_command_metadata(path, self._language)
        data = path.read_bytes()
        relative = _relative_to(self._root, path)
        return CommandFileDetail(
            id=encode_file_id(relative),
            command=command,
            label=str(metadata.get("name") or default_command_label(command)),
            description=str(metadata.get("description", "")),
            relative_path=relative,
            format=FORMAT_BY_EXTENSION[path.suffix.lower()],
            content=data.decode("utf-8", errors="replace"),
            revision=file_revision(data),
            arguments=_safe_arguments(path, metadata),
            inputs=_safe_inputs(metadata),
        )


def _safe_arguments(
    path: Path, metadata: dict[str, Any]
) -> list[CommandArgumentOption]:
    try:
        return to_command_arguments(parse_command_arguments(path, metadata))
    except CommandError:
        return []


def _safe_inputs(metadata: dict[str, Any]) -> CommandInputs:
    try:
        policy = parse_command_input_policy(metadata.get("inputs"))
    except CommandError:
        policy = parse_command_input_policy(None)
    return to_command_inputs(policy)


def file_revision(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _relative_to(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _display_relative(path: Path) -> str:
    try:
        return _relative_to(get_shared_commands_root(), path)
    except ValueError:
        return path.name


def _error(
    code: str, message: str, context: dict[str, str] | None = None
) -> AppApiError:
    return AppApiError(
        code,
        message,
        status_code=_STATUS_BY_CODE.get(code, 400),
        context=context or {},
    )


def _invalid_name(command: str) -> AppApiError:
    return _error(
        "command_file_invalid_name",
        f"Invalid command name: {command!r}.",
        {"command": command},
    )
