import logging
import os
from datetime import datetime as real_datetime

from guildbotics.utils import log_utils


def test_get_file_handler_without_env_returns_none(monkeypatch):
    """When LOG_OUTPUT_DIR is unset or empty, returns None."""
    # Ensure the environment variable is not set
    monkeypatch.delenv("LOG_OUTPUT_DIR", raising=False)

    handler = log_utils.get_file_handler()

    assert handler is None


def test_get_file_handler_with_env_creates_handler_and_path(monkeypatch, tmp_path):
    """When LOG_OUTPUT_DIR is set, returns FileHandler with expected path format."""

    # Use a fixed timestamp to make the filename deterministic
    class FixedDateTime:
        @classmethod
        def now(cls):
            return real_datetime(2024, 1, 2, 3, 4)

    logs_dir = tmp_path / "logs"
    monkeypatch.setenv("LOG_OUTPUT_DIR", str(logs_dir))
    # Patch the module's datetime to our fixed version
    monkeypatch.setattr(log_utils, "datetime", FixedDateTime, raising=False)

    handler = log_utils.get_file_handler()

    try:
        assert isinstance(handler, logging.FileHandler)
        expected = logs_dir / "MainThread" / "guildbotics_2024-01-02_0304.log"
        assert handler.baseFilename == str(expected)
        # Directory should be created and the file opened (created)
        assert logs_dir.exists()
        assert os.path.isfile(expected)
    finally:
        # Close the handler to release the file on all platforms
        if handler:
            handler.close()
