"""Structured logging setup using structlog.

JSON logs in prod (MCP_ENV != dev), pretty console logs in dev.
Every log line automatically carries any contextvars that have been bound
(e.g. ``run_id``) so a single pipeline run is traceable end-to-end.

Usage
-----
    from pipeline.logging_config import setup_logging
    setup_logging(level="INFO", json_logs=False)

    import structlog
    log = structlog.get_logger(__name__)
    log.info("pipeline_started", prefix="raw/")

    # Bind run-scoped fields for the duration of a with-block:
    with structlog.contextvars.bound_contextvars(run_id="abc123"):
        log.info("document_read", key="raw/profile/recABC/cv.txt")
"""

from __future__ import annotations

import logging
import sys

import structlog


def setup_logging(level: str = "INFO", *, json_logs: bool = False) -> None:
    """Configure structlog and stdlib logging.

    Call once at process startup before any log statements. Safe to call
    multiple times (subsequent calls reconfigure).

    Parameters
    ----------
    level:
        Root log level string, e.g. "DEBUG", "INFO", "WARNING".
    json_logs:
        True → JSON renderer (production / Docker).
        False → ConsoleRenderer with colours (local dev / TTY).
    """
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if json_logs:
        # Production: machine-readable JSON, one object per line.
        processors: list[structlog.types.Processor] = [
            *shared_processors,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ]
    else:
        # Development: human-readable console output.
        processors = [
            *shared_processors,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ]

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )

    # Also configure stdlib so boto3/opensearch-py log at the right level.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stderr,
        level=getattr(logging, level.upper(), logging.INFO),
        force=True,
    )
    # Quiet chatty libraries.
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("opensearch").setLevel(logging.WARNING)
