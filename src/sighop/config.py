"""Configuration from the environment (DESIGN.md §10, design D9).

The environment is the *only* configuration surface: there is no command line
to override it and no file to read instead (design D2). Everything an operator
can turn is a `SIGHOP_*` variable read here, parsed here, and validated here —
so a typo fails at startup with the variable's own name in the message, before
the modem is opened and before anything could be transmitted.

**No dotenv dependency.** `uv run --env-file .env.dev sighop` already loads the
file and a container takes its environment from compose, so a library to read a
file two existing tools already read would be bought for nothing. Every entry
point here takes an explicit mapping, which is also what lets a test construct a
configuration without touching the real environment.

**Defaults live here.** Modules that consume a setting import its default from
this module rather than declaring their own, as `net/` already does for
`DEFAULT_PATH_HASH_SIZE`. One declaration means the value the environment
defaults to and the value a constructor defaults to cannot drift apart.

Two rules are load-bearing and neither is cosmetic:

* **The URL is validated here, not at first connect.** asyncpg does not
  understand libpq's query parameters (`?sslmode=`, `?options=`) and has no
  synchronous mode; a URL naming another driver would otherwise surface as a
  driver exception the first time a deployment met a TLS terminator (design D1).
  It is rejected at construction with a message naming what is unsupported.
* **The password is redacted in every rendering.** `repr`, log fields, startup
  output and error messages all go through the same redacted form, so there is
  one place to be right rather than one place per caller.
"""

from __future__ import annotations

import base64
import binascii
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

DATABASE_URL_VARIABLE = "DATABASE_URL"
SECRET_KEY_VARIABLE = "SIGHOP_SECRET_KEY"
PATH_HASH_SIZE_VARIABLE = "SIGHOP_PATH_HASH_SIZE"

MODEM_VARIABLE = "SIGHOP_MODEM"
RADIO_PRESET_VARIABLE = "SIGHOP_RADIO_PRESET"
ENABLE_TRANSMIT_VARIABLE = "SIGHOP_ENABLE_TRANSMIT"
DUTY_CYCLE_CEILING_VARIABLE = "SIGHOP_DUTY_CYCLE_CEILING"
STATUS_INTERVAL_VARIABLE = "SIGHOP_STATUS_INTERVAL"
DEDUP_TTL_VARIABLE = "SIGHOP_DEDUP_TTL"
DEDUP_MAX_ENTRIES_VARIABLE = "SIGHOP_DEDUP_MAX_ENTRIES"
CAPTURE_FILE_VARIABLE = "SIGHOP_CAPTURE_FILE"
LOG_FILE_VARIABLE = "SIGHOP_LOG_FILE"
WEB_HOST_VARIABLE = "SIGHOP_WEB_HOST"
WEB_PORT_VARIABLE = "SIGHOP_WEB_PORT"
WEB_ALLOWED_HOSTS_VARIABLE = "SIGHOP_WEB_ALLOWED_HOSTS"

REQUIRED_DRIVER = "postgresql+asyncpg"
"""DESIGN.md §2 names asyncpg and the operator confirmed it (design D1). One
driver in the codebase, so `alembic/env.py` bridges rather than the URL forking."""

LIBPQ_ONLY_QUERY_PARAMETERS = frozenset(
    {
        "sslmode",
        "sslcert",
        "sslkey",
        "sslrootcert",
        "options",
        "passfile",
        "service",
        "target_session_attrs",
    }
)
"""psycopg spellings asyncpg does not read. Passing one through would connect
without the TLS the operator asked for, which is the failure worth refusing."""

DEFAULT_POOL_SIZE = 5
DEFAULT_MAX_OVERFLOW = 5
DEFAULT_POOL_TIMEOUT = 10.0
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_STATEMENT_TIMEOUT = 5.0
DEFAULT_SHUTDOWN_BUDGET = 5.0

ROLE_CONNECTION_LIMIT = 30
"""Measured on the development role (`rolconnlimit`), not assumed. The default
pool of at most 10 leaves room for a second instance, an Alembic run, a `psql`
session and the test suite before anyone meets `too many connections for role`."""

SECRET_KEY_SIZE = 32
"""XSalsa20-Poly1305's key size (design D4). Exact — never padded, truncated or
hashed into shape, because each of those silently accepts a weaker secret."""

RADIO_PRESET_NAMES = ("eu868-narrow",)
"""The preset names the environment accepts. `boot.py` maps each to its
`RadioParams`; a test asserts the two agree, so a preset cannot be offered here
and be unresolvable there."""

DEFAULT_RADIO_PRESET = "eu868-narrow"

DEFAULT_CEILING_FRACTION = 0.10
"""EU 868's 869.4-869.65 MHz sub-band permits 500 mW e.r.p. conditional on a
10% duty cycle. Enforced by the scheduler regardless of any modem-side setting —
the stock firmware ships a 50% default, which does not satisfy it."""

DEFAULT_STATUS_INTERVAL_SECONDS = 60.0

DEFAULT_TTL_SECONDS = 300.0
"""TTL is the correctness bound (design D11), and the live evidence bounds it
from both sides. Most repeats arrive within a second, but the 2 h 54 min session
on 2026-09-04 saw a flood copy arrive **200.7 s** late by a different path, so a
shorter TTL discards real duplicates. The same session also saw two byte-identical
DIRECT frames **3158 s** apart — a sender retransmitting an unacked message, not
a copy of one transmission — so a longer TTL starts swallowing retries the user
is entitled to see. Five minutes sits between those, at 1.5x margin over the
worst real duplicate. Do not re-derive it from a capture shorter than the TTL
itself: the 442-frame corpus put the worst case at 31.1 s and was simply too
short to sample the tail."""

DEFAULT_MAX_ENTRIES = 4096
"""The memory bound on the duplicate cache. Repetition rate is not a property of
the mesh (41.6% over the milestone 0 nights, 11.0% over milestone 2's), so
nothing about observed traffic bounds the distinct-packet count and the cap has
to."""

DEFAULT_WEB_HOST = "127.0.0.1"
"""Loopback, and it is the default because the panel is served over plain HTTP:
off loopback, passwords and session cookies cross the network unencrypted. An
operator may choose otherwise; the choice is announced (`web-server`)."""

DEFAULT_WEB_PORT = 8080

TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})
"""A flag's variable is spelled out both ways rather than treating "any value is
true": an operator who writes `SIGHOP_ENABLE_TRANSMIT=false` means false, and
reading that as true would put a transmitter on air."""

PATH_HASH_SIZES = ("1", "2", "3")
DEFAULT_PATH_HASH_SIZE = 3
"""Bytes per hop hash on packets sighop originates with an empty path.

Repeaters append at the width the origin chose (`Mesh.cpp:349`) and firmware's
`sendFlood` accepts 1 to 3, so 3 is the least collision-prone width the mesh takes.
Learned routes keep the width they were learned at; this governs only floods and
zero-hop packets we start."""

REDACTED = "***"


class ConfigError(ValueError):
    """A configuration value that cannot be used as given. Never carries a password."""


@dataclass(frozen=True, slots=True, repr=False)
class DatabaseConfig:
    """Where the database is and what it is allowed to cost.

    Every bound has a default and every default is overridable. The three time
    bounds exist together on purpose (design D16): the database sits behind a
    NodePort on another host, and a host that is *down blackholes packets rather
    than refusing them*, so an unbounded connect would inherit asyncpg's 60 s
    default in a pipeline that must never stall for 60 s.
    """

    url: str

    pool_size: int = DEFAULT_POOL_SIZE
    max_overflow: int = DEFAULT_MAX_OVERFLOW
    pool_timeout: float = DEFAULT_POOL_TIMEOUT
    pool_pre_ping: bool = True
    """On because idle connections through a NodePort on another host get reaped,
    and a reaped connection must surface as one retry rather than as an error."""

    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    statement_timeout: float = DEFAULT_STATEMENT_TIMEOUT

    shutdown_budget: float = DEFAULT_SHUTDOWN_BUDGET
    """What the write lanes together may spend draining at shutdown.

    The default comes from the deployment's grace period rather than from taste:
    `stop_grace_period: 20s` in compose, minus the web server's 5 s graceful
    shutdown, minus the bot drain's 2 s, minus the modem close, leaves 5 s for
    the writers with margin. It is deliberately below one `operation_timeout`
    (`connect_timeout + statement_timeout`), so a genuinely hung database has
    its in-flight batch cancelled, returned to the buffer and counted as failed
    rather than being waited out (design D4). One budget covers all the lanes
    together, so this does not multiply by the number of writers."""

    schema: str | None = None
    """The schema every connection's search path is set to, when one is named.

    The measured role cannot `CREATEDB`, so a test session isolates in a
    throwaway *schema* rather than a throwaway database (design D11). Production
    leaves this None and gets `public`, which the application owns."""

    def __post_init__(self) -> None:
        parsed = _parse_url(self.url)
        if parsed.drivername != REQUIRED_DRIVER:
            raise ConfigError(
                f"database URL names the {parsed.drivername!r} driver; sighop uses "
                f"{REQUIRED_DRIVER!r} (DESIGN.md §2). Rewrite the URL's scheme rather "
                "than adding a second driver"
            )
        unsupported = sorted(set(parsed.query) & LIBPQ_ONLY_QUERY_PARAMETERS)
        if unsupported:
            raise ConfigError(
                f"database URL carries {', '.join(unsupported)}, which "
                f"{REQUIRED_DRIVER} does not read: those are libpq spellings and "
                "asyncpg takes TLS and server settings through connect arguments"
            )
        for bound, value in (
            ("pool_size", self.pool_size),
            ("max_overflow", self.max_overflow),
        ):
            if value < 0:
                raise ConfigError(f"{bound} cannot be negative, got {value}")
        for bound, seconds in (
            ("pool_timeout", self.pool_timeout),
            ("connect_timeout", self.connect_timeout),
            ("statement_timeout", self.statement_timeout),
            ("shutdown_budget", self.shutdown_budget),
        ):
            if seconds <= 0:
                raise ConfigError(
                    f"{bound} must be positive: an unbounded database operation is "
                    f"what design D16 exists to prevent, got {seconds}"
                )

    # --- Rendering, always redacted ----------------------------------------

    @property
    def sqlalchemy_url(self) -> URL:
        return _parse_url(self.url)

    @property
    def redacted_url(self) -> str:
        """The URL with the password replaced. The only form that is ever shown."""
        return self.sqlalchemy_url.render_as_string(hide_password=True)

    @property
    def host(self) -> str:
        return self.sqlalchemy_url.host or "(no host)"

    @property
    def port(self) -> int | None:
        return self.sqlalchemy_url.port

    @property
    def database(self) -> str:
        return self.sqlalchemy_url.database or "(no database)"

    @property
    def username(self) -> str:
        return self.sqlalchemy_url.username or "(no role)"

    @property
    def max_connections(self) -> int:
        """What one instance may open at once, against `ROLE_CONNECTION_LIMIT`."""
        return self.pool_size + self.max_overflow

    def __repr__(self) -> str:
        return (
            f"DatabaseConfig(url={self.redacted_url!r}, pool_size={self.pool_size}, "
            f"max_overflow={self.max_overflow}, pool_timeout={self.pool_timeout}, "
            f"pool_pre_ping={self.pool_pre_ping}, "
            f"connect_timeout={self.connect_timeout}, "
            f"statement_timeout={self.statement_timeout}, "
            f"shutdown_budget={self.shutdown_budget})"
        )

    def as_json(self) -> dict[str, object]:
        """Log fields. Carries the redacted URL and never the password."""
        return {
            "database_url": self.redacted_url,
            "database_host": self.host,
            "database_port": self.port,
            "database_name": self.database,
            "database_role": self.username,
            "pool_size": self.pool_size,
            "max_overflow": self.max_overflow,
            "pool_timeout": self.pool_timeout,
            "connect_timeout": self.connect_timeout,
            "statement_timeout": self.statement_timeout,
            "shutdown_budget": self.shutdown_budget,
        }

    # --- What the driver is handed -----------------------------------------

    def connect_args(self) -> dict[str, object]:
        """asyncpg connect arguments carrying both time bounds explicitly.

        `timeout` bounds establishing a connection and `command_timeout` bounds
        a statement client-side; `statement_timeout` bounds it server-side too,
        so a statement abandoned by the client does not go on burning a backend.
        None of the three is left at the driver's default (design D16).
        """
        settings = {
            "statement_timeout": str(int(self.statement_timeout * 1000)),
            "application_name": "sighop",
        }
        if self.schema is not None:
            settings["search_path"] = self.schema
        return {
            "timeout": self.connect_timeout,
            "command_timeout": self.statement_timeout,
            "server_settings": settings,
        }

    def with_url(self, url: str) -> DatabaseConfig:
        return replace(self, url=url)

    def with_schema(self, schema: str | None) -> DatabaseConfig:
        return replace(self, schema=schema)


@dataclass(frozen=True, slots=True, repr=False)
class Config:
    """Everything sighop reads from the environment.

    `database` is not optional: a node with no database cannot start
    (`database` spec), so a `Config` that exists names one. `from_environment`
    is where an absent `DATABASE_URL` becomes a `ConfigError`.

    `secret_key` is held as the raw base64 text it arrived as and is never
    rendered: `repr` omits it, and only `secret_key_bytes()` decodes it, which
    is also the one place that says whether it was missing, mis-encoded or the
    wrong length (design D4).
    """

    database: DatabaseConfig
    secret_key: str | None = None
    path_hash_size_raw: str | None = None
    """Raw `SIGHOP_PATH_HASH_SIZE`, validated only by `path_hash_size()`, for
    the same reason the secret key is: a value is checked where it is used, so
    the error names the thing the operator was trying to do."""

    # --- The radio ----------------------------------------------------------

    modem: str | None = None
    """`SIGHOP_MODEM`: the stable serial device path. None is "not configured",
    which `check_environment` reports; it is not a default."""

    radio_preset: str = DEFAULT_RADIO_PRESET
    transmit_enabled: bool = False
    """`SIGHOP_ENABLE_TRANSMIT`. Off unless the environment says otherwise:
    without it packets are scheduled, charged and logged, then dropped at the
    hand-off to the modem."""

    ceiling_fraction: float = DEFAULT_CEILING_FRACTION
    status_interval: float = DEFAULT_STATUS_INTERVAL_SECONDS
    dedup_ttl_seconds: float = DEFAULT_TTL_SECONDS
    dedup_max_entries: int = DEFAULT_MAX_ENTRIES

    capture_file: str | None = None
    log_file: str | None = None

    # --- The web interface --------------------------------------------------

    web_host: str = DEFAULT_WEB_HOST
    web_port: int = DEFAULT_WEB_PORT
    web_allowed_hosts: tuple[str, ...] = ()
    """`SIGHOP_WEB_ALLOWED_HOSTS`, comma-separated. A list rather than a
    repeated flag because a variable cannot repeat; empty entries are dropped so
    a trailing comma is not a host name."""

    def secret_key_bytes(self) -> bytes:
        return parse_secret_key(self.secret_key)

    def path_hash_size(self) -> tuple[int, bool]:
        """The path hash size in force, and whether the environment set it."""
        return parse_path_hash_size(self.path_hash_size_raw), self.path_hash_size_raw is not None

    def __repr__(self) -> str:
        secret = "None" if self.secret_key is None else f"'{REDACTED}'"
        return f"Config(database={self.database!r}, secret_key={secret})"

    def as_json(self) -> dict[str, object]:
        fields: dict[str, object] = {
            "secret_key_present": self.secret_key is not None,
            "path_hash_size": self.path_hash_size_raw,
            "modem": self.modem,
            "radio_preset": self.radio_preset,
            "transmit_enabled": self.transmit_enabled,
            "web_host": self.web_host,
            "web_port": self.web_port,
        }
        fields.update(self.database.as_json())
        return fields

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Config:
        """Read and validate the whole environment.

        `environ` is explicit so a test never depends on the real environment;
        it defaults to `os.environ` for the one caller that should.

        Every value is parsed here rather than where it is used, so a malformed
        one fails before the modem is opened. The exceptions are the secret key
        and the path hash size, which are checked at their point of use and
        documented above.
        """
        env = os.environ if environ is None else environ
        url = env.get(DATABASE_URL_VARIABLE) or None
        if url is None:
            raise ConfigError(
                f"no database is configured: set {DATABASE_URL_VARIABLE} to the database's "
                "postgresql+asyncpg URL. sighop stores every identity, room, bot, channel, "
                "message and account in it and does not run without one"
            )
        return cls(
            database=DatabaseConfig(url=url),
            secret_key=env.get(SECRET_KEY_VARIABLE) or None,
            path_hash_size_raw=(env.get(PATH_HASH_SIZE_VARIABLE) or "").strip() or None,
            modem=env.get(MODEM_VARIABLE) or None,
            radio_preset=_one_of(
                env, RADIO_PRESET_VARIABLE, DEFAULT_RADIO_PRESET, RADIO_PRESET_NAMES
            ),
            transmit_enabled=_flag(env, ENABLE_TRANSMIT_VARIABLE),
            ceiling_fraction=_fraction(env, DUTY_CYCLE_CEILING_VARIABLE, DEFAULT_CEILING_FRACTION),
            status_interval=_positive_float(
                env, STATUS_INTERVAL_VARIABLE, DEFAULT_STATUS_INTERVAL_SECONDS
            ),
            dedup_ttl_seconds=_positive_float(env, DEDUP_TTL_VARIABLE, DEFAULT_TTL_SECONDS),
            dedup_max_entries=_positive_int(env, DEDUP_MAX_ENTRIES_VARIABLE, DEFAULT_MAX_ENTRIES),
            capture_file=env.get(CAPTURE_FILE_VARIABLE) or None,
            log_file=env.get(LOG_FILE_VARIABLE) or None,
            web_host=env.get(WEB_HOST_VARIABLE) or DEFAULT_WEB_HOST,
            web_port=_port(env, WEB_PORT_VARIABLE, DEFAULT_WEB_PORT),
            web_allowed_hosts=_comma_separated(env, WEB_ALLOWED_HOSTS_VARIABLE),
        )


def parse_secret_key(value: str | None) -> bytes:
    """`SIGHOP_SECRET_KEY` as exactly 32 bytes, or an error saying which it was.

    Missing, mis-encoded and wrong-length are three different operator mistakes
    with three different fixes, and a message that conflated them would send
    someone to regenerate a secret that was merely absent from the environment.
    Nothing here pads, truncates or hashes a value into shape: each of those
    turns a typo into a key that works and cannot be recovered from later.
    """
    if value is None or not value.strip():
        raise ConfigError(
            f"{SECRET_KEY_VARIABLE} is not set; entity seeds are sealed under it and "
            f"cannot be read or written without it. Generate one with `{SECRET_KEY_COMMAND}`"
        )
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError(
            f"{SECRET_KEY_VARIABLE} is not valid base64 ({exc}); it is the base64 of "
            f"exactly {SECRET_KEY_SIZE} bytes, as `{SECRET_KEY_COMMAND}` prints"
        ) from exc
    if len(raw) != SECRET_KEY_SIZE:
        raise ConfigError(
            f"{SECRET_KEY_VARIABLE} decodes to {len(raw)} bytes; it must be exactly "
            f"{SECRET_KEY_SIZE}. It is not padded, truncated or hashed into shape — "
            f"generate one with `{SECRET_KEY_COMMAND}`"
        )
    return raw


def parse_path_hash_size(value: str | None) -> int:
    """`SIGHOP_PATH_HASH_SIZE` as 1, 2 or 3; unset or blank is the default.

    Anything else is refused rather than clamped: a width the operator did not
    ask for would change every flood's on-air bytes without a word.
    """
    if value is None or not value.strip():
        return DEFAULT_PATH_HASH_SIZE
    text = value.strip()
    if text not in PATH_HASH_SIZES:
        raise ConfigError(
            f"{PATH_HASH_SIZE_VARIABLE} is {value!r}; it must be one of "
            f"{', '.join(PATH_HASH_SIZES)} (bytes per hop hash), or unset for "
            f"{DEFAULT_PATH_HASH_SIZE}"
        )
    return int(text)


def generate_secret_key() -> str:
    """A new secret from the system CSPRNG, base64 as the variable takes it."""
    return base64.b64encode(os.urandom(SECRET_KEY_SIZE)).decode("ascii")


def _parse_url(url: str) -> URL:
    try:
        return make_url(url)
    except ArgumentError as exc:
        # The driver's own message quotes the whole string back, password and
        # all. Unparseable text cannot be redacted field-wise, so the message
        # names the variable and the expected shape instead of echoing either.
        raise ConfigError(
            f"{DATABASE_URL_VARIABLE} could not be parsed as a database URL; it looks "
            f"like {REQUIRED_DRIVER}://role:password@host:port/database"
        ) from exc


# --- Parsing one variable at a time -----------------------------------------
#
# Each helper names its own variable and the value it was given, because the
# operator's next action is to edit that line of their environment file. None of
# them clamps, rounds or falls back to a default on a value it does not accept:
# a transmitter running at a duty cycle nobody asked for is worse than a node
# that refused to start.


def _flag(env: Mapping[str, str], variable: str) -> bool:
    raw = env.get(variable)
    if raw is None:
        return False
    text = raw.strip().lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    raise ConfigError(
        f"{variable} is {raw!r}; it must be one of {', '.join(sorted(TRUE_VALUES))} "
        f"or {', '.join(sorted(value for value in FALSE_VALUES if value))}, or unset"
    )


def _one_of(env: Mapping[str, str], variable: str, default: str, allowed: tuple[str, ...]) -> str:
    raw = env.get(variable)
    if raw is None or not raw.strip():
        return default
    text = raw.strip()
    if text not in allowed:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be one of {', '.join(sorted(allowed))}, "
            f"or unset for {default}"
        )
    return text


def _positive_float(env: Mapping[str, str], variable: str, default: float) -> float:
    raw = env.get(variable)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be a number of seconds, or unset for {default}"
        ) from exc
    if value <= 0:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be greater than zero, or unset for {default}"
        )
    return value


def _positive_int(env: Mapping[str, str], variable: str, default: int) -> int:
    raw = env.get(variable)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be a whole number, or unset for {default}"
        ) from exc
    if value <= 0:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be greater than zero, or unset for {default}"
        )
    return value


def _fraction(env: Mapping[str, str], variable: str, default: float) -> float:
    """A duty-cycle ceiling: greater than zero, at most one.

    Refusing above 1.0 is not pedantry. The value is a fraction of an hour, and
    a mistyped `10` meaning "10%" would otherwise read as ten hours in every
    hour and lift the regulatory limit the default exists to keep.
    """
    raw = env.get(variable)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be a fraction of an hour between 0 and 1 "
            f"(0.10 is the EU 868 limit), or unset for {default}"
        ) from exc
    if not 0 < value <= 1:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be greater than 0 and at most 1 — a "
            f"fraction of an hour, not a percentage — or unset for {default}"
        )
    return value


def _port(env: Mapping[str, str], variable: str, default: int) -> int:
    raw = env.get(variable)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be a port number, or unset for {default}"
        ) from exc
    # 0 is meaningful: it asks the operating system to choose, which is how a
    # test binds without picking a port that another test already holds.
    if not 0 <= value <= 65535:
        raise ConfigError(
            f"{variable} is {raw!r}; it must be between 0 and 65535, or unset for {default}"
        )
    return value


def _comma_separated(env: Mapping[str, str], variable: str) -> tuple[str, ...]:
    raw = env.get(variable)
    if raw is None:
        return ()
    return tuple(entry.strip() for entry in raw.split(",") if entry.strip())


# --- The startup check ------------------------------------------------------

SECRET_KEY_COMMAND = "openssl rand -base64 32"
"""How an operator generates a sealing secret now that there is no command to
print one. Named here so the refusal below and the documentation cannot drift."""


def check_environment(environ: Mapping[str, str] | None = None) -> list[str]:
    """Every problem with the environment, in the order an operator fixes them.

    Every problem rather than the first: filling in a new `.env` one restart per
    missing variable is how a five-minute deployment becomes an afternoon. The
    order is the order of consequence — a node with no database cannot store the
    identities the secret would unseal — so the list reads as a sequence of
    fixes rather than an unsorted pile.

    Returns an empty list when the environment is usable. It reports; it does
    not raise, because the caller prints the whole list and exits once.
    """
    env = os.environ if environ is None else environ
    problems: list[str] = []

    database_url = env.get(DATABASE_URL_VARIABLE) or None
    if database_url is None:
        problems.append(
            f"{DATABASE_URL_VARIABLE} is not set. sighop stores every identity, room, bot, "
            "channel, message and account in PostgreSQL and does not run without one.\n"
            f"    {DATABASE_URL_VARIABLE}={REQUIRED_DRIVER}://role:password@host:5432/database"
        )
    else:
        try:
            DatabaseConfig(url=database_url)
        except ConfigError as error:
            problems.append(str(error))

    if not (env.get(SECRET_KEY_VARIABLE) or "").strip():
        problems.append(
            f"{SECRET_KEY_VARIABLE} is not set. It seals every stored identity's private key, "
            "and a database sealed under one key cannot be opened with another.\n"
            "Generate one and keep it with the database it belongs to:\n"
            f"    {SECRET_KEY_COMMAND}"
        )
    else:
        try:
            parse_secret_key(env.get(SECRET_KEY_VARIABLE))
        except ConfigError as error:
            problems.append(f"{error}\nGenerate a replacement with:\n    {SECRET_KEY_COMMAND}")

    if not (env.get(MODEM_VARIABLE) or "").strip():
        problems.append(
            f"{MODEM_VARIABLE} is not set. It is the modem's stable device path, which does "
            "not change when the board is re-plugged:\n"
            f"    {MODEM_VARIABLE}=/dev/serial/by-id/usb-..."
        )

    # The rest in declaration order. Each is parsed in isolation so one bad
    # value does not hide the next.
    for parse in (
        lambda: _one_of(env, RADIO_PRESET_VARIABLE, DEFAULT_RADIO_PRESET, RADIO_PRESET_NAMES),
        lambda: _flag(env, ENABLE_TRANSMIT_VARIABLE),
        lambda: _fraction(env, DUTY_CYCLE_CEILING_VARIABLE, DEFAULT_CEILING_FRACTION),
        lambda: _positive_float(env, STATUS_INTERVAL_VARIABLE, DEFAULT_STATUS_INTERVAL_SECONDS),
        lambda: _positive_float(env, DEDUP_TTL_VARIABLE, DEFAULT_TTL_SECONDS),
        lambda: _positive_int(env, DEDUP_MAX_ENTRIES_VARIABLE, DEFAULT_MAX_ENTRIES),
        lambda: _port(env, WEB_PORT_VARIABLE, DEFAULT_WEB_PORT),
        lambda: parse_path_hash_size((env.get(PATH_HASH_SIZE_VARIABLE) or "").strip() or None),
    ):
        try:
            parse()
        except ConfigError as error:
            problems.append(str(error))

    return problems
