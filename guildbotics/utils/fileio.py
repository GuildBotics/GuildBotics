import json
import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore

CONFIG_PATH = ".guildbotics/config"
GUILDBOTICS_WORKSPACE_ROOT = "GUILDBOTICS_WORKSPACE_ROOT"
GUILDBOTICS_CONFIG_DIR = "GUILDBOTICS_CONFIG_DIR"
GUILDBOTICS_IDENTITY_ROOT = "GUILDBOTICS_IDENTITY_ROOT"
RESOLVED_GUEST_ENV = "GUILDBOTICS_RESOLVED_GUEST"

# Installed launchers may enter through a symlink/junction. Canonicalize the
# trusted package once, before any mount inspection, and share that spelling.
PACKAGE_ROOT = Path(__file__).resolve().parent.parent


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
    current = PACKAGE_ROOT
    while True:
        candidate = current / subpath
        if candidate.exists():
            return candidate
        if current.parent == current:
            raise FileNotFoundError(f"Could not locate directory: {subpath}")
        current = current.parent


def get_machine_root() -> Path:
    """Return the machine-wide GuildBotics directory, ``~/.guildbotics``.

    Everything that belongs to the machine rather than to a workspace lives
    under it: the managed CLI, this device's state, and the members'
    identities.
    """
    return Path.home() / ".guildbotics"


def get_identity_root() -> Path:
    """Return the root of the members' identities, which every workspace shares.

    A member is one person -- a name, a character, accounts with each service
    and their credentials -- whichever workspace it works in, so that much is
    kept once per machine rather than in each workspace. The root is laid out
    like a workspace root, with ``.guildbotics/config``, ``state``, and
    ``local`` under it, so the stores and locks that take a workspace root take
    this one unchanged. A command's isolated environment
    is told where it is mounted through ``GUILDBOTICS_IDENTITY_ROOT``.
    """
    configured = os.getenv(GUILDBOTICS_IDENTITY_ROOT, "").strip()
    return Path(configured) if configured else get_machine_root() / "identity"


def get_identity_config_dir() -> Path:
    """Return ``<identity root>/.guildbotics/config``."""
    return get_identity_root() / CONFIG_PATH


def workspace_root_from_config_dir(config_dir: Path) -> Path | None:
    """Return the workspace root when ``config_dir`` is ``<ws>/.guildbotics/config``."""
    from guildbotics.utils.safe_paths import inspect_host_path

    resolved = inspect_host_path(config_dir, missing=True).path
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
        from guildbotics.utils.safe_paths import inspect_host_path

        return inspect_host_path(workspace_root, missing=True).path
    configured = os.getenv(GUILDBOTICS_WORKSPACE_ROOT, "").strip()
    if configured:
        from guildbotics.utils.safe_paths import inspect_host_path

        return inspect_host_path(Path(configured), missing=True).path
    config_dir = os.getenv(GUILDBOTICS_CONFIG_DIR, "").strip()
    if config_dir:
        derived = workspace_root_from_config_dir(Path(config_dir))
        if derived is not None:
            return derived
    raise WorkspaceNotConfiguredError(
        "No GuildBotics workspace is selected. Use --workspace, "
        "`guildbotics workspace use <path>`, or set GUILDBOTICS_WORKSPACE_ROOT."
    )


def get_workspace_config_dir(workspace_root: Path | None = None) -> Path:
    """Return ``<workspace>/.guildbotics/config``."""
    return get_workspace_root(workspace_root) / ".guildbotics" / "config"


def get_workspace_local_path(
    *parts: str,
    workspace_root: Path | None = None,
) -> Path:
    """Return a path under ``<workspace>/.guildbotics/local``."""
    return get_workspace_root(workspace_root).joinpath(".guildbotics", "local", *parts)


def get_member_clone_path(person_id: str, workspace_root: Path | None = None) -> Path:
    """Return the member working clone directory (this machine's own)."""
    from guildbotics.utils.person_id import validate_person_id

    person_id = validate_person_id(person_id)
    resolved_path = os.environ.get(RESOLVED_GUEST_ENV)
    if resolved_path is not None:
        data = json.loads(Path(resolved_path).read_text(encoding="utf-8"))
        if data["member"]["person_id"] != person_id or workspace_root is not None:
            raise ValueError("A guest can only use its resolved member clone path.")
        path = Path(data["clone_paths"][person_id])
        if not path.is_absolute():
            raise ValueError("The resolved member clone path must be absolute.")
        return path
    return get_workspace_local_path("clones", person_id, workspace_root=workspace_root)


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

    from guildbotics.utils.person_id import person_config_directory

    p = get_config_path(
        (person_config_directory(person_id) / path_str).as_posix(), language_code
    )
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
    from guildbotics.utils.person_id import person_config_directory

    member_path = get_config_path(
        (person_config_directory(person_id) / path_str).as_posix()
    )
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


def get_intelligence_roots(
    config_dir: Path, person_id: str | None, sub_dir: str
) -> list[Path]:
    """Resolve member, team, and template intelligence configuration roots in priority order."""
    roots: list[Path] = []
    from guildbotics.utils.person_id import person_config_directory

    if person_id is not None:
        roots.append(
            config_dir / person_config_directory(person_id) / "intelligences" / sub_dir
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
    except OSError, yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}
