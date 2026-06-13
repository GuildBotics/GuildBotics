import logging
from pathlib import Path

import git

from guildbotics.utils.git_tool import GitTool


def _setup_bare_remote_with_main(tmp_path: Path) -> Path:
    """Create a bare remote repository with an initial commit on 'main'.

    This helper creates a working repo to author the initial commit, pushes it
    to a newly created bare repository, and returns the bare repo path which can
    be used as the clone URL.

    Args:
        tmp_path: Temporary directory provided by pytest.

    Returns:
        Path: Filesystem path to the bare remote repository.
    """
    remote_bare = tmp_path / "remote.git"
    git.Repo.init(remote_bare, bare=True)

    work = tmp_path / "work"
    repo = git.Repo.init(work)

    # Configure identity for making commits in the seed repository
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Seed User")
        cw.set_value("user", "email", "seed@example.com")

    (work / "README.md").write_text("hello\n", encoding="utf-8")
    repo.git.add(A=True)
    repo.index.commit("initial commit")

    # Ensure the 'main' branch exists and is current
    try:
        repo.git.checkout("-b", "main")
    except git.GitCommandError:
        # If the branch already exists for some reason, just ensure we are on it
        repo.git.checkout("main")

    # Push to the bare remote
    repo.create_remote("origin", str(remote_bare))
    repo.git.push("--set-upstream", "origin", "HEAD:main")

    return remote_bare


def _logger() -> logging.Logger:
    """Create a quiet logger for tests."""
    logger = logging.getLogger("git_tool_test")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        logger.addHandler(logging.NullHandler())
    return logger


def _init_git_tool(tmp_path: Path) -> tuple[GitTool, Path, Path]:
    """Initialize GitTool cloned from a local bare remote.

    Returns the GitTool, the workspace path, and the remote path.
    """
    remote = _setup_bare_remote_with_main(tmp_path)
    workspace = tmp_path / "workspace"
    tool = GitTool(
        workspace=workspace,
        repo_url=str(remote),
        logger=_logger(),
        user_name="Tester",
        user_email="tester@example.com",
        default_branch="main",
    )
    return tool, workspace, remote


def test_init_clones_and_configures_user(tmp_path: Path):
    tool, _, _ = _init_git_tool(tmp_path)

    # Repository should be cloned into workspace/<repo_name>
    assert tool.repo_path.exists()
    assert tool.repo.active_branch.name == "main"

    # Confirm repository-local user config is set
    with tool.repo.config_reader(config_level="repository") as cr:
        assert cr.get_value("user", "name") == "Tester"
        assert cr.get_value("user", "email") == "tester@example.com"


def test_init_with_auth_token_uses_temporary_askpass(tmp_path: Path):
    remote = _setup_bare_remote_with_main(tmp_path)
    workspace = tmp_path / "workspace"
    tool = GitTool(
        workspace=workspace,
        repo_url=str(remote),
        logger=_logger(),
        user_name="Tester",
        user_email="tester@example.com",
        default_branch="main",
        auth_token="secret-token",
    )

    assert tool.repo_path.exists()
    assert tool._askpass_path is not None
    assert tool._askpass_path.exists()

    askpass_path = tool._askpass_path
    tool.close()

    assert not askpass_path.exists()


def test_checkout_branch_creates_new_branch_from_default(tmp_path: Path):
    tool, _, _ = _init_git_tool(tmp_path)

    base_commit = tool.repo.head.commit.hexsha
    tool.checkout_branch("feature/test-branch")

    assert tool.repo.active_branch.name == "feature/test-branch"
    assert tool.repo.head.commit.hexsha == base_commit


def test_checkout_branch_uses_remote_branch_when_available(tmp_path: Path):
    tool, _, remote = _init_git_tool(tmp_path)

    seed = tmp_path / "seed"
    seed_repo = git.Repo.clone_from(str(remote), seed)
    with seed_repo.config_writer() as cw:
        cw.set_value("user", "name", "Seed User")
        cw.set_value("user", "email", "seed@example.com")
    seed_repo.git.checkout("-b", "feature/remote-only")
    (seed / "remote.txt").write_text("remote branch content\n", encoding="utf-8")
    seed_repo.git.add(A=True)
    remote_commit = seed_repo.index.commit("remote branch commit").hexsha
    seed_repo.git.push("--set-upstream", "origin", "feature/remote-only")

    tool.checkout_branch("feature/remote-only")

    assert tool.repo.active_branch.name == "feature/remote-only"
    assert tool.repo.head.commit.hexsha == remote_commit
    assert (tool.repo_path / "remote.txt").read_text(encoding="utf-8") == (
        "remote branch content\n"
    )
