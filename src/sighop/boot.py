"""The entry point: everything between a process starting and the node running.

This is the whole of sighop's command surface, and it has none. There are no
subcommands, no options and no positional arguments — the environment is the
only configuration surface (design D2), so an argument is a mistake rather than
a request, and is refused as one.

The order below is the order of consequence, and each step exists because
skipping it would produce a node that looks like it is working and is not:

1. **Check the environment, all of it, and report every problem at once.** An
   operator filling in a new `.env` must not restart once per missing variable.
2. **Apply outstanding migrations.** Starting is the deploy (`compose-deployment`),
   so there is no separate command and no flag to remember. A database *ahead*
   of this build is still refused — nothing here describes its schema.
3. **Open persistence and load the identities**, before a pipeline exists, so a
   database that cannot be reached is a startup failure rather than a node that
   quietly stores nothing (`database`).
4. **Bind the web interface**, before the run starts, so a port clash is a
   startup failure too and never a run with a silently absent panel.
5. **Run**, until a signal stops it.

This module is also the one place that knows both sides of milestone 8's seam:
`runtime.py` imports nothing from `web/` and `web/` imports nothing from
`runtime.py`, and here a `Runtime` becomes the panel's state and the interface
becomes one of the run's services.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import IO

from sighop.config import (
    Config,
    ConfigError,
    DatabaseConfig,
    check_environment,
)
from sighop.db import migrations
from sighop.db.engine import Database, Failed, classify
from sighop.db.migrations import MigrationsNotFoundError
from sighop.db.persistence import Persistence
from sighop.db.repositories import LoadedEntity
from sighop.logging import configure_logging, get_logger
from sighop.monitor.render import render_startup
from sighop.net.airtime import cross_check_airtime
from sighop.radio.capture import CaptureWriter
from sighop.radio.kiss import KissTransport, serial_connector
from sighop.radio.modem import EU868_NARROW, Modem, RadioParams
from sighop.radio.probe import ProbeResult
from sighop.runtime import Runtime, RuntimeConfig
from sighop.web.app import (
    NO_ENABLED_ACCOUNT,
    WebInterface,
    WebStartupError,
    validate_allowed_hosts,
)
from sighop.web.auth import Authenticator, FirstRunSetup
from sighop.web.chat import ChannelLog, ConversationLog
from sighop.web.feed import FeedHub

RADIO_PRESETS: dict[str, RadioParams] = {"eu868-narrow": EU868_NARROW}
"""Preset name to radio parameters. `config.RADIO_PRESET_NAMES` is what the
environment validates against; a test asserts the two agree, so a name cannot be
accepted there and be unresolvable here."""

USAGE = (
    "sighop takes no arguments: every setting is an environment variable.\n"
    "See .env.example for the full list."
)


def main(argv: Sequence[str] | None = None, out: IO[str] | None = None) -> int:
    """Boot the node. Returns a process exit code; never raises for a bad environment.

    `argv` and `out` exist for tests and for nothing else — the console script
    calls this with neither. `argv` is accepted only so that being given an
    argument can be *refused*, which is the observable behaviour the
    `container-image` spec asks for.
    """
    arguments = sys.argv[1:] if argv is None else list(argv)
    stream = out if out is not None else sys.stdout
    if arguments:
        print(USAGE, file=sys.stderr)
        return 2

    problems = check_environment()
    if problems:
        for problem in problems:
            print(problem, file=sys.stderr)
            print(file=sys.stderr)
        return 2

    config = Config.from_environment()
    if config.log_file is not None:
        with open(config.log_file, "a", encoding="utf-8") as log_stream:
            configure_logging(log_stream, stream=sys.stderr)
            return asyncio.run(run(config, out=stream))
    configure_logging(stream=sys.stderr)
    return asyncio.run(run(config, out=stream))


async def run(config: Config, out: IO[str] | None = None) -> int:
    """The node itself: modem, migrations, persistence, panel, pipeline."""
    assert config.modem is not None, "check_environment guarantees this"
    transport = KissTransport(serial_connector(config.modem))
    modem = Modem(transport, radio_params=RADIO_PRESETS[config.radio_preset])
    events = modem.events()

    async def probe_result() -> ProbeResult | None:
        await modem.probe_ready.wait()
        return modem.probe_result

    await migrate_on_start(config)
    persistence, stored = await open_persistence(config)
    writer = CaptureWriter(Path(config.capture_file)) if config.capture_file is not None else None
    if writer is not None:
        writer.open()
    interface: WebInterface | None = None
    try:
        runtime = Runtime(
            source=events,
            startup=lambda: live_startup(modem, runtime),
            config=runtime_config(config, stored),
            webhook_secret=webhook_secret(config),
            sender=modem,
            out=out,
            capture_writer=writer,
            capture_probe=probe_result,
            persistence=persistence,
        )
        interface = await attach_web(config, runtime, out)
        runtime.install_signal_handlers()
        await runtime.run()
    finally:
        if interface is not None:
            interface.close()
        if writer is not None:
            writer.close()
    return 0


async def live_startup(modem: Modem, runtime: Runtime) -> str:
    """Wait for the probe, then adopt the board's own radio readback.

    The readback rather than the configured preset: the budget is enforced
    against what the radio is actually doing, and refuses to guess when the
    board answered nothing.
    """
    await modem.probe_ready.wait()
    probe_result = modem.probe_result
    # Adopted before the radio, so `set_radio` can hand a room server the
    # telemetry the board actually answered with (design D14).
    runtime.probe_result = probe_result
    runtime.set_radio(probe_result.observed_radio if probe_result is not None else None)
    if probe_result is not None:
        await cross_check_airtime(modem, runtime.radio or modem.radio_params)
    return render_startup(probe_result)


def runtime_config(config: Config, stored: Sequence[LoadedEntity] = ()) -> RuntimeConfig:
    """The environment's settings as the runtime's own configuration object."""
    # Raises ConfigError on a bad SIGHOP_PATH_HASH_SIZE, before the pipeline
    # exists and so before anything could be transmitted at a width not asked for.
    path_hash_size, path_hash_size_from_env = config.path_hash_size()
    return RuntimeConfig(
        transmit_enabled=config.transmit_enabled,
        status_interval=config.status_interval,
        dedup_ttl_seconds=config.dedup_ttl_seconds,
        dedup_max_entries=config.dedup_max_entries,
        ceiling_fraction=config.ceiling_fraction,
        stored_entities=tuple(stored),
        path_hash_size=path_hash_size,
        path_hash_size_from_env=path_hash_size_from_env,
    )


# --- Migrations and persistence ---------------------------------------------


async def migrate_on_start(config: Config) -> None:
    """Bring a database that is behind up to this build's head.

    Unconditional: starting the container is the deploy (`compose-deployment`),
    and a flag that the only deployment always passes is not a choice, it is a
    step someone can forget.

    Only *behind* is fixed here. A database ahead of the code is left untouched
    for the schema-version check to refuse: nothing in this build describes its
    schema, and Alembic would fail on it less legibly than that check does.
    """
    logger = get_logger(component="db")
    database = config.database
    before = await _read_revision(database)
    try:
        if before is not None and not migrations.knows_revision(before):
            migrations.migrations_dir()  # a missing chain, not an ahead database
            return
        await migrations.upgrade_async(database)
    except MigrationsNotFoundError as exc:
        raise ConfigError(str(exc)) from exc
    except Exception as exc:  # the driver's own failures, classified for the operator
        raise classify(exc, config=database, operation="migrate") from exc
    after = await _read_revision(database)
    logger.info(
        "database_migrated",
        outcome="success",
        from_revision=before,
        to_revision=after,
        applied=before != after,
    )


async def _read_revision(database: DatabaseConfig) -> str | None:
    handle = Database(config=database)
    try:
        return await handle.read_applied_revision()
    finally:
        await handle.dispose()


async def open_persistence(config: Config) -> tuple[Persistence, tuple[LoadedEntity, ...]]:
    """Open the database, check the schema version and load the entities.

    Never falls back: a database that cannot be reached, is unauthenticated or
    is unmigrated raises, because falling back would present a running node
    whose durability an operator had already assumed (`database` spec).
    """
    persistence = Persistence(database=Database(config=config.database), writes_enabled=True)
    await persistence.open()

    try:
        # The secret is required only when there is something sealed to open.
        # A first run against an empty store must not demand a key it has no
        # use for; a run with entities in it must, and says which variable.
        listed = await persistence.entities.list_all()
        if isinstance(listed, Failed):
            raise listed.error
        if not any(record.enabled for record in listed.value):
            return persistence, ()
        loaded = await persistence.entities.load_openable(
            config.secret_key_bytes(), enabled_only=True
        )
        if isinstance(loaded, Failed):
            raise loaded.error
        for refusal in loaded.value.stranded:
            # A row left behind by migration 0008. The run continues on the
            # identities that did open — see `load_openable` — but this must
            # reach the terminal every start until the row is dealt with.
            print(f"!! {refusal}", file=sys.stderr)
    except BaseException:
        await persistence.stop()
        raise
    return persistence, loaded.value.opened


# --- Secrets ----------------------------------------------------------------


def webhook_secret(config: Config) -> bytes | None:
    """`SIGHOP_SECRET_KEY` for opening webhook URLs, or `None` when unusable.

    Not demanded here: `check_environment` has already refused a run without a
    usable secret, so this cannot be the place a bad value is first discovered.
    """
    try:
        return config.secret_key_bytes()
    except ConfigError:  # pragma: no cover - the run would already have failed
        return None


def web_sealing_secret(config: Config) -> bytes | None:
    """`SIGHOP_SECRET_KEY` for the panel, or `None` when it cannot be read.

    Design D1: the panel exports a *stored* identity, which means opening a
    sealed private key, which needs the key this module already reads. It is
    passed in rather than reached for inside `web/` because this is the one
    module that composes both sides.
    """
    try:
        return config.secret_key_bytes()
    except ConfigError:  # pragma: no cover - the run would already have failed
        return None


# --- The web interface ------------------------------------------------------


async def attach_web(config: Config, runtime: Runtime, out: IO[str] | None) -> WebInterface:
    """Bind the panel's socket and hang it off the run.

    Called after the runtime is composed and **before** it runs, which is what
    makes a port clash a startup failure: nothing has been received, nothing has
    been transmitted, and the run does not continue with a silently absent
    interface (`web-server`).

    Two refusals come before the bind, each before any socket exists: an allowed
    host name that is a wildcard or a URL, and a database whose accounts are all
    disabled (nobody could sign in, and an operator locked it deliberately). A
    database with no account at all is served in first-run setup instead, with a
    one-time code generated here (web-first-run-setup design D1).

    There is no way to decline the interface: it is how the node is administered
    now that there is no command line, so a node without one cannot be operated.
    """
    allowed = validate_allowed_hosts(config.web_allowed_hosts)
    persistence = runtime.persistence
    total = await persistence.web_users.count()
    if isinstance(total, Failed):
        raise total.error
    enabled = await persistence.web_users.count_enabled()
    if isinstance(enabled, Failed):
        raise enabled.error
    setup: FirstRunSetup | None = None
    if total.value == 0:
        setup = FirstRunSetup()
    elif enabled.value < 1:
        raise WebStartupError(NO_ENABLED_ACCOUNT)
    auth = Authenticator(accounts=persistence.web_users, setup=setup)
    # One hub for the process, fed by the pipeline's observer and the run's TX
    # resolution callback, and fanning out to a bounded queue per browser
    # (design D4). The runtime is handed two plain callables and never learns
    # what is on the other end of them.
    hub = FeedHub()
    hub.subscribe(runtime.bus)
    runtime.watch_traffic(
        on_reception=hub.on_reception,
        on_transmission=lambda submission, outcome, at: hub.on_transmission(
            submission, outcome, at=at
        ),
    )
    # The panel's own view of this run's conversations, attached as a second
    # record sink beside the durable one: the live half of chat, which keeps
    # working while the database is degraded.
    conversations = ConversationLog()
    runtime.watch_messages(conversations)
    channel_log = ChannelLog()
    runtime.watch_channels(channel_log)
    interface = WebInterface.bind(
        runtime,
        auth=auth,
        accounts_enabled=enabled.value,
        host=config.web_host,
        port=config.web_port,
        allowed=allowed,
        feed=hub,
        conversations=conversations,
        sealing_secret=web_sealing_secret(config),
        announce=runtime.say,
        channel_log=channel_log,
    )
    # The event first, then the output. Both are unconditional: there is no
    # setting that serves a non-loopback bind without saying what it exposes.
    interface.report()
    stream = out if out is not None else sys.stdout
    for line in interface.startup_lines():
        print(line, file=stream)
    runtime.services = (*runtime.services, interface.service())
    return interface
