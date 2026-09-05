"""sighop command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import IO

from sighop.config import (
    SECRET_KEY_VARIABLE,
    Config,
    ConfigError,
    DatabaseConfig,
    generate_secret_key,
    parse_secret_key,
)
from sighop.db import migrations
from sighop.db.engine import Database, DatabaseError, Failed, Outcome, classify
from sighop.db.migrations import MigrationsNotFoundError
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    EntityExistsError,
    EntityLoadError,
    EntityRecord,
    EntityRepository,
    LoadedEntity,
)
from sighop.db.sealing import SealError
from sighop.keystore import (
    PLAINTEXT_SEED_NOTICE,
    KeyfileError,
    create_keyfile,
    load_keyfile,
)
from sighop.logging import configure_logging, get_logger
from sighop.monitor.render import render_replay_startup, render_startup
from sighop.monitor.run import MonitorRun
from sighop.net.airtime import cross_check_airtime
from sighop.net.dedup import DEFAULT_MAX_ENTRIES, DEFAULT_TTL_SECONDS
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.protocol.payloads import NodeType
from sighop.radio.capture import CaptureRun, CaptureWriter
from sighop.radio.kiss import KissTransport, serial_connector
from sighop.radio.modem import EU868_NARROW, Modem, RadioParams
from sighop.radio.probe import ProbeResult
from sighop.radio.replay import CaptureReplay
from sighop.runtime import (
    DEFAULT_PEER_WAIT_SECONDS,
    DEFAULT_STATUS_INTERVAL_SECONDS,
    Runtime,
    RuntimeConfig,
)

RADIO_PRESETS = {"eu868-narrow": EU868_NARROW}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sighop")
    subparsers = parser.add_subparsers(dest="command", required=True)

    capture = subparsers.add_parser(
        "capture", help="dump raw modem frames with RxMeta and timestamps to a file"
    )
    capture.add_argument(
        "--device", required=True, help="stable serial device path (/dev/serial/by-id/...)"
    )
    capture.add_argument("--out", required=True, type=Path, help="capture output file (JSONL)")
    capture.add_argument(
        "--radio-preset",
        default="eu868-narrow",
        choices=sorted(RADIO_PRESETS),
        help="radio parameters to apply via SetRadio (default: %(default)s)",
    )
    capture.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="also append wide-event logs here (default: <out stem>.log next to --out)",
    )

    monitor = subparsers.add_parser(
        "monitor", help="decode and print received packets, live or from a capture file"
    )
    source = monitor.add_mutually_exclusive_group(required=True)
    source.add_argument("--device", help="stable serial device path (/dev/serial/by-id/...)")
    source.add_argument(
        "--replay", type=Path, help="capture file to replay through the same decode path"
    )
    monitor.add_argument(
        "--radio-preset",
        default="eu868-narrow",
        choices=sorted(RADIO_PRESETS),
        help="radio parameters to apply via SetRadio, live only (default: %(default)s)",
    )
    monitor.add_argument(
        "--capture",
        type=Path,
        default=None,
        help="also write the monitored frames to this capture file (JSONL)",
    )
    monitor.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="append wide-event logs here instead of standard output",
    )

    run = subparsers.add_parser(
        "run",
        help="run the platform: decode, dedup, learn paths, and schedule transmissions",
    )
    run_source = run.add_mutually_exclusive_group(required=True)
    run_source.add_argument("--device", help="stable serial device path (/dev/serial/by-id/...)")
    run_source.add_argument(
        "--replay", type=Path, help="capture file to replay through the same pipeline"
    )
    run.add_argument(
        "--radio-preset",
        default="eu868-narrow",
        choices=sorted(RADIO_PRESETS),
        help="radio parameters to apply via SetRadio, live only (default: %(default)s)",
    )
    run.add_argument(
        "--enable-transmit",
        action="store_true",
        help=(
            "open the receive-only gate. WITHOUT THIS FLAG NOTHING IS TRANSMITTED: "
            "packets are scheduled, charged against the duty-cycle budget and "
            "logged, then dropped at the hand-off to the modem"
        ),
    )
    run.add_argument(
        "--duty-cycle-ceiling",
        type=float,
        default=DEFAULT_CEILING_FRACTION,
        help=(
            "fraction of an hour sighop may transmit for (default: %(default)s). "
            "On EU 868 the 10%% default is a regulatory limit, not a tuning knob"
        ),
    )
    run.add_argument(
        "--status-interval",
        type=float,
        default=DEFAULT_STATUS_INTERVAL_SECONDS,
        help="seconds between status lines (default: %(default)s)",
    )
    run.add_argument(
        "--stub",
        action="append",
        default=[],
        metavar="NAME",
        dest="stubs",
        help=(
            "add an in-memory advert stub with this name; repeatable. Keys are "
            "generated per process and are never persisted"
        ),
    )
    run.add_argument(
        "--entity",
        action="append",
        default=[],
        type=Path,
        metavar="KEYFILE",
        dest="entities",
        help=(
            "load a persistent entity identity from this keyfile; repeatable. "
            "Two keyfiles whose public keys share a first byte fail startup"
        ),
    )
    run.add_argument(
        "--peer",
        default=None,
        metavar="NAME|HEX-PREFIX",
        help="the contact to send --send to, by exact name or hex public key prefix",
    )
    run.add_argument(
        "--send",
        default=None,
        metavar="TEXT",
        dest="send_text",
        help=(
            "send this text to --peer once, then keep running. The outcome — "
            "acknowledged, unacknowledged, or dropped — is always reported"
        ),
    )
    run.add_argument(
        "--allow-flood",
        action="store_true",
        help=(
            "permit sending to a peer no route is known to. WITHOUT THIS FLAG a "
            "send with no known route is refused: a flood is rebroadcast by every "
            "repeater in the mesh, and must not be reachable by mistyping a name"
        ),
    )
    run.add_argument(
        "--advert-zero-hop",
        default=None,
        metavar="NAME",
        dest="zero_hop_advert",
        help=(
            "emit exactly one zero-hop advert for this entity at startup. It "
            "reaches direct neighbours and stops there, and creates no recurring "
            "zero-hop schedule"
        ),
    )
    run.add_argument(
        "--peer-wait",
        type=float,
        default=DEFAULT_PEER_WAIT_SECONDS,
        help=(
            "seconds to wait for --peer's advert before reporting it unknown "
            "(default: %(default)s). The run continues receiving either way"
        ),
    )
    run.add_argument(
        "--advert-override-seconds",
        type=float,
        default=None,
        help=(
            "advert faster than the 24 h floor, for a dry run. Requires "
            "--advert-override-expires-in, which is capped at 24 h"
        ),
    )
    run.add_argument(
        "--advert-override-expires-in",
        type=float,
        default=None,
        help="seconds until the advert override auto-reverts to the 24 h floor",
    )
    run.add_argument(
        "--dedup-ttl",
        type=float,
        default=DEFAULT_TTL_SECONDS,
        help="duplicate cache time-to-live in seconds (default: %(default)s)",
    )
    run.add_argument(
        "--dedup-max-entries",
        type=int,
        default=DEFAULT_MAX_ENTRIES,
        help="duplicate cache entry cap (default: %(default)s)",
    )
    run.add_argument(
        "--capture",
        type=Path,
        default=None,
        help=(
            "also write the received frames to this capture file (JSONL), with the "
            "same provenance header `sighop capture` writes. Live sources only"
        ),
    )
    run.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="append wide-event logs here instead of standard output",
    )
    _add_database_url_argument(run)
    run.add_argument(
        "--persist-replay",
        action="store_true",
        help=(
            "let --replay write to the database. WITHOUT THIS FLAG a replay "
            "writes nothing: its receptions carry an earlier session's "
            "timestamps, and storing them would make a week-old contact "
            "indistinguishable from one heard this minute"
        ),
    )

    keys = subparsers.add_parser(
        "keys", help="create and inspect entity identity keyfiles"
    )
    key_actions = keys.add_subparsers(dest="keys_command", required=True)
    keys_new = key_actions.add_parser(
        "new", help="generate an entity keyfile and print its public key"
    )
    keys_new.add_argument("--name", required=True, help="the entity's advertised name")
    keys_new.add_argument(
        "--out", required=True, type=Path, help="keyfile to create (never overwritten)"
    )
    keys_new.add_argument(
        "--node-type",
        default=NodeType.CHAT.name,
        choices=[node_type.name for node_type in NodeType],
        help="the node type the entity adverts as (default: %(default)s)",
    )
    keys_new.add_argument(
        "--burned",
        action="store_true",
        help=(
            "mark the keyfile as a published test vector that must never be used "
            "on air again. For an identity committed to the repository as a "
            "fixture; a real entity generates its own"
        ),
    )
    keys_show = key_actions.add_parser(
        "show", help="print a keyfile's name, node type, public key and node hash"
    )
    keys_show.add_argument("keyfile", type=Path, help="the keyfile to inspect")

    key_actions.add_parser(
        "secret",
        help=(
            "generate a SIGHOP_SECRET_KEY. Printed once: losing it makes every "
            "stored identity unrecoverable"
        ),
    )
    keys_list = key_actions.add_parser(
        "list", help="list the identities in the entity store (needs a database)"
    )
    keys_import = key_actions.add_parser(
        "import", help="store a keyfile's identity, sealing its seed (needs a database)"
    )
    keys_import.add_argument("keyfile", type=Path, help="the keyfile to import")
    keys_export = key_actions.add_parser(
        "export",
        help=(
            "write a stored identity back to a keyfile. The file holds an "
            "unencrypted seed (needs a database)"
        ),
    )
    keys_export.add_argument(
        "reference", help="the stored entity, by exact name or hex public key prefix"
    )
    keys_export.add_argument("path", type=Path, help="keyfile to write (never overwritten)")
    for store_parser in (keys_list, keys_import, keys_export):
        _add_database_url_argument(store_parser)

    database = subparsers.add_parser(
        "db", help="apply and report database migrations (never done by `run`)"
    )
    db_actions = database.add_subparsers(dest="db_command", required=True)
    db_upgrade = db_actions.add_parser(
        "upgrade", help="apply outstanding migrations and print the resulting revision"
    )
    db_current = db_actions.add_parser(
        "current",
        help="print the applied and expected schema revisions and whether they agree",
    )
    for database_parser in (db_upgrade, db_current):
        _add_database_url_argument(database_parser)

    return parser


def _add_database_url_argument(parser: argparse.ArgumentParser) -> None:
    """`--database-url`, overriding `DATABASE_URL` wherever a database is used."""
    parser.add_argument(
        "--database-url",
        default=None,
        metavar="URL",
        help=(
            "database to use, overriding DATABASE_URL from the environment. Must "
            "name the postgresql+asyncpg driver; the password is never printed"
        ),
    )


async def _run_capture(device: str, out: Path, radio_preset: str, log_file: Path) -> int:
    with log_file.open("a", encoding="utf-8") as log_stream:
        configure_logging(log_stream)
        logger = get_logger(component="cli")
        transport = KissTransport(serial_connector(device))
        modem = Modem(transport, radio_params=RADIO_PRESETS[radio_preset])
        run = CaptureRun(modem, out)
        run.install_signal_handlers()
        logger.info(
            "capture_starting",
            device=device,
            out=str(out),
            radio_preset=radio_preset,
            log_file=str(log_file),
        )
        try:
            await run.run()
        except Exception:
            logger.error("capture_failed", device=device, out=str(out))
            raise
        logger.info("capture_stopped")
    return 0


async def _run_monitor_live(
    device: str,
    radio_preset: str,
    capture: Path | None,
    out: IO[str] | None = None,
) -> int:
    """Live monitor: open the link, probe it, then render every frame."""
    transport = KissTransport(serial_connector(device))
    modem = Modem(transport, radio_params=RADIO_PRESETS[radio_preset])

    async def startup() -> str:
        await modem.probe_ready.wait()
        return render_startup(modem.probe_result)

    async def probe_result() -> ProbeResult | None:
        await modem.probe_ready.wait()
        return modem.probe_result

    writer = CaptureWriter(capture) if capture is not None else None
    if writer is not None:
        writer.open()
    try:
        run = MonitorRun(
            modem.events(),
            startup=startup,
            out=out,
            capture_writer=writer,
            capture_probe=probe_result,
            reconnects=lambda: modem.reconnect_count,
            reboots=lambda: modem.reboot_count,
        )
        run.install_signal_handlers()
        await run.run()
    finally:
        if writer is not None:
            writer.close()
    return 0


async def _run_monitor_replay(path: Path, out: IO[str] | None = None) -> int:
    """Replay: no device is opened, and the file's own provenance is the
    startup line — or its absence is, for the headerless milestone 0 captures.
    """
    replay = CaptureReplay.open(path)

    async def startup() -> str:
        return render_replay_startup(replay.provenance, str(path))

    run = MonitorRun(replay.events(), startup=startup, out=out)
    run.install_signal_handlers()
    await run.run()
    if replay.unreadable:
        for line in replay.unreadable:
            print(f"unreadable {line}", file=sys.stderr)
    return 0


def _run_config(
    args: argparse.Namespace, stored: Sequence[LoadedEntity] = ()
) -> RuntimeConfig:
    return RuntimeConfig(
        transmit_enabled=args.enable_transmit,
        status_interval=args.status_interval,
        dedup_ttl_seconds=args.dedup_ttl,
        dedup_max_entries=args.dedup_max_entries,
        ceiling_fraction=args.duty_cycle_ceiling,
        stub_names=tuple(args.stubs),
        advert_override_seconds=args.advert_override_seconds,
        advert_override_expires_in=args.advert_override_expires_in,
        entity_keyfiles=tuple(args.entities),
        stored_entities=tuple(stored),
        replay_persists=bool(getattr(args, "persist_replay", False)),
        peer=args.peer,
        send_text=args.send_text,
        allow_flood=args.allow_flood,
        zero_hop_advert=args.zero_hop_advert,
        peer_wait_seconds=args.peer_wait,
    )


# --- Opening persistence for a run ------------------------------------------


async def open_persistence(
    args: argparse.Namespace, *, replay: bool
) -> tuple[Persistence | None, tuple[LoadedEntity, ...]]:
    """Open the database, check the schema version and load the entities.

    Returns `(None, ())` when no database is configured — which is not an error
    and never falls back silently the other way: a *configured* database that
    cannot be reached, is unauthenticated or is unmigrated raises, because
    falling back would present a running node whose durability an operator had
    already assumed (`database` spec).

    A replay run opens the database but does not write to it unless asked
    (design D13): its receptions carry an earlier session's timestamps, and
    storing them would make a week-old contact indistinguishable from a live one.
    """
    config = Config.from_environment(database_url=args.database_url)
    if config.database is None:
        return None, ()

    persistence = Persistence(
        database=Database(config=config.database),
        writes_enabled=not replay or bool(args.persist_replay),
    )
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
        loaded = await persistence.entities.load_all(
            config.secret_key_bytes(), enabled_only=True
        )
        if isinstance(loaded, Failed):
            raise loaded.error
    except BaseException:
        await persistence.stop()
        raise
    return persistence, tuple(loaded.value)


# --- Key management (design D12) --------------------------------------------


def _keys_new(args: argparse.Namespace, out: IO[str]) -> int:
    """Create a keyfile and print the public key another node's contact list needs."""
    try:
        keyfile = create_keyfile(
            args.out,
            args.name,
            node_type=NodeType[args.node_type],
            burned=args.burned,
        )
    except KeyfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"created {keyfile.path}", file=out)
    print(f"name       {keyfile.name}", file=out)
    print(f"node_type  {NodeType(keyfile.node_type).name}", file=out)
    print(f"public_key {keyfile.public_key.hex()}", file=out)
    print(f"node_hash  0x{keyfile.node_hash:02x}", file=out)
    print(f"!! {PLAINTEXT_SEED_NOTICE}", file=out)
    for warning in keyfile.warnings:
        print(f"!! {warning}", file=out)
    return 0


def _keys_secret(out: IO[str]) -> int:
    """Generate the key entity seeds are sealed under, and say what losing it costs.

    Printed once, from the system CSPRNG, with no passphrase anywhere near it:
    accepting a passphrase invites `hunter2` and then requires an Argon2
    parameter conversation for something no human needs to type (design D4).
    """
    print(f"SIGHOP_SECRET_KEY={generate_secret_key()}", file=out)
    print(
        "!! this is printed once. Losing it makes every stored identity "
        "unrecoverable — that is what encryption at rest means. Put it in the "
        "environment (for example .env.dev, which is gitignored) and back up any "
        "identity you care about with `sighop keys export`",
        file=out,
    )
    return 0


def _keys_list(args: argparse.Namespace, out: IO[str]) -> int:
    """Stored identities. No seed, no ciphertext, and no flag that would print one."""
    database = _database_config(args, out)
    if database is None:
        return 2
    outcome = asyncio.run(_with_store(database, lambda store: store.list_all()))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if not outcome.value:
        print("no identities are stored", file=out)
        return 0
    for record in outcome.value:
        node_type = (
            record.node_type.name
            if isinstance(record.node_type, NodeType)
            else f"type_{record.node_type}"
        )
        print(
            f"{record.name}  type={record.type}  node_type={node_type}  "
            f"public_key={record.public_key.hex()}  "
            f"node_hash=0x{record.node_hash:02x}  "
            f"{'enabled' if record.enabled else 'disabled'}",
            file=out,
        )
    return 0


def _keys_import(args: argparse.Namespace, out: IO[str]) -> int:
    """Milestone 4's promised one-function conversion (its design D1)."""
    database = _database_config(args, out)
    if database is None:
        return 2
    try:
        secret = parse_secret_key(os.environ.get(SECRET_KEY_VARIABLE))
        keyfile = load_keyfile(args.keyfile)
    except (ConfigError, KeyfileError) as exc:
        print(str(exc), file=sys.stderr)
        return 2

    async def store_it(store: EntityRepository) -> Outcome[EntityRecord]:
        existing = await store.get(keyfile.public_key)
        if isinstance(existing, Failed):
            return existing
        if existing.value is not None:
            raise EntityExistsError(
                f"{keyfile.path}: public key {keyfile.public_key.hex()} is already "
                f"stored as entity {existing.value.name!r} "
                f"({existing.value.id}); the stored row is unchanged"
            )
        return await store.store(
            name=keyfile.name,
            identity=keyfile.identity,
            secret=secret,
            node_type=keyfile.node_type,
        )

    try:
        outcome = asyncio.run(_with_store(database, store_it))
    except EntityExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    record = outcome.value
    print(f"imported   {keyfile.path}", file=out)
    print(f"entity_id  {record.id}", file=out)
    print(f"name       {record.name}", file=out)
    print(f"public_key {record.public_key.hex()}", file=out)
    print(f"node_hash  0x{record.node_hash:02x}", file=out)
    print("the seed is sealed under SIGHOP_SECRET_KEY and is not stored in the clear", file=out)
    for warning in keyfile.warnings:
        print(f"!! {warning}", file=out)
    return 0


def _keys_export(args: argparse.Namespace, out: IO[str]) -> int:
    """Deliberate export, which is what §6 asks for. Never a side effect."""
    database = _database_config(args, out)
    if database is None:
        return 2
    try:
        secret = parse_secret_key(os.environ.get(SECRET_KEY_VARIABLE))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    async def load(store: EntityRepository) -> Outcome[list[LoadedEntity]]:
        return await store.load_all(secret)

    try:
        outcome = asyncio.run(_with_store(database, load))
    except (EntityLoadError, SealError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2

    matches = [
        entity
        for entity in outcome.value
        if entity.name == args.reference
        or entity.public_key.hex().startswith(args.reference.removeprefix("0x").lower())
    ]
    if not matches:
        print(f"no stored identity matches {args.reference!r}", file=sys.stderr)
        return 2
    if len(matches) > 1:
        listed = ", ".join(f"{e.name} ({e.public_key.hex()[:16]})" for e in matches)
        print(f"{args.reference!r} matches {len(matches)} identities: {listed}", file=sys.stderr)
        return 2

    entity = matches[0]
    try:
        keyfile = create_keyfile(
            args.path,
            entity.name,
            node_type=entity.record.node_type,
            identity=entity.identity,
        )
    except KeyfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(f"exported   {keyfile.path}", file=out)
    print(f"name       {keyfile.name}", file=out)
    print(f"public_key {keyfile.public_key.hex()}", file=out)
    print(f"node_hash  0x{keyfile.node_hash:02x}", file=out)
    print(f"!! {PLAINTEXT_SEED_NOTICE}", file=out)
    return 0


async def _with_store[T](
    database: DatabaseConfig, work: Callable[[EntityRepository], Awaitable[T]]
) -> T:
    handle = Database(config=database)
    try:
        await handle.open()
        return await work(EntityRepository(database=handle))
    finally:
        await handle.dispose()


def _keys_show(args: argparse.Namespace, out: IO[str]) -> int:
    """Inspect an identity. The seed is not printed, and there is no flag for it."""
    try:
        keyfile = load_keyfile(args.keyfile)
    except KeyfileError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    node_type = (
        keyfile.node_type.name
        if isinstance(keyfile.node_type, NodeType)
        else f"type_{keyfile.node_type}"
    )
    print(f"keyfile    {keyfile.path}", file=out)
    print(f"name       {keyfile.name}", file=out)
    print(f"node_type  {node_type}", file=out)
    print(f"public_key {keyfile.public_key.hex()}", file=out)
    print(f"node_hash  0x{keyfile.node_hash:02x}", file=out)
    for warning in keyfile.warnings:
        print(f"!! {warning}", file=out)
    return 0


# --- Migrations (design D5) -------------------------------------------------
#
# Applying migrations is a deliberate act and never a side effect of `run`: an
# old binary restarted after a failed deploy meets a schema it does not know,
# and a new binary racing another instance applies DDL twice.


def _database_config(args: argparse.Namespace, out: IO[str]) -> DatabaseConfig | None:
    """The configured database, or None with the reason already printed."""
    try:
        config = Config.from_environment(database_url=getattr(args, "database_url", None))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return None
    if config.database is None:
        print(
            "no database is configured: set DATABASE_URL or pass --database-url. "
            "This action needs one; `sighop keys new`/`show` do not",
            file=sys.stderr,
        )
        return None
    return config.database


def _db_upgrade(args: argparse.Namespace, out: IO[str]) -> int:
    database = _database_config(args, out)
    if database is None:
        return 2
    try:
        migrations.upgrade(database)
    except (DatabaseError, MigrationsNotFoundError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:  # the driver's own failures, classified for the operator
        print(str(classify(exc, config=database, operation="upgrade")), file=sys.stderr)
        return 2
    applied = asyncio.run(_read_revision(database))
    print(f"database   {database.redacted_url}", file=out)
    print(f"applied    {applied or '(none)'}", file=out)
    print(f"expected   {migrations.expected_revision()}", file=out)
    return 0


def _db_current(args: argparse.Namespace, out: IO[str]) -> int:
    database = _database_config(args, out)
    if database is None:
        return 2
    expected = migrations.expected_revision()
    try:
        applied = asyncio.run(_read_revision(database))
    except DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    agree = applied == expected
    print(f"database   {database.redacted_url}", file=out)
    print(f"applied    {applied or '(none)'}", file=out)
    print(f"expected   {expected}", file=out)
    print(
        "agree      yes"
        if agree
        else f"agree      NO — reconcile with `{migrations.UPGRADE_COMMAND}`",
        file=out,
    )
    return 0 if agree else 1


async def _read_revision(database: DatabaseConfig) -> str | None:
    handle = Database(config=database)
    try:
        return await handle.read_applied_revision()
    finally:
        await handle.dispose()


async def _run_live(args: argparse.Namespace, out: IO[str] | None = None) -> int:
    """The platform against a live link. The gate is closed unless asked for."""
    transport = KissTransport(serial_connector(args.device))
    modem = Modem(transport, radio_params=RADIO_PRESETS[args.radio_preset])
    events = modem.events()

    async def probe_result() -> ProbeResult | None:
        await modem.probe_ready.wait()
        return modem.probe_result

    persistence, stored = await open_persistence(args, replay=False)
    writer = CaptureWriter(args.capture) if args.capture is not None else None
    if writer is not None:
        writer.open()
    try:
        runtime = Runtime(
            source=events,
            startup=lambda: _live_startup(modem, runtime),
            config=_run_config(args, stored),
            sender=modem,
            out=out,
            capture_writer=writer,
            capture_probe=probe_result,
            persistence=persistence,
        )
        runtime.install_signal_handlers()
        await runtime.run()
    finally:
        if writer is not None:
            writer.close()
    return 0


async def _live_startup(modem: Modem, runtime: Runtime) -> str:
    """Wait for the probe, then adopt the board's own radio readback.

    The readback rather than the configured preset: the budget is enforced
    against what the radio is actually doing, and refuses to guess when the
    board answered nothing.
    """
    await modem.probe_ready.wait()
    probe_result = modem.probe_result
    runtime.set_radio(probe_result.observed_radio if probe_result is not None else None)
    if probe_result is not None:
        await cross_check_airtime(modem, runtime.radio or modem.radio_params)
    return render_startup(probe_result)


async def _run_replay(args: argparse.Namespace, out: IO[str] | None = None) -> int:
    """The same pipeline over recorded frames — no device, no transmission."""
    replay = CaptureReplay.open(args.replay)
    persistence, stored = await open_persistence(args, replay=True)
    config = _run_config(args, stored)
    radio = _replay_radio(replay.provenance) or RADIO_PRESETS[args.radio_preset]

    runtime = Runtime(
        source=replay.events(),
        startup=lambda: _replay_startup(replay, args.replay),
        config=config,
        radio=radio,
        out=out,
        persistence=persistence,
    )
    runtime.install_signal_handlers()
    await runtime.run()
    return 0


async def _replay_startup(replay: CaptureReplay, path: Path) -> str:
    return render_replay_startup(replay.provenance, str(path))


def _replay_radio(provenance: dict | None) -> RadioParams | None:
    """The radio the capture was recorded on, when its header says.

    A replayed run computes airtime against the parameters the frames were
    actually received under, not against today's configuration.
    """
    if not provenance:
        return None
    radio = provenance.get("radio")
    value = radio.get("value") if isinstance(radio, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        return RadioParams(
            freq_hz=int(value["freq_hz"]),
            bw_hz=int(value["bw_hz"]),
            sf=int(value["sf"]),
            cr=int(value["cr"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def main(argv: list[str] | None = None, out: IO[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    stream = out if out is not None else sys.stdout

    if args.command == "keys":
        configure_logging(stream=sys.stderr)
        match args.keys_command:
            case "new":
                return _keys_new(args, stream)
            case "show":
                return _keys_show(args, stream)
            case "secret":
                return _keys_secret(stream)
            case "list":
                return _keys_list(args, stream)
            case "import":
                return _keys_import(args, stream)
            case _:
                return _keys_export(args, stream)

    if args.command == "db":
        configure_logging(stream=sys.stderr)
        if args.db_command == "upgrade":
            return _db_upgrade(args, stream)
        return _db_current(args, stream)

    if args.command == "capture":
        log_file = args.log_file or args.out.with_name(args.out.stem + ".log")
        return asyncio.run(_run_capture(args.device, args.out, args.radio_preset, log_file))

    if args.command == "monitor":
        # Rendered lines own standard output; the wide events go to stderr and
        # to the log file when one is given. Both records of the session
        # exist, and neither is parsed out of the other.
        if args.log_file is not None:
            with args.log_file.open("a", encoding="utf-8") as log_stream:
                configure_logging(log_stream, stream=sys.stderr)
                return asyncio.run(_monitor(args))
        configure_logging(stream=sys.stderr)
        return asyncio.run(_monitor(args))

    if args.command == "run":
        if args.log_file is not None:
            with args.log_file.open("a", encoding="utf-8") as log_stream:
                configure_logging(log_stream, stream=sys.stderr)
                return asyncio.run(_run(args))
        configure_logging(stream=sys.stderr)
        return asyncio.run(_run(args))

    return 0


async def _monitor(args: argparse.Namespace) -> int:
    if args.replay is not None:
        return await _run_monitor_replay(args.replay)
    return await _run_monitor_live(args.device, args.radio_preset, args.capture)


async def _run(args: argparse.Namespace) -> int:
    if args.replay is not None:
        if args.capture is not None:
            # Re-recording a replay would produce a capture whose header
            # describes a probe that never happened. A capture is evidence of a
            # session on the air (DESIGN.md §12), not a copy of a file.
            print(
                "--capture records a live session; it cannot be combined with --replay",
                file=sys.stderr,
            )
            return 2
        run = _run_replay(args)
    else:
        run = _run_live(args)
    try:
        return await run
    except (ConfigError, DatabaseError, EntityLoadError, SealError) as exc:
        # A configured database that cannot be reached, is unauthenticated, is
        # at the wrong revision, or whose seeds will not open is a *startup*
        # failure that applies nothing and transmits nothing (`database` and
        # `entity-store` specs). It is reported here rather than as a traceback,
        # and the message names both revisions or the variable at fault.
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
