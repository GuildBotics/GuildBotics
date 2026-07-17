import logging

from guildbotics.utils import log_utils


def test_get_logger_uses_stderr_handler_without_file_output(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logger = logging.getLogger("guildbotics")
    logger.handlers.clear()

    result = log_utils.get_logger()

    assert result.level == logging.DEBUG
    assert len(result.handlers) == 1
    assert type(result.handlers[0]) is logging.StreamHandler
