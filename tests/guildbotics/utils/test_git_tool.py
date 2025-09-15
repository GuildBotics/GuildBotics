import logging
import sys
from pathlib import Path

import git

# Ensure repository root is importable when running pytest directly
repo_root = str(Path(__file__).resolve().parents[3])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

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
    tool, workspace, _ = _init_git_tool(tmp_path)

    # Repository should be cloned into workspace/<repo_name>
    assert tool.repo_path.exists()
    assert tool.repo.active_branch.name == "main"

    # Confirm repository-local user config is set
    with tool.repo.config_reader(config_level="repository") as cr:
        assert cr.get_value("user", "name") == "Tester"
        assert cr.get_value("user", "email") == "tester@example.com"


def test_checkout_branch_creates_new_branch_from_default(tmp_path: Path):
    tool, _, _ = _init_git_tool(tmp_path)

    base_commit = tool.repo.head.commit.hexsha
    tool.checkout_branch("feature/test-branch")

    assert tool.repo.active_branch.name == "feature/test-branch"
    assert tool.repo.head.commit.hexsha == base_commit


def test_commit_changes_commits_and_pushes(tmp_path: Path):
    tool, _, remote = _init_git_tool(tmp_path)

    # Create a new file to commit
    new_file = tool.repo_path / "new.txt"
    new_file.write_text("content\n", encoding="utf-8")

    sha = tool.commit_changes("add new file")
    assert isinstance(sha, str) and len(sha) > 0

    # Verify the bare remote received the new commit on 'main'
    remote_repo = git.Repo(remote)
    assert remote_repo.commit("main").hexsha == sha


def test_commit_changes_noop_when_clean(tmp_path: Path):
    tool, _, _ = _init_git_tool(tmp_path)

    sha = tool.commit_changes("no changes")
    assert sha is None


def test_get_diff_includes_status_and_diff(tmp_path: Path):
    tool, _, _ = _init_git_tool(tmp_path)

    # Modify tracked file without staging
    readme = tool.repo_path / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "more\n", encoding="utf-8")

    diff_output = tool.get_diff()
    # Should mention the file in status and include a diff header
    assert "README.md" in diff_output
    assert "diff --git" in diff_output or " M " in diff_output
