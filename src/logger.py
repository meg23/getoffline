import logging
import os
from pathlib import Path

_LOGGER_NAME = "getoffline"
_LOG_FILE = Path(os.path.expanduser("~/getoffline/getoffline.log"))

# Ensure parent directory exists before configuring logging
_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

_logging_configured = False


def _configure_logging() -> logging.Logger:
    """Configure a shared application logger."""
    global _logging_configured

    logger = logging.getLogger(_LOGGER_NAME)
    if _logging_configured:
        return logger

    logger.setLevel(logging.INFO)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    file_handler = logging.FileHandler(_LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    _logging_configured = True
    return logger


log = _configure_logging()
