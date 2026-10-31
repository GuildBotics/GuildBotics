"""Thin member CLI: interpret the backend's plan and handle local data only."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO

import click
import httpx

from guildbotics.capabilities.artifact_archive import (
    ArtifactError,
    unpacked,
    write_download,
)
from guildbotics.cli import _resolve_version
from guildbotics.cli.member_output import to_markdown
from guildbotics.local_git import (
    GitError,
    LocalGit,
    commit_staged,
    current_repository,
    known_commits,
)
from guildbotics.observability.interactive_sessions import (
    interactive_host,
    interactive_thread_key,
)
from guildbotics.utils.fileio import (
    get_identity_config_dir,
    get_workspace_root,
    workspace_root_from_config_dir,
)
from guildbotics.utils.i18n_tool import get_language, t
from guildbotics.utils.local_api import connect_local_api
from guildbotics.utils.log_utils import get_logger
from guildbotics.utils.secret_redaction import redact_secrets, register_secret_names
from guildbotics.utils.workspace_state import read_active_workspace
from guildbotics.workspace.identity import read_workspace_identity


def _workspace(arguments: list[str]) -> tuple[Path, list[str]]:
    chosen = None
    result = []
    tokens = iter(arguments)
    for token in tokens:
        if token == "--workspace":
            try:
                chosen = next(tokens)
            except StopIteration as exc:
                raise click.UsageError(
                    "Option '--workspace' requires an argument."
                ) from exc
        elif token.startswith("--workspace="):
            chosen = token.partition("=")[2]
        else:
            result.append(token)
    configured = os.environ.get("GUILDBOTICS_CONFIG_DIR", "")
    root = (
        Path(chosen)
        if chosen is not None
        else workspace_root_from_config_dir(Path(configured))
        if configured
        else None
    )
    if root is None and os.environ.get("GUILDBOTICS_WORKSPACE_ROOT"):
        root = get_workspace_root()
    if root is None and (Path.cwd() / ".guildbotics/config").is_dir():
        root = Path.cwd()
    if root is None:
        state = read_active_workspace()
        root = state.workspace if state else None
    if root is None:
        raise click.ClickException(t("cli.member.client.workspace_required"))
    return root.expanduser().resolve(), result


def _read_content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise click.ClickException(
            t("cli.member.content.file_not_utf8", path=path)
        ) from exc
    except OSError as exc:
        raise click.ClickException(
            t("cli.member.content.file_read_failed", path=path, error=exc)
        ) from exc


def _exchange(
    client: httpx.Client,
    path: str,
    body: dict[str, Any],
    target: BinaryIO,
    bundle: Path | None = None,
) -> dict[str, Any]:
    """Send once; a failed POST may already have executed its write."""

    def upload() -> Iterator[bytes]:
        yield (json.dumps(body) + "\n").encode()
        if bundle is not None:
            with bundle.open("rb") as source:
                while chunk := source.read(1 << 16):
                    yield chunk

    kwargs = {"content": upload()} if path == "/member/git" else {"json": body}
    with client.stream("POST", path, **kwargs) as response:
        if response.is_error:
            response.read()
            try:
                message = response.json()["message"]
            except ValueError, KeyError:
                message = f"HTTP {response.status_code}"
            raise click.ClickException(message)
        header = bytearray()
        metadata = None
        size = 0
        for received in response.iter_bytes():
            chunk = received
            if metadata is None:
                header.extend(chunk)
                line, separator, tail = header.partition(b"\n")
                if len(line) > 32 << 20:
                    raise ValueError("Member response metadata is too large.")
                if not separator:
                    continue
                metadata = json.loads(line)
                chunk = bytes(tail)
                header.clear()
            size += len(chunk)
            if size > 1 << 30:
                raise ValueError("Member response data is too large.")
            target.write(chunk)
        if metadata is None or size != metadata.get("size", 0):
            raise ValueError("Incomplete member response.")
        return metadata


def _result(result: dict[str, Any]) -> None:
    click.echo(result["stdout"], nl=False)
    click.echo(result["stderr"], nl=False, err=True)
    if result["exit_code"]:
        raise click.exceptions.Exit(result["exit_code"])


def _git_exchange(
    client: httpx.Client,
    body: dict[str, Any],
    phase: str,
    data: dict[str, Any],
    output: Path,
    bundle: Path | None = None,
) -> dict[str, Any]:
    with output.open("wb") as target:
        result = _exchange(
            client,
            "/member/git",
            {**body, "phase": phase, "data": data},
            target,
            bundle,
        )
    if result["exit_code"]:
        raise GitError(result["stderr"])
    return json.loads(result["stdout"])


def _current_git(
    client: httpx.Client,
    body: dict[str, Any],
    plan: dict[str, Any],
    stdin: str,
    *,
    cwd: Path | None = None,
) -> dict[str, Any]:
    root = current_repository(Path(plan["repo_path"]), cwd or Path.cwd())
    git = LocalGit(root)
    operation = plan["operation"]
    with tempfile.TemporaryDirectory(prefix="guildbotics-client-git-") as directory:
        incoming, outgoing = (
            Path(directory) / "in.bundle",
            Path(directory) / "out.bundle",
        )

        def call(
            phase: str, data: dict[str, Any], bundle: Path | None = None
        ) -> dict[str, Any]:
            return _git_exchange(client, body, phase, data, outgoing, bundle)

        commit = None
        if operation in {"commit", "publish"}:
            identity = call("identity", {})
            commit = commit_staged(
                git, root, stdin, identity["user"], identity["email"]
            )
            call("record", commit)
            if operation == "commit":
                return commit
        branch = (
            git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.decode().strip()
        )
        head = git(
            "rev-parse", "-q", "--verify", f"refs/heads/{branch}^{{commit}}", ok=(0, 1)
        )
        if head.returncode:
            raise GitError(f"Branch '{branch}' has no commit to push.")
        data = {
            "remotes": git.push_urls(),
            "branch": branch,
            "repo_path": str(root),
            "head": head.stdout.decode().strip(),
        }
        known = call("known", data)["known"]
        push_failed = False
        try:
            for attempt in range(2):
                if incoming.exists():
                    incoming.unlink()
                transfer = git.branch_bundle(
                    branch, known if attempt == 0 else (), incoming
                )
                pushed = call("push", data, incoming if transfer else None)
                if not pushed.get("retry"):
                    break
            else:
                raise GitError("The backend could not receive the branch history.")
        except BaseException:
            push_failed = True
            raise
        finally:
            try:
                for exclusions in (sorted(known_commits(git)), []):
                    tracking = call("tracking", {**data, "known": exclusions})
                    bundle = outgoing if outgoing.stat().st_size else None
                    if bundle is None or git.verify_bundle(bundle):
                        git.update_tracking(bundle, tracking["refs"])
                        break
                else:
                    raise GitError(
                        "The returned tracking history could not be verified."
                    )
            except Exception as exc:
                if not push_failed:
                    raise
                get_logger().warning("Could not return origin tracking refs: %s", exc)
        if commit is not None:
            return {
                **pushed,
                **commit,
                "pushed": pushed["pushed"],
                "status": "published"
                if commit["commit_sha"] or pushed["pushed"]
                else "up_to_date",
            }
        return pushed


@click.command(
    name="member",
    add_help_option=False,
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.pass_context
def member(ctx: click.Context) -> None:
    """Operate as a configured GuildBotics member through the running backend."""
    workspace, arguments = _workspace(ctx.args)
    identity = read_workspace_identity(workspace)
    if identity is None:
        raise click.ClickException(t("cli.member.client.workspace_required"))
    register_secret_names(
        (workspace / ".guildbotics/config", get_identity_config_dir())
    )
    original_arguments = arguments[:]
    arguments = [redact_secrets(arg) for arg in arguments]
    body = {
        "version": _resolve_version(),
        "workspace_id": identity.workspace_id,
        "arguments": arguments,
        "interactive_host": interactive_host(),
        "interactive_thread_key": interactive_thread_key(),
    }
    try:
        with connect_local_api(
            headers={"Accept-Language": get_language()}, timeout=None
        ) as client:
            if client is None:
                raise click.ClickException(t("cli.member.client.backend_required"))
            with tempfile.TemporaryFile() as output:
                described = _exchange(client, "/member/plan", body, output)
            _result(described)
            plan = described["plan"]
            if (
                "result" not in plan
                and not plan["memory"]
                and arguments != original_arguments
            ):
                body["arguments"] = original_arguments
                with tempfile.TemporaryFile() as output:
                    described = _exchange(client, "/member/plan", body, output)
                _result(described)
                plan = described["plan"]
            if "result" in plan:
                _result(plan["result"])
                return
            stdin = ""
            if plan["content_file"] is not None:
                source = plan["file_source"]
                stdin = _read_content(
                    Path(original_arguments[source["index"]][source["offset"] :])
                )
            elif plan["stdin"]:
                stdin = click.get_text_stream("stdin").read()
            arguments = plan["arguments"]
            if plan["memory"]:
                arguments = [redact_secrets(arg) for arg in arguments]
                stdin = redact_secrets(stdin)
            if plan["download"]:
                arguments = [*arguments, "--format", "json"]
            body.update(arguments=arguments, stdin=stdin)
            if plan["git"] is not None:
                result = _current_git(client, body, plan["git"], stdin)
                click.echo(
                    json.dumps(result, ensure_ascii=False)
                    if plan["git"]["output_format"] == "json"
                    else to_markdown(result)
                )
                return
            with tempfile.TemporaryFile() as output:
                result = _exchange(client, "/member/run", body, output)
                if "download" in result and result["exit_code"] == 0:
                    output.seek(0)
                    destination = (
                        Path(result["download"]["destination"]).expanduser().resolve()
                    )
                    files = write_download(
                        output, destination, *result["download"]["attachment"]
                    )
                    payload = {
                        **json.loads(result["stdout"]),
                        **unpacked(destination, files),
                    }
                    result["stdout"] = (
                        json.dumps(payload, ensure_ascii=False)
                        if plan["output_format"] == "json"
                        else to_markdown(payload)
                    ) + "\n"
                _result(result)
    except (httpx.HTTPError, OSError, ValueError, GitError, ArtifactError) as exc:
        raise click.ClickException(str(exc)) from exc
