import logging
import os
import threading
from datetime import datetime
from pathlib import Path

file_handlers: dict[str, logging.FileHandler] = {}


def get_log_output_dir(name: str = "") -> Path | None:
    output_dir = os.getenv("LOG_OUTPUT_DIR", "")
    if output_dir:
        dir_name = name if name else threading.current_thread().name
        return Path(output_dir) / dir_name
    return None


def get_file_handler() -> logging.FileHandler | None:
    name = threading.current_thread().name
    if name in file_handlers:
        return file_handlers[name]

    log_output_dir = get_log_output_dir(name)
    if log_output_dir:
        log_output_dir.mkdir(parents=True, exist_ok=True)
        current_time = datetime.now().strftime("%Y-%m-%d_%H%M")
        file_handler = logging.FileHandler(
            log_output_dir / f"guildbotics_{current_time}.log"
        )
        file_handlers[name] = file_handler
        return file_handler
    return None


def get_logger() -> logging.Logger:
    logger = logging.getLogger(f"guildbotics")
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] [@%(threadName)s] %(message)s"
    )

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        sh = logging.StreamHandler()
        sh.setFormatter(formatter)
        sh.setLevel(logger.level)
        logger.addHandler(sh)

    if threading.current_thread() is not threading.main_thread():
        fh = get_file_handler()
        if fh and not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
            fh.setFormatter(formatter)
            fh.setLevel(logger.level)
            logger.addHandler(fh)

    return logger
