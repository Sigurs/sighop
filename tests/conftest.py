"""Shared test setup.

`configure_logging` is global and caches its bound loggers, so a test that runs
`sighop.cli.main` binds structlog to whatever stream that command chose — under
pytest, a capture buffer that is closed when the test ends. Any later test whose
code logs through the module-level logger then dies on a closed file, which
makes failures depend on test order rather than on behaviour.

Rebinding to a throwaway buffer before every test contains that: production
logging configuration is exercised by the code that owns it, and tests that care
about log output pass their own recording logger.
"""

from __future__ import annotations

import io

import pytest

from sighop.logging import configure_logging


@pytest.fixture(autouse=True)
def _isolated_logging() -> None:
    configure_logging(stream=io.StringIO())
