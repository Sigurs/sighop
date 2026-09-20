"""Configuration from the environment (DESIGN.md §10, design D9).

§11 has listed this module since the beginning and nothing needed it until
persistence arrived. It gains exactly two settings — `DATABASE_URL` and
`SIGHOP_SECRET_KEY` — plus the knobs that bound what the database layer may do
with them.

**No dotenv dependency.** `uv run --env-file .env.dev sighop run …` already
loads the file and a container takes its environment from compose or Docker
secrets, so a library to read a file two existing tools already read would be
bought for nothing. Every entry point here takes an explicit mapping, which is
also what lets a test construct a configuration without touching the real
environment.

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

DATABASE_SCHEMA_VARIABLE = "SIGHOP_DB_SCHEMA"
"""The schema every connection's search path is set to. Normally unset, and the
application then owns `public` in its own database.

It exists because the measured role cannot `CREATEDB`, so a test session isolates
in a throwaway *schema* (design D11) — and a test that drives the command line
rather than the repository needs some way to point that command at it. The
environment is the mechanism this module already uses, and a URL cannot carry it:
asyncpg does not read libpq query parameters, which is the same reason those are
refused above."""

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

    `secret_key` is held as the raw base64 text it arrived as and is never
    rendered: `repr` omits it, and only `secret_key_bytes()` decodes it, which
    is also the one place that says whether it was missing, mis-encoded or the
    wrong length (design D4).
    """

    database: DatabaseConfig | None = None
    secret_key: str | None = None
    path_hash_size_raw: str | None = None
    """Raw `SIGHOP_PATH_HASH_SIZE`, validated only by `path_hash_size()`, so a
    typo fails `sighop run` and not `sighop keys` or `sighop db`, which never
    transmit — the same reasoning as the secret key."""

    @property
    def persistent(self) -> bool:
        return self.database is not None

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
            "persistence": "on" if self.persistent else "off",
            "secret_key_present": self.secret_key is not None,
            "path_hash_size": self.path_hash_size_raw,
        }
        if self.database is not None:
            fields.update(self.database.as_json())
        return fields

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        database_url: str | None = None,
    ) -> Config:
        """Read the environment, with the command line overriding the URL.

        `environ` is explicit so a test never depends on the real environment;
        it defaults to `os.environ` for the one caller that should.
        """
        env = os.environ if environ is None else environ
        url = database_url or env.get(DATABASE_URL_VARIABLE) or None
        secret = env.get(SECRET_KEY_VARIABLE) or None
        schema = env.get(DATABASE_SCHEMA_VARIABLE) or None
        path_hash_size = (env.get(PATH_HASH_SIZE_VARIABLE) or "").strip() or None
        return cls(
            database=None if url is None else DatabaseConfig(url=url, schema=schema),
            secret_key=secret,
            path_hash_size_raw=path_hash_size,
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
            "cannot be read or written without it. Generate one with `sighop keys secret`"
        )
    try:
        raw = base64.b64decode(value.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ConfigError(
            f"{SECRET_KEY_VARIABLE} is not valid base64 ({exc}); it is the base64 of "
            f"exactly {SECRET_KEY_SIZE} bytes, as `sighop keys secret` prints"
        ) from exc
    if len(raw) != SECRET_KEY_SIZE:
        raise ConfigError(
            f"{SECRET_KEY_VARIABLE} decodes to {len(raw)} bytes; it must be exactly "
            f"{SECRET_KEY_SIZE}. It is not padded, truncated or hashed into shape — "
            "generate one with `sighop keys secret`"
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
