#!/usr/bin/env python3
"""Logging configuration helper for IPyCam.

IPyCam follows standard library-logging practice: every module logs through
``logging.getLogger(__name__)`` and the package attaches a ``NullHandler`` to
the ``ipycam`` logger (see ``ipycam/__init__.py``) so simply importing the
library produces no console output. Applications that embed IPyCam configure
logging however they like; the bundled CLI (``python -m ipycam``) calls
``configure_logging()`` below to get sensible console output.
"""

import logging
import sys
from typing import Optional, TextIO


def configure_logging(level: int = logging.INFO, stream: Optional[TextIO] = None) -> logging.Handler:
    """Attach a console StreamHandler to the ``ipycam`` logger tree.

    Args:
        level: Minimum log level to emit (default ``logging.INFO``).
        stream: Stream to write log records to (default ``sys.stderr``).

    Returns:
        The ``logging.Handler`` that was attached, in case the caller wants
        to remove or reconfigure it later.
    """
    logger = logging.getLogger("ipycam")
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    return handler
