import os
from pathlib import Path

import yaml

CONFIG_PATH = ".guildbotics/config"


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


def get_storage_path() -> Path:
    """
    Get the storage path for the project.
    Returns:
        Path: The storage path for the project.
    """
    return Path.home() / ".guildbotics" / "data"


def get_workspace_path(person_id: str) -> Path:
    """
    Get the workspace path for a specific person.
    Args:
        person_id (str): The ID of the person.
    Returns:
        Path: The workspace path for the person.
    """
    return get_storage_path() / "workspaces" / person_id


def get_template_path() -> Path:
    """
    Get the path to the templates directory.
    Returns:
        Path: The path to the templates directory.
    """
    return find_package_subdir(Path("templates"))


def _get_config_path(path: Path) -> Path:

    config_dir = os.getenv("GUILDBOTICS_CONFIG_DIR", CONFIG_PATH)
    p = Path(config_dir) / path
    if p.exists():
        return p

    p = Path.home() / CONFIG_PATH / path
    if p.exists():
        return p

    return get_template_path() / path


def get_config_path(path_str: str, language_code: str | None = None) -> Path:
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


def load_markdown_with_frontmatter(file: Path) -> dict:
    """
    Load a Markdown file with YAML front matter and return as dict.
    Front matter keys are parsed as key-value pairs, and the body is stored under 'description'.

    Args:
        file (Path): Path to the Markdown file.

    Returns:
        dict: Parsed front matter with 'description' key for the body.
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


def get_prompt_path(
    name: str, language_code: str | None = None, person_id: str | None = None
) -> Path:
    """
    Get the path to a custom prompt file, either for a specific person or a general prompt.

    Args:
        name (str): The relative path to the prompt file.
        language_code (str | None): The language code for localization (optional).
        person_id (str | None): The ID of the person (optional).

    Returns:
        Path: The path to the custom prompt file.
    """
    if person_id:
        path = get_person_config_path(person_id, f"prompts/{name}.md", language_code)
        if path.exists():
            return path

    path = get_config_path(f"prompts/{name}.md", language_code)
    if path.exists():
        return path

    return get_person_config_path(person_id, f"intelligences/{name}.md", language_code)


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
