import logging
from pathlib import Path

import git
from git import GitCommandError


class GitTool:
    """
    A robust Git tool for managing Git operations within a specified workspace.
    """

    def __init__(
        self,
        workspace: Path,
        repo_url: str,
        logger: logging.Logger,
        user_name: str,
        user_email: str,
        default_branch: str = "main",
    ):
        """
        Initialize the GitTool.

        Args:
            workspace (Path): Directory for Git operations.
            repo_url (str): URL of the Git repository.
            logger (logging.Logger): Logger for recording operations.
            user_name (str, optional): Git user name for commits. Defaults to None.
            user_email (str, optional): Git user email for commits. Defaults to None.
            default_branch (str): The main branch name to base new branches on (e.g., 'main' or 'master').
        """
        self.workspace = workspace
        self.repo_url = repo_url
        self.logger = logger
        self.user_name = user_name
        self.user_email = user_email
        self.default_branch = default_branch

        # Ensure workspace exists
        self.workspace.mkdir(parents=True, exist_ok=True)

        # Extract repository name safely
        repo_name = Path(repo_url.rstrip("/")).stem
        self.repo_path = self.workspace / repo_name

        try:
            if not self.repo_path.exists():
                self.logger.info(f"Cloning {repo_url} into {self.repo_path}")
                self.repo = git.Repo.clone_from(repo_url, self.repo_path)
            else:
                self.repo = git.Repo(self.repo_path)
        except GitCommandError as e:
            self.logger.error(f"Git operation failed: {e}")
            raise

        # Use a local config writer to scope to this repo only
        with self.repo.config_writer(config_level="repository") as cw:
            cw.set_value("user", "name", self.user_name)
            cw.set_value("user", "email", self.user_email)

        # Ensure default branch exists locally
        try:
            self.logger.info(f"Checking out default branch '{self.default_branch}'")
            self.checkout_branch(self.default_branch)
            self.logger.info(f"Pulling latest changes on '{self.default_branch}'")
            origin = self.repo.remotes.origin
            origin.pull(self.default_branch)
        except GitCommandError as e:
            self.logger.error(
                f"Failed to checkout or update default branch '{self.default_branch}': {e}"
            )
            raise

    def checkout_branch(self, branch_name: str):
        """
        Create a new local branch from the default branch, or switch to it if it already exists.
        If the branch exists remotely, set the upstream tracking.

        Args:
            branch_name (str): The branch name to create or switch to.
        """
        current_branch = self.repo.active_branch.name
        if current_branch == branch_name:
            self.logger.info(f"Already on branch '{branch_name}'. No action taken.")
            return

        try:
            origin = self.repo.remotes.origin
            origin.fetch()

            # Remove untracked working tree files to avoid checkout conflicts
            untracked = self.repo.untracked_files
            if untracked:
                self.logger.info(
                    f"Removing untracked files before checkout: {untracked}"
                )
                # -f: force, -d: remove untracked directories
                self.repo.git.clean("-fd")

            # Revert local modifications on tracked files to avoid checkout conflicts
            if self.repo.is_dirty(untracked_files=False):
                self.logger.info("Reverting local changes before checkout")
                self.repo.git.reset("--hard")

            local_branches = {b.name for b in self.repo.branches}
            if branch_name in local_branches:
                # If branch exists locally: checkout
                self.logger.info(
                    f"Branch '{branch_name}' exists locally. Checking out."
                )
                self.repo.git.checkout(branch_name)
                # If exists remotely, pull and set upstream
                remote_branches = {ref.remote_head for ref in origin.refs}
                if branch_name in remote_branches:
                    self.logger.info(
                        f"Setting upstream and pulling '{branch_name}' from origin."
                    )
                    self.repo.git.branch(
                        "--set-upstream-to", f"origin/{branch_name}", branch_name
                    )
                    try:
                        origin.pull(branch_name)
                    except GitCommandError as e:
                        self.logger.warning(
                            f"Failed to pull branch '{branch_name}' from origin: {e}"
                        )
                        self.repo.git.checkout(self.default_branch)
                        self.repo.git.branch("-D", branch_name)
                        origin.fetch(f"{branch_name}:{branch_name}")
                        self.repo.git.checkout(branch_name)
            else:
                # Create new branch locally from default
                self.logger.info(
                    f"Creating and checking out new branch '{branch_name}' from '{self.default_branch}'."
                )
                self.repo.git.checkout("-b", branch_name)
        except GitCommandError as e:
            self.logger.error(
                f"Failed to create or checkout branch '{branch_name}': {e}"
            )
            raise

    def commit_changes(self, message: str) -> str | None:
        """
        Commit and push changes to the current branch.

        Args:
            message (str): The commit message.

        Returns:
            str | None: The commit SHA if a commit was made, otherwise None.
        """
        try:
            commit_sha = None
            # Check for any changes (including untracked files)
            if self.repo.is_dirty(untracked_files=True):
                self.logger.info("Staging all changes.")
                self.repo.git.add(A=True)
                self.logger.info(f"Committing changes with message: '{message}'.")
                commit_obj = self.repo.index.commit(message)
                commit_sha = commit_obj.hexsha
            else:
                self.logger.info("No changes to commit.")

            # Determine if we need to push:
            origin = self.repo.remotes.origin
            origin.fetch()
            current_branch = self.repo.active_branch.name

            # Try to list commits that are on local but not on remote.
            try:
                commits_ahead = list(
                    self.repo.iter_commits(f"origin/{current_branch}..{current_branch}")
                )
                # Push if there are commits ahead
                need_push = bool(commits_ahead)
            except GitCommandError:
                # Remote branch does not exist => needs push
                need_push = True

            if need_push:
                self.logger.info(f"Pushing branch '{current_branch}' to remote.")
                origin.push(current_branch)
            return commit_sha
        except GitCommandError as e:
            self.logger.error(f"Failed to commit or push changes: {e}")
            raise

    def get_diff(self) -> str:
        """
        Get the current git diff as a string.

        Returns:
            str: The output of 'git diff' (working tree changes).
        """
        try:
            # Get the diff of the working tree (unstaged and staged changes)
            status = self.repo.git.status("--short")
            diff = self.repo.git.diff()
            return f"{status}\n{diff}" if diff else status
        except GitCommandError as e:
            self.logger.error(f"Failed to get git diff: {e}")
            raise
