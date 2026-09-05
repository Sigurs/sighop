"""Shared test setup.

`configure_logging` is global and caches its bound loggers, so a test that runs
`sighop.cli.main` binds structlog to whatever stream that command chose — under
pytest, a capture buffer that is closed when the test ends. Any later test whose
code logs through the module-level logger then dies on a closed file, which
makes failures depend on test order rather than on behaviour.

Rebinding to a throwaway buffer before every test contains that: production
logging configuration is exercised by the code that owns it, and tests that care
about log output pass their own recording logger.

The database fixtures live in `tests/dbfixtures.py` and are re-exported here so
every test module sees them. They skip when no test database is configured,
which is the condition the proposal set: no test may require Postgres to pass.
"""

from __future__ import annotations

import io

import pytest

from sighop.logging import configure_logging
from tests.dbfixtures import (  # noqa: F401 - re-exported as fixtures
    SKIP_REASON,
    configured_url,
    database,
    database_config,
    database_url,
    test_schema,
)


@pytest.fixture(autouse=True)
def _isolated_logging() -> None:
    configure_logging(stream=io.StringIO())


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip every `database`-marked test when no test database is configured.

    A skip rather than a failure, and applied at collection so the reason is
    reported once per test rather than discovered inside a fixture.
    """
    if configured_url() is not None:
        return
    skip = pytest.mark.skip(reason=SKIP_REASON)
    for item in items:
        if "database" in item.keywords:
            item.add_marker(skip)
