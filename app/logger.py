"""Tiny logging helper so logs are easy to read on Render."""
from __future__ import annotations

import logging
import sys
import threading


_HANDLER_MARK = "_agent_stream_handler"
_lock = threading.Lock()


def get_logger(name: str = "agent") -> logging.Logger:
    logger = logging.getLogger(name)
    with _lock:
        if not any(getattr(h, _HANDLER_MARK, False) for h in logger.handlers):
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            setattr(handler, _HANDLER_MARK, True)
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
    return logger
