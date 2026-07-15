"""
Tests for ipycam.logging_config.configure_logging().

IPyCam attaches a NullHandler to the "ipycam" logger by default (see
ipycam/__init__.py) so importing the library is silent; configure_logging()
is what the bundled CLI calls to get console output. These tests attach (and
clean up) a handler on the real "ipycam" logger, so each test removes what it
added to avoid leaking handlers/levels across the test session.
"""

import io
import logging

import pytest

from ipycam.logging_config import configure_logging


@pytest.fixture
def clean_ipycam_logger():
    """Snapshot and restore the 'ipycam' logger's handlers/level."""
    logger = logging.getLogger("ipycam")
    original_handlers = list(logger.handlers)
    original_level = logger.level
    yield logger
    logger.handlers = original_handlers
    logger.setLevel(original_level)


def test_configure_logging_attaches_stream_handler(clean_ipycam_logger):
    handler = configure_logging(level=logging.WARNING)
    try:
        assert isinstance(handler, logging.StreamHandler)
        assert handler in clean_ipycam_logger.handlers
        assert clean_ipycam_logger.level == logging.WARNING
    finally:
        clean_ipycam_logger.removeHandler(handler)


def test_configure_logging_defaults_to_info_level(clean_ipycam_logger):
    handler = configure_logging()
    try:
        assert clean_ipycam_logger.level == logging.INFO
    finally:
        clean_ipycam_logger.removeHandler(handler)


def test_configure_logging_writes_to_provided_stream(clean_ipycam_logger):
    buf = io.StringIO()
    handler = configure_logging(level=logging.DEBUG, stream=buf)
    try:
        logging.getLogger("ipycam.somemodule").debug("hello from test")
        output = buf.getvalue()
        assert "hello from test" in output
        assert "ipycam.somemodule" in output
    finally:
        clean_ipycam_logger.removeHandler(handler)


def test_configure_logging_returns_a_new_handler_each_call(clean_ipycam_logger):
    handler1 = configure_logging()
    handler2 = configure_logging()
    try:
        assert handler1 is not handler2
        assert handler1 in clean_ipycam_logger.handlers
        assert handler2 in clean_ipycam_logger.handlers
    finally:
        clean_ipycam_logger.removeHandler(handler1)
        clean_ipycam_logger.removeHandler(handler2)
