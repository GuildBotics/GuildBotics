import os
import tempfile
from pathlib import Path


def create_git_askpass_script() -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        delete=False,
        prefix="guildbotics-git-askpass-",
        suffix=".sh",
    ) as askpass:
        askpass.write(
            "#!/bin/sh\n"
            'case "$1" in\n'
            '*Username*) printf "%s\\n" "${GIT_USERNAME:-x-access-token}" ;;\n'
            '*Password*) printf "%s\\n" "$GIT_PASSWORD" ;;\n'
            '*) printf "\\n" ;;\n'
            "esac\n"
        )
    os.chmod(askpass.name, 0o700)
    return Path(askpass.name)


def build_git_auth_environment(askpass_path: Path, token: str) -> dict[str, str]:
    """Build an isolated Git authentication environment for a member token.

    The empty command-scope ``credential.helper`` value resets helpers inherited
    from system, global, and repository configuration. This prevents a local
    credential manager from replacing the member token or opening an interactive
    sign-in prompt.
    """
    config_index = int(os.getenv("GIT_CONFIG_COUNT", "0"))
    return {
        "GIT_ASKPASS": str(askpass_path),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_USERNAME": "x-access-token",
        "GIT_PASSWORD": token,
        "GIT_CONFIG_COUNT": str(config_index + 1),
        f"GIT_CONFIG_KEY_{config_index}": "credential.helper",
        f"GIT_CONFIG_VALUE_{config_index}": "",
    }
