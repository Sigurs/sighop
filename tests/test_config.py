"""Configuration from the environment (milestone 5, design D1/D6/D9/D16).

Every test here builds its configuration from an explicit mapping rather than
from `os.environ`: a suite that read the real environment would pass or fail
depending on whether the developer running it happened to have a database
configured, which is exactly the property the proposal forbids.
"""

from __future__ import annotations

import base64

import pytest

from sighop.config import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_OVERFLOW,
    DEFAULT_POOL_SIZE,
    DEFAULT_POOL_TIMEOUT,
    DEFAULT_STATEMENT_TIMEOUT,
    REQUIRED_DRIVER,
    ROLE_CONNECTION_LIMIT,
    SECRET_KEY_SIZE,
    Config,
    ConfigError,
    DatabaseConfig,
    generate_secret_key,
    parse_secret_key,
)

PASSWORD = "2B9dQCPEzz5"
URL = f"postgresql+asyncpg://apps_sighop-dev:{PASSWORD}@172.20.4.20:30432/apps_sighop-dev"


# --- 1.2 Reading the environment -------------------------------------------


def test_config_reads_an_explicit_mapping_not_the_process_environment() -> None:
    secret = generate_secret_key()
    config = Config.from_environment({"DATABASE_URL": URL, "SIGHOP_SECRET_KEY": secret})
    assert config.persistent
    assert config.database is not None
    assert config.database.database == "apps_sighop-dev"
    assert config.secret_key == secret


def test_no_database_url_is_not_an_error() -> None:
    config = Config.from_environment({})
    assert config.database is None
    assert not config.persistent


def test_command_line_url_overrides_the_environment() -> None:
    other = "postgresql+asyncpg://role:pw@elsewhere:5432/other"
    config = Config.from_environment({"DATABASE_URL": URL}, database_url=other)
    assert config.database is not None
    assert config.database.host == "elsewhere"


# --- 1.3 URL validation ----------------------------------------------------


def test_asyncpg_url_is_accepted() -> None:
    assert DatabaseConfig(url=URL).sqlalchemy_url.drivername == REQUIRED_DRIVER


def test_psycopg_url_is_refused_naming_the_driver() -> None:
    with pytest.raises(ConfigError) as excinfo:
        DatabaseConfig(url=URL.replace("+asyncpg", "+psycopg"))
    assert "psycopg" in str(excinfo.value)
    assert REQUIRED_DRIVER in str(excinfo.value)


def test_libpq_query_parameter_is_refused_naming_what_is_unsupported() -> None:
    with pytest.raises(ConfigError) as excinfo:
        DatabaseConfig(url=f"{URL}?sslmode=require")
    assert "sslmode" in str(excinfo.value)


def test_unparseable_url_names_the_variable_and_not_the_value() -> None:
    with pytest.raises(ConfigError) as excinfo:
        DatabaseConfig(url=f"not a url at all {PASSWORD}")
    assert "DATABASE_URL" in str(excinfo.value)
    assert PASSWORD not in str(excinfo.value)


# --- 1.4 Redaction ---------------------------------------------------------


def test_the_password_appears_in_no_rendering_of_the_config() -> None:
    config = DatabaseConfig(url=URL)
    renderings = [
        repr(config),
        str(config),
        config.redacted_url,
        repr(config.as_json()),
        repr(Config(database=config, secret_key="a-secret")),
    ]
    for rendering in renderings:
        assert PASSWORD not in rendering, rendering
    assert "apps_sighop-dev" in config.redacted_url


def test_the_secret_key_appears_in_no_rendering_of_the_config() -> None:
    secret = generate_secret_key()
    config = Config(database=DatabaseConfig(url=URL), secret_key=secret)
    assert secret not in repr(config)
    assert secret not in repr(config.as_json())
    assert config.as_json()["secret_key_present"] is True


# --- 1.5 Pool sizing (design D6) -------------------------------------------


def test_pool_defaults_sit_well_under_the_measured_role_limit() -> None:
    config = DatabaseConfig(url=URL)
    assert config.pool_size == DEFAULT_POOL_SIZE == 5
    assert config.max_overflow == DEFAULT_MAX_OVERFLOW == 5
    assert config.pool_timeout == DEFAULT_POOL_TIMEOUT == 10.0
    assert config.pool_pre_ping is True
    assert config.max_connections == 10
    # Room for a second instance, an Alembic run, a psql session and the suite.
    assert config.max_connections * 2 < ROLE_CONNECTION_LIMIT


def test_pool_settings_are_overridable() -> None:
    config = DatabaseConfig(url=URL, pool_size=2, max_overflow=1, pool_timeout=3.0)
    assert (config.pool_size, config.max_overflow, config.pool_timeout) == (2, 1, 3.0)


def test_an_unbounded_pool_timeout_is_refused() -> None:
    with pytest.raises(ConfigError, match="pool_timeout"):
        DatabaseConfig(url=URL, pool_timeout=0)


# --- 1.6 Time bounds (design D16) ------------------------------------------


def test_connect_and_statement_bounds_reach_the_driver_and_are_not_its_defaults() -> None:
    config = DatabaseConfig(url=URL)
    assert config.connect_timeout == DEFAULT_CONNECT_TIMEOUT == 5.0
    assert config.statement_timeout == DEFAULT_STATEMENT_TIMEOUT == 5.0

    args = config.connect_args()
    # asyncpg's own connect default is 60 s; leaving it there is what design D16
    # exists to prevent against a host that blackholes rather than refuses.
    assert args["timeout"] == 5.0
    assert args["command_timeout"] == 5.0
    assert args["server_settings"]["statement_timeout"] == "5000"


def test_time_bounds_are_overridable_and_reach_the_driver() -> None:
    config = DatabaseConfig(url=URL, connect_timeout=1.5, statement_timeout=2.0)
    args = config.connect_args()
    assert args["timeout"] == 1.5
    assert args["command_timeout"] == 2.0
    assert args["server_settings"]["statement_timeout"] == "2000"


@pytest.mark.parametrize("bound", ["connect_timeout", "statement_timeout"])
def test_a_non_positive_time_bound_is_refused(bound: str) -> None:
    overrides: dict[str, float] = {bound: 0.0}
    with pytest.raises(ConfigError, match=bound):
        DatabaseConfig(url=URL, **overrides)  # type: ignore[arg-type]


# --- 4.2 The encryption secret ---------------------------------------------


def test_a_generated_secret_decodes_to_thirty_two_bytes() -> None:
    assert len(parse_secret_key(generate_secret_key())) == SECRET_KEY_SIZE


def test_a_missing_secret_says_it_is_missing() -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_secret_key(None)
    assert "not set" in str(excinfo.value)
    assert "SIGHOP_SECRET_KEY" in str(excinfo.value)


def test_a_secret_that_is_not_base64_says_so() -> None:
    with pytest.raises(ConfigError) as excinfo:
        parse_secret_key("this is not base64!!")
    assert "base64" in str(excinfo.value)


def test_a_secret_of_the_wrong_length_says_so_and_is_not_reshaped() -> None:
    short = base64.b64encode(b"\x01" * 16).decode()
    with pytest.raises(ConfigError) as excinfo:
        parse_secret_key(short)
    assert "16 bytes" in str(excinfo.value)
    assert "32" in str(excinfo.value)
