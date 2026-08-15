import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore

CONFIG_PATH = ".guildbotics/config"
GUILDBOTICS_WORKSPACE_ROOT = "GUILDBOTICS_WORKSPACE_ROOT"
GUILDBOTICS_CONFIG_DIR = "GUILDBOTICS_CONFIG_DIR"


class WorkspaceNotConfiguredError(RuntimeError):
    """Raised when a workspace path is required but none is selected."""


def find_package_subdir(subpath: Path) -> Path:
    """
    Find the project subdirectory relative to the current working directory.
    Args:
        subpath (Path): The subdirectory path to find.
    Returns:
        Path: The path to the found subdirectory.
    """
    current = Path(__file__).resolve().parent
    while True:
        candidate = current / subpath
        if candidate.exists():
            return candidate
        if current.parent == current:
            raise FileNotFoundError(f"Could not locate directory: {subpath}")
        current = current.parent


def get_machine_state_root() -> Path:
    """Return the machine-local GuildBotics state root."""
    return Path.home() / ".guildbotics" / "data"


def get_machine_state_path(*parts: str) -> Path:
    """Return a path under the machine-local GuildBotics state root."""
    return get_machine_state_root().joinpath(*parts)


def _resolve_path(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def workspace_root_from_config_dir(config_dir: Path) -> Path | None:
    """Return the workspace root when ``config_dir`` is ``<ws>/.guildbotics/config``."""
    resolved = _resolve_path(config_dir)
    if resolved.name == "config" and resolved.parent.name == ".guildbotics":
        return resolved.parent.parent
    return None


def get_workspace_root(workspace_root: Path | None = None) -> Path:
    """Return the selected GuildBotics workspace root.

    Resolution order:
    1. The explicit ``workspace_root`` argument
    2. ``GUILDBOTICS_WORKSPACE_ROOT``
    3. ``GUILDBOTICS_CONFIG_DIR`` when it points at ``<ws>/.guildbotics/config``

    The process cwd and member working clones are never used as a workspace.
    """
    if workspace_root is not None:
        return _resolve_path(workspace_root)
    configured = os.getenv(GUILDBOTICS_WORKSPACE_ROOT, "").strip()
    if configured:
        return _resolve_path(Path(configured))
    config_dir = os.getenv(GUILDBOTICS_CONFIG_DIR, "").strip()
    if config_dir:
        derived = workspace_root_from_config_dir(Path(config_dir))
        if derived is not None:
            return derived
    raise WorkspaceNotConfiguredError(
        "No GuildBotics workspace is selected. Use --workspace, "
        "`guildbotics workspace use <path>`, or set GUILDBOTICS_WORKSPACE_ROOT."
    )


def apply_workspace_root(workspace_root: Path) -> Path:
    """Publish the selected workspace root and its config dir."""
    resolved = _resolve_path(workspace_root)
    os.environ[GUILDBOTICS_WORKSPACE_ROOT] = str(resolved)
    os.environ[GUILDBOTICS_CONFIG_DIR] = str(resolved / ".guildbotics" / "config")
    return resolved


def get_workspace_config_dir(workspace_root: Path | None = None) -> Path:
    """Return ``<workspace>/.guildbotics/config``."""
    return get_workspace_root(workspace_root) / ".guildbotics" / "config"


def get_workspace_state_path(
    *parts: str,
    workspace_root: Path | None = None,
) -> Path:
    """Return a path under ``<workspace>/.guildbotics/state``."""
    return get_workspace_root(workspace_root).joinpath(".guildbotics", "state", *parts)


def get_workspace_local_path(
    *parts: str,
    workspace_root: Path | None = None,
) -> Path:
    """Return a path under ``<workspace>/.guildbotics/local``."""
    return get_workspace_root(workspace_root).joinpath(".guildbotics", "local", *parts)


def get_member_clone_path(person_id: str, workspace_root: Path | None = None) -> Path:
    """Return the member working clone directory (not synchronized)."""
    return get_workspace_local_path("clones", person_id, workspace_root=workspace_root)


def get_workspace_work_path(
    *parts: str,
    workspace_root: Path | None = None,
) -> Path:
    """Return a path under the local work directory."""
    return get_workspace_local_path("work", *parts, workspace_root=workspace_root)


def get_template_path() -> Path:
    """
    Get the path to the templates directory.
    Returns:
        Path: The path to the templates directory.
    """
    return find_package_subdir(Path("templates"))


def get_primary_config_dir() -> Path | None:
    """Return the selected workspace config dir, or ``None`` when unset."""
    configured = os.getenv(GUILDBOTICS_CONFIG_DIR, "").strip()
    if configured:
        config_dir = Path(configured).expanduser()
        if not config_dir.is_absolute():
            config_dir = Path.cwd() / config_dir
        return config_dir
    try:
        return get_workspace_config_dir()
    except WorkspaceNotConfiguredError:
        return None


def get_primary_config_path(path: Path) -> Path:
    """
    Get the primary configuration path from the selected workspace.

    The returned path may not exist; check with .exists() if needed.

    Args:
        path (Path): The relative path to the configuration file.

    Returns:
        Path: The absolute path to the configuration file.

    Raises:
        WorkspaceNotConfiguredError: When no workspace is selected. Reads
            that want the package-template fallback use ``get_config_path``;
            the primary path is also a write target and must never point
            into the package templates.
    """
    config_dir = get_primary_config_dir()
    if config_dir is None:
        raise WorkspaceNotConfiguredError("No GuildBotics workspace is selected.")
    return config_dir / path


def _get_config_path(path: Path) -> Path:
    """
    Resolve the configuration path by searching in priority order.

    Returns the first existing file from the following locations:
    1. Primary config path (GUILDBOTICS_CONFIG_DIR or CONFIG_PATH)
    2. Template path (returned even if not found)

    Args:
        path (Path): The relative path to the configuration file.

    Returns:
        Path: An absolute path to an existing file, or the template path fallback.
    """
    try:
        p = get_primary_config_path(path)
    except WorkspaceNotConfiguredError:
        p = None
    if p is not None and p.exists():
        return p

    return get_template_path() / path


def get_config_path(path_str: str, language_code: str | None = None) -> Path:
    """
    Get the configuration path, with optional language-specific localization.

    If language_code is provided, searches for files in this order:
    1. File with language code suffix (e.g., "config.ja.yaml")
    2. English file with ".en" suffix (e.g., "config.en.yaml")
    3. File without suffix (fallback)

    Each search uses _get_config_path() to check multiple locations.

    Args:
        path_str (str): The relative path to the configuration file.
        language_code (str | None): The language code for localization (optional).

    Returns:
        Path: The absolute path to the configuration file.
    """
    if language_code:
        p = Path(path_str)
        new_path = _get_config_path(p.with_stem(f"{p.stem}.{language_code}"))
        if new_path.exists():
            return new_path
        new_path = _get_config_path(p.with_stem(f"{p.stem}.en"))
        if new_path.exists():
            return new_path

    return _get_config_path(Path(path_str))


def get_person_config_path(
    person_id, path_str: str, language_code: str | None = None
) -> Path:
    """
    Get the configuration path for a specific person.
    Args:
        person_id (str): The ID of the person.
        path_str (str): The relative path to the configuration file.
        language_code (str | None): The language code for localization (optional).
    Returns:
        Path: The absolute path to the configuration file.
    """
    p = get_config_path(f"team/members/{person_id}/{path_str}", language_code)
    if p.exists():
        return p
    return get_config_path(path_str, language_code)


def load_person_slot_mapping(person_id: str, path_str: str) -> dict:
    """Load a slot mapping for a person, merged over the team-level defaults.

    Slot mappings (for example ``intelligences/model_mapping.yml``) map a slot
    name to a definition reference. A member may define their own mapping file
    to override individual slots; any slot the member does not define is
    inherited from the team-level mapping. This per-key merge means a partial
    member override never drops team-provided slots.

    Args:
        person_id (str): The ID of the person.
        path_str (str): The relative path to the mapping file
            (for example ``"intelligences/model_mapping.yml"``).

    Returns:
        dict: The merged slot mapping, member entries taking precedence.
    """
    mapping: dict = {}
    team_path = get_config_path(path_str)
    if team_path.exists():
        team_mapping = load_yaml_file(team_path)
        if isinstance(team_mapping, dict):
            mapping.update(team_mapping)
    member_path = get_config_path(f"team/members/{person_id}/{path_str}")
    if member_path.exists() and member_path != team_path:
        member_mapping = load_yaml_file(member_path)
        if isinstance(member_mapping, dict):
            mapping.update(member_mapping)
    return mapping


def load_markdown_with_frontmatter(file: Path) -> dict:
    """
    Load a Markdown file with YAML front matter and return as dict.
    Front matter keys are parsed as key-value pairs, and the body is stored under 'body'.

    Args:
        file (Path): Path to the Markdown file.

    Returns:
        dict: Parsed front matter with 'body' key for the markdown body.
    """
    with file.open("r", encoding="utf-8") as f:
        content = f.read()

    # Split front matter and body, tolerating different newline styles
    front_matter = ""
    body = content

    if content.startswith("---"):
        lines = content.splitlines(keepends=True)
        if lines and lines[0].strip("\r\n") == "---":
            front_lines = []
            closing_index = None

            for idx, line in enumerate(lines[1:], start=1):
                if line.strip("\r\n") == "---":
                    closing_index = idx
                    break
                front_lines.append(line)

            if closing_index is not None:
                front_matter = "".join(front_lines)
                body = "".join(lines[closing_index + 1 :])

    # Parse front matter as YAML
    metadata = yaml.safe_load(front_matter) if front_matter.strip() else {}

    # Ensure metadata is a dict
    if not isinstance(metadata, dict):
        metadata = {}

    # Add body
    metadata["body"] = body.strip()

    return metadata


def load_yaml_file(file: Path) -> dict | list[dict]:
    with file.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_yaml_file(file_path: Path, data: dict | list[dict]) -> None:
    """
    Save the given data to a YAML file, omitting keys with None or empty-string values.

    Args:
        file_path (Path): Path to the output YAML file.
        data (dict or list of dict): Data to save.

    Returns:
        None
    """
    # Clean data by removing keys with None or empty-string values
    cleaned = _clean_data(data)
    with file_path.open("w", encoding="utf-8") as f:
        yaml.dump(
            cleaned, f, allow_unicode=True, sort_keys=False, default_flow_style=False
        )


def _clean_data(data):
    """
    Recursively remove keys with None or empty-string values from dicts, and clean lists.

    Args:
        data (dict or list): Input data structure.

    Returns:
        Cleaned data with empty entries removed.
    """
    if isinstance(data, dict):
        return {k: _clean_data(v) for k, v in data.items() if v is not None and v != ""}
    if isinstance(data, list):
        return [_clean_data(item) for item in data]
    return data


def get_intelligence_roots(
    config_dir: Path, person_id: str | None, sub_dir: str
) -> list[Path]:
    """Resolve member, team, and template intelligence configuration roots in priority order."""
    roots: list[Path] = []
    if person_id:
        roots.append(
            config_dir / "team/members" / person_id / "intelligences" / sub_dir
        )
    roots.append(config_dir / "intelligences" / sub_dir)
    roots.append(get_template_path() / "intelligences" / sub_dir)
    return roots


def load_yaml_dict(path: Path) -> dict[str, Any]:
    """Safely load a YAML file as a dictionary, returning empty dict if not found or invalid."""
    if not path.exists():
        return {}
    try:
        data = load_yaml_file(path)
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}
