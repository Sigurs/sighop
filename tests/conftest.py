"""Shared test setup.

`configure_logging` is global and caches its bound loggers, so a test that runs
`sighop.boot.main` binds structlog to whatever stream the entry point chose — under
pytest, a capture buffer that is closed when the test ends. Any later test whose
code logs through the module-level logger then dies on a closed file, which
makes failures depend on test order rather than on behaviour.

Rebinding to a throwaway buffer before every test contains that: production
logging configuration is exercised by the code that owns it, and tests that care
about log output pass their own recording logger.

The database fixtures live in `tests/dbfixtures.py` and are re-exported here so
every test module sees them. The database is required: a run that configures
none fails there rather than skipping, so a passing suite is a suite that
exercised persistence.
"""

from __future__ import annotations

import io

import pytest

from sighop.logging import configure_logging
from tests.dbfixtures import (  # noqa: F401 - re-exported as fixtures
    NO_DATABASE,
    configured_url,
    database,
    database_config,
    database_url,
    default_persistence,
    fresh_persistence,
    test_schema,
)


def pytest_configure(config: pytest.Config) -> None:
    """Refuse the run once, before collection, when no database is configured.

    The fixture refuses too, but it refuses per test, and a developer who has
    not set the variable would read the same paragraph several hundred times
    before reaching the summary. Stopping here states it once.
    """
    if configured_url() is None:
        pytest.exit(NO_DATABASE, returncode=pytest.ExitCode.USAGE_ERROR)


@pytest.fixture(autouse=True)
def _isolated_logging() -> None:
    configure_logging(stream=io.StringIO())
