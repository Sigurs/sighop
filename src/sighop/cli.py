"""sighop command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import IO, Any

from sighop.bots import drivers as bot_drivers
from sighop.bots.base import BotConfigError, BotMode, UnknownDriverError
from sighop.config import (
    SECRET_KEY_VARIABLE,
    Config,
    ConfigError,
    DatabaseConfig,
    generate_secret_key,
    parse_secret_key,
)
from sighop.db import migrations
from sighop.db.engine import (
    Database,
    DatabaseError,
    Failed,
    Outcome,
    Succeeded,
    classify,
)
from sighop.db.migrations import MigrationsNotFoundError
from sighop.db.persistence import Persistence
from sighop.db.repositories import (
    BOT_ENTITY_TYPE,
    BotExistsError,
    BotRecord,
    EntityExistsError,
    EntityHasRoleError,
    EntityLoadError,
    EntityRecord,
    EntityRepository,
    LoadedEntity,
    RoomExistsError,
    RoomRecord,
    advert_config_for,
)
from sighop.db.sealing import SealError
from sighop.keystore import (
    PLAINTEXT_SEED_NOTICE,
    KeyfileError,
    create_keyfile,
    load_keyfile,
)
from sighop.logging import configure_logging, get_logger
from sighop.monitor.render import (
    render_bot_mode_change,
    render_replay_startup,
    render_startup,
)
from sighop.monitor.run import MonitorRun
from sighop.net.airtime import cross_check_airtime
from sighop.net.dedup import DEFAULT_MAX_ENTRIES, DEFAULT_TTL_SECONDS
from sighop.net.room import POST_SYNC_DELAY_SECS, STORED_POST_TEXT_LEN
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.passwords import hash_password
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
    keys_import.add_argument(
        "--bot",
        action="store_true",
        help=(
            "store the identity as a bot. A bot adverts as an ordinary chat node "
            "— it is a companion to every other node — so only the stored type "
            "tells them apart, and it is an explicit choice here rather than a "
            "side effect of binding a bot to the identity later"
        ),
    )
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

    room = subparsers.add_parser(
        "room",
        help="create and administer rooms on stored room-server identities",
    )
    room_actions = room.add_subparsers(dest="room_command", required=True)

    room_create = room_actions.add_parser(
        "create", help="bind a room to a stored room-server identity"
    )
    room_create.add_argument(
        "identity", help="the stored entity, by exact name or hex public key prefix"
    )
    room_create.add_argument("--name", required=True, help="the room's name")
    room_create.add_argument(
        "--guest-password",
        action="store_true",
        help=(
            "also read a guest password from the prompt or from standard input "
            "after the admin one. Without this and without --open, guest logins "
            "are refused"
        ),
    )
    room_create.add_argument(
        "--open",
        action="store_true",
        dest="guest_open",
        help=(
            "admit any password as a guest, including an empty one. An explicit "
            "choice, never the default (DESIGN.md §7)"
        ),
    )
    room_create.add_argument(
        "--allow-read-only",
        action="store_true",
        help=(
            "admit a sender whose password matched nothing as a read-only "
            "spectator, which may receive history and may not post"
        ),
    )

    room_actions.add_parser("list", help="list the rooms this database holds")

    room_show = room_actions.add_parser("show", help="show one room's configuration")
    room_show.add_argument("room", help="the room, by name")

    room_passwd = room_actions.add_parser(
        "passwd",
        help=(
            "change a room's admin or guest password. Read from a prompt or "
            "from standard input, never from an argument"
        ),
    )
    room_passwd.add_argument("room", help="the room, by name")
    room_passwd.add_argument(
        "--guest",
        action="store_true",
        help="change the guest password rather than the admin one",
    )
    room_passwd.add_argument(
        "--clear",
        action="store_true",
        help="remove the guest password, so guest logins are refused again",
    )
    room_passwd.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from standard input rather than prompting",
    )

    room_members = room_actions.add_parser("members", help="list a room's members")
    room_members.add_argument("room", help="the room, by name")

    room_revoke = room_actions.add_parser(
        "revoke", help="remove a member; it must log in again to return"
    )
    room_revoke.add_argument("room", help="the room, by name")
    room_revoke.add_argument("member", help="the member's hex public key, or a prefix of it")

    room_retention = room_actions.add_parser(
        "retention", help="set or clear a room's retention bounds"
    )
    room_retention.add_argument("room", help="the room, by name")
    room_retention.add_argument(
        "--days", type=int, default=None, metavar="N", help="delete messages older than N days"
    )
    room_retention.add_argument(
        "--messages",
        type=int,
        default=None,
        metavar="N",
        help="keep only the newest N messages",
    )
    room_retention.add_argument(
        "--clear",
        action="store_true",
        help="remove both bounds, returning the room to keeping everything",
    )

    room_post = room_actions.add_parser(
        "post", help="post to a room as the room's own identity"
    )
    room_post.add_argument("room", help="the room, by name")
    room_post.add_argument("text", help="the message text")

    room_history = room_actions.add_parser("history", help="read a room's stored messages")
    room_history.add_argument("room", help="the room, by name")
    room_history.add_argument(
        "--limit", type=int, default=50, help="how many messages to show (default: %(default)s)"
    )

    for room_parser in (
        room_create,
        room_actions.choices["list"],
        room_show,
        room_passwd,
        room_members,
        room_revoke,
        room_retention,
        room_post,
        room_history,
    ):
        _add_database_url_argument(room_parser)

    bot = subparsers.add_parser(
        "bot",
        help="create and administer bots on stored identities",
    )
    bot_actions = bot.add_subparsers(dest="bot_command", required=True)

    bot_create = bot_actions.add_parser(
        "create", help="bind a driver to a stored identity, in observe mode"
    )
    bot_create.add_argument(
        "identity", help="the stored entity, by exact name or hex public key prefix"
    )
    bot_create.add_argument(
        "--driver",
        required=True,
        help=(
            "the driver to run, named explicitly. An unknown name is refused "
            f"with the list of what exists ({', '.join(bot_drivers.driver_names())})"
        ),
    )

    bot_actions.add_parser("list", help="list the bots this database holds")

    bot_show = bot_actions.add_parser(
        "show", help="show one bot's driver, mode, configuration and limits"
    )
    bot_show.add_argument("bot", help="the bot, by the name of the identity it runs on")

    bot_enable = bot_actions.add_parser("enable", help="let a bot run again")
    bot_enable.add_argument("bot", help="the bot, by the name of the identity it runs on")
    bot_disable = bot_actions.add_parser(
        "disable", help="stop a bot running; it stays configured and is reported as disabled"
    )
    bot_disable.add_argument("bot", help="the bot, by the name of the identity it runs on")

    bot_mode = bot_actions.add_parser(
        "mode",
        help=(
            "switch a bot between observe and active. Active is what lets it "
            "transmit at all; the run's --enable-transmit flag still applies"
        ),
    )
    bot_mode.add_argument("bot", help="the bot, by the name of the identity it runs on")
    bot_mode.add_argument(
        "mode", choices=[mode.value for mode in BotMode], help="observe or active"
    )

    bot_set = bot_actions.add_parser(
        "set", help="set one driver configuration value, validated by the driver"
    )
    bot_set.add_argument("bot", help="the bot, by the name of the identity it runs on")
    bot_set.add_argument("key", help="the setting's name")
    bot_set.add_argument("value", help="the value, parsed and checked by the driver")

    bot_greeted = bot_actions.add_parser(
        "greeted",
        help=(
            "inspect or change which contacts a greeter considers already "
            "greeted. Clearing one greets it the next time it adverts"
        ),
    )
    bot_greeted.add_argument("bot", help="the bot, by the name of the identity it runs on")
    bot_greeted.add_argument(
        "peer",
        nargs="?",
        default=None,
        help="one contact, by exact name or hex public key prefix. Omit to list them all",
    )
    bot_greeted.add_argument(
        "--clear",
        action="store_true",
        help="forget this contact's greeting record, so it is greeted when it next adverts",
    )
    bot_greeted.add_argument(
        "--set",
        action="store_true",
        dest="set_greeted",
        help="record this contact as greeted, so it never is",
    )
    bot_greeted.add_argument(
        "--seed",
        action="store_true",
        help=(
            "record every contact currently known as already greeted, without "
            "touching records that already exist. What `bot create` does, for a "
            "greeter whose seeding did not complete"
        ),
    )

    bot_state = bot_actions.add_parser(
        "state", help="inspect or clear a bot's durable state"
    )
    bot_state.add_argument("bot", help="the bot, by the name of the identity it runs on")
    bot_state.add_argument(
        "--clear",
        action="store_true",
        help=(
            "delete everything the bot has stored. For a greeter this discards "
            "its greeting records, so previously greeted nodes may be greeted "
            "again the next time they advert and create a contact"
        ),
    )

    for bot_parser in (
        bot_create,
        bot_actions.choices["list"],
        bot_show,
        bot_enable,
        bot_disable,
        bot_mode,
        bot_set,
        bot_greeted,
        bot_state,
    ):
        _add_database_url_argument(bot_parser)

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

    if args.bot and keyfile.node_type != NodeType.CHAT:
        # A bot is a companion to every other node and adverts as one, so the
        # keyfile has to say so too. Forcing the node type silently would make
        # the stored identity disagree with the file it came from.
        print(
            f"{keyfile.path} adverts as {NodeType(keyfile.node_type).name}; a bot "
            "presents itself to the mesh as a chat node, indistinguishable from a "
            "companion. Create the identity with --node-type CHAT",
            file=sys.stderr,
        )
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
            entity_type=BOT_ENTITY_TYPE if args.bot else None,
            advert_config=advert_config_for(NodeType.CHAT) if args.bot else None,
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
    print(f"type       {record.type}", file=out)
    print(f"node_type  {NodeType(record.node_type).name}", file=out)
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


# --- Rooms (milestone 6) ----------------------------------------------------
#
# One rule here is not a convenience and is enforced by the parser above: **a
# password is never a command-line argument** (design D16). `ps` publishes
# `argv` to every user on the host, and a password that reaches it has been
# disclosed to everyone logged in whether or not anybody looked. Every password
# comes from a prompt or from standard input, and there is no flag that would
# take one otherwise.


ROTATION_EVICTS_NOBODY = (
    "existing members keep their access: membership is keyed on the public key "
    "recorded at first login, so a new password gates only new logins. Removing "
    "a member takes `sighop room revoke`."
)
"""§7's counterintuitive rule, printed at the moment it matters rather than
documented somewhere an operator would have to already suspect it."""

REVOKE_DISCARDS_EVERYTHING = (
    "it must log in again to return, and its sync position and replay guard are "
    "discarded with it — so it receives history from wherever its next login says."
)


async def _with_rooms[T](
    database: DatabaseConfig, work: Callable[[Persistence], Awaitable[T]]
) -> T:
    handle = Database(config=database)
    try:
        await handle.open()
        return await work(Persistence(database=handle))
    finally:
        await handle.dispose()


def _read_password(prompt: str, *, from_stdin: bool) -> str:
    """A password, from standard input or from a prompt that does not echo."""
    if from_stdin or not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    import getpass

    return getpass.getpass(prompt)


def _render_room(record: RoomRecord, *, members: int, messages: int, out: IO[str]) -> None:
    """One room's configuration. Never a password, and never a hash."""
    print(f"room       {record.name}", file=out)
    print(f"room_id    {record.id}", file=out)
    print(f"entity_id  {record.entity_id}", file=out)
    print(f"members    {members}", file=out)
    print(f"messages   {messages}", file=out)
    print(f"guest      {record.guest_access}", file=out)
    print(f"read_only  {'allowed' if record.allow_read_only else 'refused'}", file=out)
    print(f"retention  {record.retention}", file=out)


async def _find_room(persistence: Persistence, name: str) -> Any:
    rooms = await persistence.rooms.list_all()
    if isinstance(rooms, Failed):
        return rooms
    for record in rooms.value:
        if record.name == name:
            return record
    return None


def _room_command(args: argparse.Namespace, out: IO[str]) -> int:
    database = _database_config(args, out)
    if database is None:
        return 2
    match args.room_command:
        case "create":
            return _room_create(args, database, out)
        case "list":
            return _room_list(args, database, out)
        case "show":
            return _room_show(args, database, out)
        case "passwd":
            return _room_passwd(args, database, out)
        case "members":
            return _room_members(args, database, out)
        case "revoke":
            return _room_revoke(args, database, out)
        case "retention":
            return _room_retention(args, database, out)
        case "post":
            return _room_post(args, database, out)
        case _:
            return _room_history(args, database, out)


def _room_create(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    admin = _read_password("admin password: ", from_stdin=False)
    if not admin:
        print(
            "an admin password is required: it is the only credential that admits "
            "an administrator, and §7 asks for a distinct one per room server",
            file=sys.stderr,
        )
        return 2
    guest = (
        _read_password("guest password: ", from_stdin=False)
        if args.guest_password
        else None
    )

    async def work(persistence: Persistence) -> Any:
        entities = EntityRepository(database=persistence.database)
        found = await entities.list_all()
        if isinstance(found, Failed):
            return found
        matches = [
            record
            for record in found.value
            if record.name == args.identity
            or record.public_key.hex().startswith(args.identity.lower())
        ]
        if len(matches) != 1:
            raise EntityLoadError(
                f"{args.identity!r} matches {len(matches)} stored identities; "
                "name one exactly, or give a longer public key prefix"
            )
        entity = matches[0]
        if entity.node_type is not NodeType.ROOM_SERVER:
            raise EntityLoadError(
                f"{entity.name!r} adverts as {entity.node_type!r}, not a room server; "
                "create the identity with --node-type ROOM_SERVER, because being a "
                "room server is an explicit choice and never a side effect of "
                "having a room bound to it"
            )
        return await persistence.rooms.create(
            entity_id=entity.id,
            name=args.name,
            admin_password_hash=hash_password(admin),
            guest_password_hash=None if guest is None else hash_password(guest),
            guest_open=args.guest_open,
            allow_read_only=args.allow_read_only,
        )

    try:
        outcome = asyncio.run(_with_rooms(database, work))
    except (RoomExistsError, EntityLoadError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    _render_room(outcome.value, members=0, messages=0, out=out)
    print("the passwords are stored as Argon2id hashes and cannot be recovered", file=out)
    return 0


def _room_list(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        rooms = await persistence.rooms.list_all()
        if isinstance(rooms, Failed):
            return rooms
        rows = []
        for record in rooms.value:
            members = await persistence.members.load_for_room(record.id)
            messages = await persistence.messages.count(record.id)
            rows.append(
                (
                    record,
                    len(members.value) if isinstance(members, Succeeded) else 0,
                    messages.value if isinstance(messages, Succeeded) else 0,
                )
            )
        return rows

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if not outcome:
        print("no rooms are configured", file=out)
        return 0
    for record, members, messages in outcome:
        print(
            f"{record.name}  entity={record.entity_id}  members={members}  "
            f"messages={messages}  guest={record.guest_access}  "
            f"retention={record.retention}",
            file=out,
        )
    return 0


def _room_show(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        members = await persistence.members.load_for_room(record.id)
        messages = await persistence.messages.count(record.id)
        return (
            record,
            len(members.value) if isinstance(members, Succeeded) else 0,
            messages.value if isinstance(messages, Succeeded) else 0,
        )

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    record, members, messages = outcome
    _render_room(record, members=members, messages=messages, out=out)
    return 0


def _room_passwd(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    which = "guest" if args.guest else "admin"
    if args.clear and not args.guest:
        print(
            "the admin password cannot be cleared: a room with no admin password "
            "has no administrator and no way to gain one",
            file=sys.stderr,
        )
        return 2
    password = (
        None
        if args.clear
        else _read_password(f"{which} password: ", from_stdin=args.password_stdin)
    )

    async def work(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        hashed = None if password is None else hash_password(password)
        return await persistence.rooms.set_passwords(
            record.id,
            admin_password_hash=None if args.guest else hashed,
            guest_password_hash=hashed if args.guest else None,
            clear_guest_password=args.clear and args.guest,
        )

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if args.clear:
        print("the guest password is removed; guest logins are refused again", file=out)
    else:
        print(f"the {which} password is changed and stored as an Argon2id hash", file=out)
    print(ROTATION_EVICTS_NOBODY, file=out)
    return 0


def _room_members(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        return await persistence.members.load_for_room(record.id)

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if not outcome.value:
        print("no members have logged in", file=out)
        return 0
    for member in outcome.value:
        print(
            f"{member.public_key.hex()}  node_hash=0x{member.node_hash:02x}  "
            f"{member.permission.name.lower()}  sync_since={member.sync_since}  "
            f"last_activity={member.last_activity.isoformat()}",
            file=out,
        )
    return 0


def _room_revoke(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        members = await persistence.members.load_for_room(record.id)
        if isinstance(members, Failed):
            return members
        wanted = args.member.lower()
        matches = [
            member
            for member in members.value
            if member.public_key.hex().startswith(wanted)
        ]
        if len(matches) != 1:
            return matches
        removed = await persistence.members.delete(record.id, matches[0].public_key)
        return (matches[0], removed)

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if isinstance(outcome, list):
        print(
            f"{args.member!r} matches {len(outcome)} members; give a longer prefix",
            file=sys.stderr,
        )
        return 2
    member, removed = outcome
    if isinstance(removed, Failed):
        print(str(removed.error), file=sys.stderr)
        return 2
    print(f"revoked {member.public_key.hex()}", file=out)
    print(REVOKE_DISCARDS_EVERYTHING, file=out)
    return 0


def _room_retention(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    days = None if args.clear else args.days
    messages = None if args.clear else args.messages

    async def work(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        applied = await persistence.rooms.set_retention(
            record.id, retention_days=days, retention_messages=messages
        )
        if isinstance(applied, Failed):
            return applied
        return await _find_room(persistence, args.room)

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    print(f"retention  {outcome.retention}", file=out)
    if outcome.retention == "unlimited":
        print("nothing will be deleted, however old or numerous", file=out)
    else:
        print(
            "messages beyond the policy are removed by a periodic task. A member "
            "whose sync position predates what is removed misses those messages, "
            "and the count is reported when it happens",
            file=out,
        )
    return 0


def _room_post(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    text = args.text.encode("utf-8")
    if len(text) > STORED_POST_TEXT_LEN:
        print(
            f"the post is {len(text)} bytes and a room keeps "
            f"{STORED_POST_TEXT_LEN}, which is all a stock client can show; "
            "shorten it and post again. A post arriving over the air is "
            "truncated instead, because its author cannot be told; you can be",
            file=sys.stderr,
        )
        return 2

    async def work(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        entities = EntityRepository(database=persistence.database)
        rows = await entities.list_all()
        if isinstance(rows, Failed):
            return rows
        author = next(
            (row.public_key for row in rows.value if row.id == record.entity_id), None
        )
        if author is None:  # pragma: no cover - the FK makes this unreachable
            return None
        return await persistence.messages.store(
            room_id=record.id, author_public_key=author, text=text
        )

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    post = outcome.value
    print(f"posted     @{post.post_timestamp}", file=out)
    print(f"author     {post.author_public_key.hex()}  (the room's own identity)", file=out)
    print(
        "it becomes deliverable to every member after the reference "
        f"implementation's {POST_SYNC_DELAY_SECS}s hold",
        file=out,
    )
    return 0


def _room_history(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        return await persistence.messages.history(record.id, limit=args.limit)

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if not outcome.value:
        print("the room holds no messages", file=out)
        return 0
    for post in outcome.value:
        # §4.1 at the display edge: what is shown is a *rendering* of the bytes,
        # and text that is not valid UTF-8 says so rather than passing our guess
        # off as the author's words.
        rendered = post.rendered
        text = (
            repr(rendered.text)
            if rendered.is_valid_utf8
            else f"{rendered.text!r} (rendering of {len(post.text)} bytes, not valid UTF-8)"
        )
        print(
            f"@{post.post_timestamp}  {post.author_public_key.hex()[:16]}  {text}",
            file=out,
        )
    return 0


# --- Bots (milestone 7) -----------------------------------------------------
#
# The `sighop room` shape, deliberately (design D3), with one rule of its own:
# **every output that changes what a bot may do says what it may now do.**
# Creating one says it will transmit nothing until it is made active; making it
# active says the run's transmit flag still applies; clearing its state says
# what the bot will do again. A safety posture an operator has to infer is a
# safety posture that gets inferred wrongly.

TRANSMIT_STILL_GATED = (
    "transmission still requires the run's --enable-transmit flag, and stays "
    "under the duty-cycle ceiling"
)

CLEARING_STATE_FORGETS = (
    "the bot has forgotten everything it recorded, the seed included. A greeter "
    "will greet a previously greeted node again the next time it adverts"
)

SEEDING_IS_WHAT_A_NEW_GREETER_OWES = (
    "recorded as already greeted, so this greeter starts owing nothing to the "
    "contacts this node already knew. Release one with `sighop bot greeted "
    "{name} <peer> --clear`"
)
"""Design D7: the debt is data an operator can read and edit, rather than a gate
that also refuses the cases the gate was never meant to refuse. Printed at the
moment it is incurred, because a seeded contact and a greeted one are
indistinguishable from the outside afterwards."""


async def _seed_greeted(persistence: Persistence, record: BotRecord) -> Outcome[int]:
    """Record every contact this node already knows as already greeted.

    Read through `ContactRepository` rather than the runtime's store, because
    this runs from the command line with no runtime: the durable table *is* the
    platform's memory of the mesh, which is exactly what is being seeded from.
    """
    from sighop.bots.greeter import SEEDED, greeted_key

    contacts = await persistence.contacts.load_all()
    if isinstance(contacts, Failed):
        return contacts
    entries: dict[str, object] = {
        greeted_key(contact.public_key): {
            "outcome": SEEDED,
            "at": _now_iso(),
            "name": contact.display_name,
        }
        for contact in contacts.value
    }
    return await persistence.bot_state.set_many(record.id, entries)


def _now_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat()


def _render_bot(record: BotRecord, out: IO[str]) -> None:
    """One bot's configuration. No key material appears here and none can:
    nothing on this path has ever held a seed or its ciphertext."""
    print(f"bot        {record.entity_name}", file=out)
    print(f"bot_id     {record.id}", file=out)
    print(f"entity_id  {record.entity_id}", file=out)
    print(f"driver     {record.driver}", file=out)
    print(f"mode       {record.mode}", file=out)
    print(f"enabled    {'yes' if record.enabled else 'no'}", file=out)
    for key, value in sorted(record.config.items()):
        print(f"  {key:<14} {'none' if value is None else value}", file=out)


async def _find_bot(persistence: Persistence, name: str) -> Any:
    found = await persistence.bots.get_by_name(name)
    if isinstance(found, Failed):
        return found
    return found.value


def _bot_command(args: argparse.Namespace, out: IO[str]) -> int:
    database = _database_config(args, out)
    if database is None:
        return 2
    match args.bot_command:
        case "create":
            return _bot_create(args, database, out)
        case "list":
            return _bot_list(args, database, out)
        case "show":
            return _bot_show(args, database, out)
        case "enable" | "disable":
            return _bot_enablement(args, database, out)
        case "mode":
            return _bot_mode(args, database, out)
        case "set":
            return _bot_set(args, database, out)
        case "greeted":
            return _bot_greeted(args, database, out)
        case _:
            return _bot_state(args, database, out)


def _bot_create(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    try:
        config = bot_drivers.default_config(args.driver)
    except UnknownDriverError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    async def work(persistence: Persistence) -> Any:
        entities = EntityRepository(database=persistence.database)
        found = await entities.list_all()
        if isinstance(found, Failed):
            return found
        matches = [
            record
            for record in found.value
            if record.name == args.identity
            or record.public_key.hex().startswith(args.identity.lower())
        ]
        if len(matches) != 1:
            raise EntityLoadError(
                f"{args.identity!r} matches {len(matches)} stored identities; "
                "name one exactly, or give a longer public key prefix"
            )
        entity = matches[0]
        if entity.type != BOT_ENTITY_TYPE:
            raise EntityHasRoleError(
                f"{entity.name!r} is stored as {entity.type!r}, not a bot; import "
                "the identity with `sighop keys import --bot`, because being a "
                "bot is an explicit choice and never a side effect of having a "
                "bot bound to it"
            )
        created = await persistence.bots.create(
            entity_id=entity.id,
            driver=args.driver,
            config=config,
            entity_name=entity.name,
        )
        if isinstance(created, Failed):
            return created
        # Seeded after the row exists, because `bot_state` has a foreign key to
        # it. A seed that fails leaves a bot owing the whole contact table, so
        # the failure is loud and the remedy is named (design D7).
        seeded = await _seed_greeted(persistence, created.value)
        return created.value, seeded

    try:
        outcome = asyncio.run(_with_rooms(database, work))
    except (BotExistsError, EntityHasRoleError, EntityLoadError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    record, seeded = outcome
    _render_bot(record, out)
    print(
        "the bot is enabled and in observe mode: it runs its whole decision path "
        f"and will transmit nothing until `sighop bot mode {record.entity_name} "
        "active` says otherwise",
        file=out,
    )
    print(TRANSMIT_STILL_GATED, file=out)
    if isinstance(seeded, Failed):
        print(
            f"the bot was created, but seeding its greeting records failed: "
            f"{seeded.error}. It currently owes a greeting to every contact this "
            f"node knows — do not make it active until `sighop bot greeted "
            f"{record.entity_name} --seed` succeeds",
            file=sys.stderr,
        )
        return 2
    if seeded.value:
        print(
            f"seeded     {seeded.value} existing contacts "
            + SEEDING_IS_WHAT_A_NEW_GREETER_OWES.format(name=record.entity_name),
            file=out,
        )
    else:
        print(
            "seeded     nothing — this node knows no contacts yet, so every node "
            "it hears from here on is one this greeter has said nothing to",
            file=out,
        )
    return 0


def _bot_list(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    outcome = asyncio.run(_with_rooms(database, lambda p: p.bots.list_all()))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if not outcome.value:
        print("no bots are configured", file=out)
        return 0
    for record in outcome.value:
        print(
            f"{record.entity_name}  driver={record.driver}  mode={record.mode}  "
            f"{'enabled' if record.enabled else 'disabled'}  entity={record.entity_id}",
            file=out,
        )
    return 0


def _bot_show(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        record = await _find_bot(persistence, args.bot)
        if record is None or isinstance(record, Failed):
            return record
        state = await persistence.bot_state.list(record.id)
        return record, len(state.value) if isinstance(state, Succeeded) else 0

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no bot named {args.bot!r}", file=sys.stderr)
        return 2
    record, keys = outcome
    _render_bot(record, out)
    print(f"state_keys {keys}", file=out)
    # Counters are per run and live in the running process; a bot that has never
    # run has none, and saying so beats printing zeros that look like facts.
    print(
        "counters are reported by the run itself (`sighop run` status lines); "
        "nothing is persisted per run",
        file=out,
    )
    return 0


def _bot_enablement(
    args: argparse.Namespace, database: DatabaseConfig, out: IO[str]
) -> int:
    enabled = args.bot_command == "enable"

    async def work(persistence: Persistence) -> Any:
        record = await _find_bot(persistence, args.bot)
        if record is None or isinstance(record, Failed):
            return record
        changed = await persistence.bots.set_enabled(record.id, enabled)
        if isinstance(changed, Failed):
            return changed
        return record

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no bot named {args.bot!r}", file=sys.stderr)
        return 2
    if enabled:
        print(
            f"bot {outcome.entity_name!r} is enabled and will run, in "
            f"{outcome.mode} mode",
            file=out,
        )
        return 0
    print(
        f"bot {outcome.entity_name!r} is disabled: it stays configured, receives "
        "no events, and every run reports it as not running with that reason",
        file=out,
    )
    return 0


def _bot_mode(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    mode = BotMode(args.mode)

    async def work(persistence: Persistence) -> Any:
        record = await _find_bot(persistence, args.bot)
        if record is None or isinstance(record, Failed):
            return record
        changed = await persistence.bots.set_mode(record.id, str(mode))
        if isinstance(changed, Failed):
            return changed
        return record

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no bot named {args.bot!r}", file=sys.stderr)
        return 2
    print(render_bot_mode_change(outcome.entity_name, mode), file=out)
    return 0


def _bot_set(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    """Hand the value to whoever owns the setting, and store nothing on refusal."""

    async def work(persistence: Persistence) -> Any:
        record = await _find_bot(persistence, args.bot)
        if record is None or isinstance(record, Failed):
            return record
        # Validated against the driver named by the *stored* row, so the check
        # is the one the bot will actually run under.
        value = bot_drivers.validate_config(record.driver, args.key, args.value)
        config = {**record.config, args.key: value}
        changed = await persistence.bots.set_config(record.id, config)
        if isinstance(changed, Failed):
            return changed
        return await _find_bot(persistence, args.bot)

    try:
        outcome = asyncio.run(_with_rooms(database, work))
    except (BotConfigError, UnknownDriverError) as exc:
        # Refused before anything was written: the stored configuration is
        # exactly what it was (design D14).
        print(str(exc), file=sys.stderr)
        print("the stored configuration is unchanged", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no bot named {args.bot!r}", file=sys.stderr)
        return 2
    _render_bot(outcome, out)
    return 0


def _greeting_state(record: dict) -> str:
    """One greeting record, as something an operator can act on.

    The attempt count is shown for a record that is *not* settled, because that
    is the only case where it changes what happens next: an unacknowledged
    greeting is retried, and how many have already gone tells an operator
    whether the peer is about to be left alone.
    """
    from sighop.bots.greeter import SETTLED

    outcome = str(record.get("outcome", "unknown"))
    if outcome in SETTLED:
        return outcome
    attempts = record.get("attempts", 0)
    return f"{outcome} after {attempts} attempt(s)"


async def _resolve_contact(persistence: Persistence, reference: str) -> Any:
    """One contact by exact name or hex key prefix, from the durable table.

    Through `ContactStore.resolve` rather than a query, so the command line
    resolves a peer exactly as a run does — including the ambiguity error that
    lists every match, which a `LIKE` would have to reinvent worse.
    """
    from sighop.net.contacts import ContactStore

    loaded = await persistence.contacts.load_all()
    if isinstance(loaded, Failed):
        return loaded
    store = ContactStore()
    store.restore(loaded.value)
    return store.resolve(reference)


def _bot_greeted(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    """Who this greeter considers already greeted, and the operator's say in it.

    The greeting record is the whole gate (design D7), so this command is the
    whole of the policy an operator can turn: releasing one contact is one
    command and one contact, which is the granularity a decision to message a
    stranger deserves.
    """
    from sighop.bots.greeter import OPERATOR, greeted_key, greeted_public_key
    from sighop.net.contacts import ContactError

    if args.clear and args.set_greeted:
        print(
            "--clear and --set say opposite things about the same contact",
            file=sys.stderr,
        )
        return 2

    async def work(persistence: Persistence) -> Any:
        record = await _find_bot(persistence, args.bot)
        if record is None or isinstance(record, Failed):
            return record

        if args.seed:
            seeded = await _seed_greeted(persistence, record)
            return ("seeded", record, seeded)

        if args.peer is None:
            listed = await persistence.bot_state.list(record.id)
            if isinstance(listed, Failed):
                return listed
            return ("list", record, listed.value)

        contact = await _resolve_contact(persistence, args.peer)
        if isinstance(contact, Failed):
            return contact
        key = greeted_key(contact.public_key)
        if args.clear:
            cleared = await persistence.bot_state.delete(record.id, key)
            if isinstance(cleared, Failed):
                return cleared
            return ("clear", record, (contact, cleared.value))
        if args.set_greeted:
            written = await persistence.bot_state.set(
                record.id,
                key,
                {"outcome": OPERATOR, "at": _now_iso(), "name": contact.display_name},
            )
            if isinstance(written, Failed):
                return written
            return ("set", record, contact)
        found = await persistence.bot_state.get(record.id, key)
        if isinstance(found, Failed):
            return found
        return ("show", record, (contact, found.value))

    try:
        outcome = asyncio.run(_with_rooms(database, work))
    except ContactError as exc:
        # Unknown or ambiguous: the store's own message lists every match, which
        # is what an operator needs to give a longer prefix.
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no bot named {args.bot!r}", file=sys.stderr)
        return 2

    action, record, payload = outcome
    match action:
        case "seeded":
            print(
                f"seeded {payload.value} contacts for bot {record.entity_name!r}; "
                "records that already existed were left alone",
                file=out,
            )
        case "list":
            greetings = {
                key: value
                for key, value in payload.items()
                if greeted_public_key(key) is not None
            }
            if not greetings:
                print(
                    f"bot {record.entity_name!r} has greeted nobody and owes a "
                    "greeting to every contact it hears from",
                    file=out,
                )
                return 0
            for key, value in sorted(greetings.items()):
                public_key = greeted_public_key(key)
                assert public_key is not None
                rendered = value if isinstance(value, dict) else {}
                print(
                    f"{public_key.hex()[:16]}  {_greeting_state(rendered)}  "
                    f"{rendered.get('name', '')}",
                    file=out,
                )
        case "clear":
            contact, removed = payload
            if not removed:
                print(
                    f"{contact.display_name} ({contact.public_key.hex()[:16]}) had no "
                    "greeting record; it is already eligible to be greeted",
                    file=out,
                )
                return 0
            print(
                f"cleared the greeting record for {contact.display_name} "
                f"({contact.public_key.hex()[:16]}): bot {record.entity_name!r} will "
                "greet it the next time it adverts, if the hop, node-type and rate "
                "gates pass",
                file=out,
            )
        case "set":
            print(
                f"{payload.display_name} ({payload.public_key.hex()[:16]}) is recorded "
                f"as greeted by an operator: bot {record.entity_name!r} will never "
                "greet it",
                file=out,
            )
        case _:
            contact, value = payload
            if value is None:
                print(
                    f"{contact.display_name} ({contact.public_key.hex()[:16]}) has no "
                    f"greeting record from bot {record.entity_name!r} and will be "
                    "greeted when it next adverts",
                    file=out,
                )
                return 0
            rendered = value if isinstance(value, dict) else {}
            print(
                f"{contact.display_name} ({contact.public_key.hex()[:16]})  "
                f"{_greeting_state(rendered)}  at {rendered.get('at', '?')}",
                file=out,
            )
    return 0


def _bot_state(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        record = await _find_bot(persistence, args.bot)
        if record is None or isinstance(record, Failed):
            return record
        if args.clear:
            cleared = await persistence.bot_state.clear(record.id)
            if isinstance(cleared, Failed):
                return cleared
            return record, cleared.value, {}
        listed = await persistence.bot_state.list(record.id)
        if isinstance(listed, Failed):
            return listed
        return record, None, listed.value

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no bot named {args.bot!r}", file=sys.stderr)
        return 2
    record, cleared, state = outcome
    if cleared is not None:
        print(f"cleared {cleared} keys from bot {record.entity_name!r}", file=out)
        print(CLEARING_STATE_FORGETS, file=out)
        return 0
    if not state:
        print(f"bot {record.entity_name!r} has stored nothing", file=out)
        return 0
    for key, value in sorted(state.items()):
        print(f"{key}  {value}", file=out)
    return 0


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
    # Adopted before the radio, so `set_radio` can hand a room server the
    # telemetry the board actually answered with (design D14).
    runtime.probe_result = probe_result
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

    if args.command == "room":
        configure_logging(stream=sys.stderr)
        return _room_command(args, stream)

    if args.command == "bot":
        configure_logging(stream=sys.stderr)
        return _bot_command(args, stream)

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
