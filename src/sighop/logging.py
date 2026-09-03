"""structlog setup and the wide-event helper.

Per DESIGN.md §9: JSON output, one context-rich wide event per unit of work,
two levels (info/error), no unstructured strings. Every event carries
service/version/commit_hash/instance_id/event_type/duration_ms/outcome.
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
import uuid
from collections.abc import Iterator
from importlib.metadata import PackageNotFoundError, version as pkg_version
from typing import IO

import structlog

_INSTANCE_ID = os.environ.get("SIGHOP_INSTANCE_ID", uuid.uuid4().hex[:12])


def _package_version() -> str:
    try:
        return pkg_version("sighop")
    except PackageNotFoundError:
        return "0.0.0-dev"


class _Tee:
    """Writes each line to multiple streams (e.g. stdout and a log file)."""

    def __init__(self, *streams: IO[str]) -> None:
        self._streams = streams

    def write(self, data: str) -> None:
        for stream in self._streams:
            stream.write(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def configure_logging(log_file: IO[str] | None = None) -> None:
    """Configure JSON wide-event logging to stdout, and additionally to
    `log_file` when given (e.g. so an unattended run's logs survive
    alongside its capture file).
    """
    output: IO[str] = _Tee(sys.stdout, log_file) if log_file is not None else sys.stdout
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.add_log_level,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
        logger_factory=structlog.PrintLoggerFactory(file=output),
        cache_logger_on_first_use=True,
    )


def get_logger(**initial_context: object) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger().bind(
        service="sighop",
        version=_package_version(),
        commit_hash=os.environ.get("SIGHOP_COMMIT_HASH", "unknown"),
        instance_id=_INSTANCE_ID,
        **initial_context,
    )


@contextlib.contextmanager
def wide_event(
    logger: structlog.stdlib.BoundLogger, event_type: str, **context: object
) -> Iterator[dict[str, object]]:
    """Time and log one unit of work as a single wide event.

    Extra fields can be added to the yielded dict up until the event is
    logged on exit. Logs at error level with outcome="error" if the block
    raises; the exception propagates either way.
    """
    fields: dict[str, object] = dict(context)
    start = time.monotonic()
    try:
        yield fields
    except Exception:
        duration_ms = round((time.monotonic() - start) * 1000, 3)
        logger.error(event_type, duration_ms=duration_ms, outcome="error", **fields)
        raise
    else:
        duration_ms = round((time.monotonic() - start) * 1000, 3)
        outcome = fields.pop("outcome", "success")
        logger.info(event_type, duration_ms=duration_ms, outcome=outcome, **fields)
