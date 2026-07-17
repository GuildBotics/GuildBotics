import logging
import os


def get_logger() -> logging.Logger:
    logger = logging.getLogger("guildbotics")
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [@%(threadName)s] %(message)s"
    )

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        sh.setLevel(logger.level)
        logger.addHandler(sh)

    return logger
