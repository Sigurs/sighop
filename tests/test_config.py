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
    DEDUP_MAX_ENTRIES_VARIABLE,
    DEDUP_TTL_VARIABLE,
    DEFAULT_CEILING_FRACTION,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_MAX_ENTRIES,
    DEFAULT_MAX_OVERFLOW,
    DEFAULT_PATH_HASH_SIZE,
    DEFAULT_POOL_SIZE,
    DEFAULT_POOL_TIMEOUT,
    DEFAULT_RADIO_PRESET,
    DEFAULT_SHUTDOWN_BUDGET,
    DEFAULT_STATEMENT_TIMEOUT,
    DEFAULT_STATUS_INTERVAL_SECONDS,
    DEFAULT_TTL_SECONDS,
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    DUTY_CYCLE_CEILING_VARIABLE,
    ENABLE_TRANSMIT_VARIABLE,
    MODEM_VARIABLE,
    PATH_HASH_SIZE_VARIABLE,
    RADIO_PRESET_VARIABLE,
    REQUIRED_DRIVER,
    ROLE_CONNECTION_LIMIT,
    SECRET_KEY_COMMAND,
    SECRET_KEY_SIZE,
    SECRET_KEY_VARIABLE,
    STATUS_INTERVAL_VARIABLE,
    WEB_PORT_VARIABLE,
    Config,
    ConfigError,
    DatabaseConfig,
    check_environment,
    generate_secret_key,
    parse_path_hash_size,
    parse_secret_key,
)

PASSWORD = "not-a-real-password-9f3c"
URL = f"postgresql+asyncpg://apps_sighop-dev:{PASSWORD}@172.20.4.20:30432/apps_sighop-dev"


# --- 1.2 Reading the environment -------------------------------------------


def test_config_reads_an_explicit_mapping_not_the_process_environment() -> None:
    secret = generate_secret_key()
    config = Config.from_environment({"DATABASE_URL": URL, "SIGHOP_SECRET_KEY": secret})
    assert config.database.database == "apps_sighop-dev"
    assert config.secret_key == secret


def test_no_database_url_is_refused() -> None:
    """The database is required, so there is no configuration without one."""
    with pytest.raises(ConfigError) as excinfo:
        Config.from_environment({})
    assert "DATABASE_URL" in str(excinfo.value)


def test_the_environment_is_the_only_source_of_the_url() -> None:
    """There is no command line, so nothing overrides the variable.

    Asserted against the signature rather than against a convention: a
    `database_url=` keyword is what a reintroduced command line would reach for
    first, and its absence is what makes "the environment is the only
    configuration surface" (design D2) checkable.
    """
    import inspect

    parameters = inspect.signature(Config.from_environment).parameters
    assert list(parameters) == ["environ"]


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


def test_the_shutdown_budget_is_bounded_below_one_operation_and_overridable() -> None:
    config = DatabaseConfig(url=URL)
    assert config.shutdown_budget == DEFAULT_SHUTDOWN_BUDGET == 5.0
    # Below one operation_timeout on purpose (design D4): a hung database has
    # its batch cancelled and counted rather than being waited out.
    assert config.shutdown_budget < config.connect_timeout + config.statement_timeout
    assert DatabaseConfig(url=URL, shutdown_budget=1.5).shutdown_budget == 1.5
    assert DatabaseConfig(url=URL).as_json()["shutdown_budget"] == 5.0


@pytest.mark.parametrize("bound", ["connect_timeout", "statement_timeout", "shutdown_budget"])
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


# --- Path hash size (add-path-hash-size-setting) ---------------------------


@pytest.mark.parametrize("value", [None, "", "   "])
def test_an_unset_or_blank_path_hash_size_is_the_default_of_three(value: str | None) -> None:
    assert DEFAULT_PATH_HASH_SIZE == 3
    assert parse_path_hash_size(value) == 3


@pytest.mark.parametrize(("value", "expected"), [("1", 1), ("2", 2), ("3", 3), (" 2 ", 2)])
def test_a_supported_path_hash_size_is_accepted(value: str, expected: int) -> None:
    assert parse_path_hash_size(value) == expected


@pytest.mark.parametrize("value", ["0", "4", "three", "-1"])
def test_an_unsupported_path_hash_size_is_refused_not_clamped(value: str) -> None:
    with pytest.raises(ConfigError) as caught:
        parse_path_hash_size(value)
    message = str(caught.value)
    assert PATH_HASH_SIZE_VARIABLE in message
    assert repr(value) in message
    assert "1, 2, 3" in message


def test_the_path_hash_size_is_read_from_the_environment() -> None:
    config = Config.from_environment({"DATABASE_URL": URL, PATH_HASH_SIZE_VARIABLE: "2"})
    assert config.path_hash_size() == (2, True)
    assert config.as_json()["path_hash_size"] == "2"


@pytest.mark.parametrize("raw", ["", None])
def test_an_unset_path_hash_size_is_the_default_and_not_from_the_environment(
    raw: str | None,
) -> None:
    environ = {"DATABASE_URL": URL}
    if raw is not None:
        environ[PATH_HASH_SIZE_VARIABLE] = raw
    assert Config.from_environment(environ).path_hash_size() == (3, False)


def test_an_invalid_path_hash_size_fails_only_where_it_is_used() -> None:
    config = Config.from_environment({"DATABASE_URL": URL, PATH_HASH_SIZE_VARIABLE: "4"})
    with pytest.raises(ConfigError, match=PATH_HASH_SIZE_VARIABLE):
        config.path_hash_size()


# --- Every run setting is an environment variable (design D2) ---------------
#
# There is no command line, so each of these is the only way to turn the thing
# it turns. The tests come in threes: the default when unset, the value when
# set, and the refusal when the value is one the setting cannot take.


def environment(**overrides: str) -> dict[str, str]:
    """A minimal usable environment, plus whatever the test is about."""
    return {"DATABASE_URL": URL, "SIGHOP_SECRET_KEY": generate_secret_key(), **overrides}


def test_every_run_setting_has_a_default_that_needs_no_variable() -> None:
    config = Config.from_environment(environment())
    assert config.radio_preset == DEFAULT_RADIO_PRESET
    assert config.transmit_enabled is False
    assert config.ceiling_fraction == DEFAULT_CEILING_FRACTION
    assert config.status_interval == DEFAULT_STATUS_INTERVAL_SECONDS
    assert config.dedup_ttl_seconds == DEFAULT_TTL_SECONDS
    assert config.dedup_max_entries == DEFAULT_MAX_ENTRIES
    assert config.web_host == DEFAULT_WEB_HOST
    assert config.web_port == DEFAULT_WEB_PORT
    assert config.web_allowed_hosts == ()
    assert config.modem is None
    assert config.capture_file is None
    assert config.log_file is None


def test_each_setting_is_read_from_its_own_variable() -> None:
    config = Config.from_environment(
        environment(
            SIGHOP_MODEM="/dev/serial/by-id/usb-modem",
            SIGHOP_RADIO_PRESET="eu868-narrow",
            SIGHOP_ENABLE_TRANSMIT="1",
            SIGHOP_DUTY_CYCLE_CEILING="0.05",
            SIGHOP_STATUS_INTERVAL="30",
            SIGHOP_DEDUP_TTL="120",
            SIGHOP_DEDUP_MAX_ENTRIES="512",
            SIGHOP_CAPTURE_FILE="/tmp/capture.jsonl",
            SIGHOP_LOG_FILE="/tmp/sighop.log",
            SIGHOP_WEB_HOST="0.0.0.0",
            SIGHOP_WEB_PORT="9000",
            SIGHOP_WEB_ALLOWED_HOSTS="localhost:9000, panel.example:443",
        )
    )
    assert config.modem == "/dev/serial/by-id/usb-modem"
    assert config.transmit_enabled is True
    assert config.ceiling_fraction == 0.05
    assert config.status_interval == 30.0
    assert config.dedup_ttl_seconds == 120.0
    assert config.dedup_max_entries == 512
    assert config.capture_file == "/tmp/capture.jsonl"
    assert config.log_file == "/tmp/sighop.log"
    assert config.web_host == "0.0.0.0"
    assert config.web_port == 9000
    assert config.web_allowed_hosts == ("localhost:9000", "panel.example:443")


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_a_flag_is_true_when_it_says_so(value: str) -> None:
    assert Config.from_environment(environment(SIGHOP_ENABLE_TRANSMIT=value)).transmit_enabled


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_a_flag_written_out_as_false_is_false(value: str) -> None:
    """`SIGHOP_ENABLE_TRANSMIT=false` must not open the gate.

    "Any value is true" is the usual shortcut and it is wrong here: the variable
    keys a transmitter, and an operator who wrote `false` would have one on air.
    """
    assert not Config.from_environment(environment(SIGHOP_ENABLE_TRANSMIT=value)).transmit_enabled


def test_a_flag_that_is_neither_is_refused_rather_than_guessed() -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.from_environment(environment(SIGHOP_ENABLE_TRANSMIT="maybe"))
    assert ENABLE_TRANSMIT_VARIABLE in str(excinfo.value)
    assert "maybe" in str(excinfo.value)


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (RADIO_PRESET_VARIABLE, "us915"),
        (DUTY_CYCLE_CEILING_VARIABLE, "ten-percent"),
        (STATUS_INTERVAL_VARIABLE, "soon"),
        (STATUS_INTERVAL_VARIABLE, "0"),
        (STATUS_INTERVAL_VARIABLE, "-1"),
        (DEDUP_TTL_VARIABLE, "0"),
        (DEDUP_MAX_ENTRIES_VARIABLE, "0"),
        (DEDUP_MAX_ENTRIES_VARIABLE, "lots"),
        (WEB_PORT_VARIABLE, "70000"),
        (WEB_PORT_VARIABLE, "http"),
    ],
)
def test_a_value_a_setting_cannot_take_names_the_variable_and_the_value(
    variable: str, value: str
) -> None:
    with pytest.raises(ConfigError) as excinfo:
        Config.from_environment(environment(**{variable: value}))
    message = str(excinfo.value)
    assert variable in message
    assert value in message


@pytest.mark.parametrize("value", ["10", "1.5", "0", "-0.1"])
def test_a_duty_cycle_ceiling_outside_a_fraction_is_refused(value: str) -> None:
    """`SIGHOP_DUTY_CYCLE_CEILING=10` means ten hours in every hour, not 10%.

    Clamping it to 1.0 would honour a typo by transmitting continuously, which
    is the regulatory failure the default exists to prevent.
    """
    with pytest.raises(ConfigError, match=DUTY_CYCLE_CEILING_VARIABLE):
        Config.from_environment(environment(SIGHOP_DUTY_CYCLE_CEILING=value))


def test_port_zero_is_accepted_because_it_asks_the_operating_system() -> None:
    assert Config.from_environment(environment(SIGHOP_WEB_PORT="0")).web_port == 0


def test_an_empty_allowed_host_entry_is_not_a_host_name() -> None:
    config = Config.from_environment(environment(SIGHOP_WEB_ALLOWED_HOSTS="a.example,,b.example,"))
    assert config.web_allowed_hosts == ("a.example", "b.example")


# --- The startup check (design D3) ------------------------------------------


def test_a_usable_environment_has_no_problems() -> None:
    assert check_environment(environment(SIGHOP_MODEM="/dev/modem")) == []


def test_every_problem_is_reported_in_one_pass_not_the_first_one() -> None:
    """An operator filling in a new `.env` must not restart once per variable."""
    problems = check_environment({"SIGHOP_STATUS_INTERVAL": "soon"})
    joined = "\n".join(problems)
    assert len(problems) == 4
    assert "DATABASE_URL" in joined
    assert SECRET_KEY_VARIABLE in joined
    assert MODEM_VARIABLE in joined
    assert STATUS_INTERVAL_VARIABLE in joined


def test_the_problems_are_ordered_by_consequence() -> None:
    problems = check_environment({})
    assert "DATABASE_URL" in problems[0]
    assert SECRET_KEY_VARIABLE in problems[1]
    assert MODEM_VARIABLE in problems[2]


def test_a_missing_secret_prints_the_command_that_generates_one() -> None:
    """There is no `sighop keys secret` any more, so the refusal carries it."""
    (problem,) = [p for p in check_environment(environment(SIGHOP_SECRET_KEY="")) if "SECRET" in p]
    assert SECRET_KEY_COMMAND in problem
    assert SECRET_KEY_COMMAND == "openssl rand -base64 32"


def test_a_malformed_secret_also_says_how_to_generate_a_replacement() -> None:
    (problem,) = [
        p for p in check_environment(environment(SIGHOP_SECRET_KEY="short")) if "SECRET" in p
    ]
    assert SECRET_KEY_COMMAND in problem


def test_the_check_never_prints_the_password() -> None:
    problems = check_environment({"DATABASE_URL": f"{URL}?sslmode=require"})
    assert PASSWORD not in "\n".join(problems)


def test_the_check_reports_rather_than_raises() -> None:
    """The caller prints the whole list and exits once; a raise would stop at one."""
    assert isinstance(check_environment({}), list)
