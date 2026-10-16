"""Logging: `structlog` JSON (or console) with a mandatory redactor."""

import logging
import sys

import structlog

from homing.redact import REDACTOR


def configure_logging(*, json_logs: bool = False, quiet: bool = False) -> None:
    """Configure structlog.

    Redaction runs last before rendering, so nothing bypasses it.
    """
    renderer: structlog.typing.Processor = (
        structlog.processors.JSONRenderer()
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,
            REDACTOR.structlog_processor,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.WARNING if quiet else logging.INFO,
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=False,
    )
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
