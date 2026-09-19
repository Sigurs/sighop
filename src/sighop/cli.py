"""sighop command-line entry point."""

from __future__ import annotations

import argparse
import asyncio
import base64
import secrets
import sys
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import Path
from typing import IO, Any

from sighop.bots import drivers as bot_drivers
from sighop.bots.base import BotConfigError, BotMode, UnknownDriverError
from sighop.config import (
    Config,
    ConfigError,
    DatabaseConfig,
    generate_secret_key,
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
    GUESSABLE_STATEMENT,
    PUBLIC_CHANNEL_NAME,
    BotExistsError,
    BotRecord,
    ChannelConfigError,
    ChannelRecord,
    EntityExistsError,
    EntityHasRoleError,
    EntityLoadError,
    EntityNameError,
    EntityRecord,
    EntityRepository,
    EntityRoleError,
    LoadedEntity,
    OpenedEntities,
    RoomExistsError,
    RoomNameError,
    RoomNameTakenError,
    RoomRecord,
    UsernameError,
    WebhookRecord,
    WebUserExistsError,
    advert_config_for,
    normalise_username,
)
from sighop.db.sealing import SealError
from sighop.keystore import (
    PLAINTEXT_KEY_NOTICE,
    Keyfile,
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
from sighop.net.channels import ChannelKind, ChannelMessageRecord, ChannelOutcome
from sighop.net.dedup import DEFAULT_MAX_ENTRIES, DEFAULT_TTL_SECONDS
from sighop.net.room import POST_SYNC_DELAY_SECS, STORED_POST_TEXT_LEN
from sighop.net.tx import DEFAULT_CEILING_FRACTION
from sighop.passwords import hash_password
from sighop.protocol.identity import (
    LocalIdentity,
    PrivateKeyError,
    private_key_from_hex,
    refuse_unusable_node_hash,
)
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

# The one module that knows both sides of milestone 8's seam (design D2).
# `runtime.py` imports nothing from `web/` and `web/` imports nothing from
# `runtime.py`; this is where a `Runtime` becomes the panel's `PanelState` and
# the interface becomes one of the run's services.
from sighop.web.app import (
    DEFAULT_WEB_HOST,
    DEFAULT_WEB_PORT,
    NO_DATABASE_FOR_WEB,
    NO_ENABLED_ACCOUNT,
    WebBindError,
    WebInterface,
    WebStartupError,
    validate_allowed_hosts,
)
from sighop.web.auth import Authenticator, FirstRunSetup
from sighop.web.chat import ChannelLog, ConversationLog
from sighop.web.feed import FeedHub
from sighop.webhooks.config import (
    FIRST_RUN_BURST,
    PLAINTEXT_HTTP_WARNING,
    WebhookConfigError,
    WebhookFormat,
)
from sighop.webhooks.dispatcher import send_sample
from sighop.webhooks.triggers import Trigger

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
    run.add_argument(
        "--web",
        action="store_true",
        help=(
            "serve the web panel from inside this run. WITHOUT THIS FLAG no "
            "socket is listened on. Every page requires signing in with an "
            "account from `sighop web user add`, so this needs a database holding "
            "at least one enabled account"
        ),
    )
    run.add_argument(
        "--web-host",
        default=DEFAULT_WEB_HOST,
        metavar="ADDRESS",
        help=(
            "address the panel listens on (default: %(default)s). A "
            "non-loopback address is permitted and is announced at startup as "
            "reachable from the network over plain HTTP, with passwords and "
            "session cookies unencrypted in transit"
        ),
    )
    run.add_argument(
        "--web-port",
        type=int,
        default=DEFAULT_WEB_PORT,
        help="port the panel listens on (default: %(default)s); 0 asks the OS",
    )
    run.add_argument(
        "--web-allowed-host",
        action="append",
        default=[],
        metavar="NAME[:PORT]",
        help=(
            "another host name the panel answers to, beside those the bound "
            "address implies — for example localhost:8080 for a panel bound to "
            "0.0.0.0 inside a container. Repeatable. No wildcards; every other "
            "host name is refused as a rebinding attempt"
        ),
    )
    _add_database_url_argument(run)
    run.add_argument(
        "--migrate",
        action="store_true",
        help=(
            "apply outstanding migrations before starting, as `sighop db upgrade` "
            "would. Meant for the container, whose start is the deploy; without it "
            "a database at an older revision refuses to start, naming that command. "
            "A database ahead of this build refuses either way"
        ),
    )
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

    keys = subparsers.add_parser("keys", help="create and inspect entity identity keyfiles")
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
    keys_new.add_argument(
        "--private-key",
        default=None,
        metavar="HEX",
        help=(
            "write a keyfile for an identity you already hold, instead of "
            "generating one: the 64-byte private key a MeshCore device stores, as "
            "128 hex characters. A 32-byte seed is not accepted"
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
        "import",
        help=("store an identity from a keyfile or a private key, sealing it (needs a database)"),
    )
    # One of the two, never both (design D8). Not argparse's mutually exclusive
    # group: `keyfile` is positional, so the group's own message would talk
    # about an option and an argument rather than about two ways to name an
    # identity. `_keys_import` says it in those terms instead.
    keys_import.add_argument(
        "keyfile",
        type=Path,
        nargs="?",
        default=None,
        help="the keyfile to import",
    )
    keys_import.add_argument(
        "--private-key",
        default=None,
        metavar="HEX",
        help=(
            "import an identity you already hold instead of a keyfile: the "
            "64-byte private key a MeshCore device stores, as 128 hex characters. "
            "Needs --name. Nothing is written to disk"
        ),
    )
    keys_import.add_argument(
        "--name",
        default=None,
        help="the entity's advertised name; required with --private-key",
    )
    keys_import.add_argument(
        "--node-type",
        default=NodeType.CHAT.name,
        choices=[node_type.name for node_type in NodeType],
        help=(
            "the node type a --private-key identity adverts as (default: "
            "%(default)s). A keyfile carries its own"
        ),
    )
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
            "unencrypted private key (needs a database)"
        ),
    )
    keys_export.add_argument(
        "reference", help="the stored entity, by exact name or hex public key prefix"
    )
    keys_export.add_argument("path", type=Path, help="keyfile to write (never overwritten)")
    keys_rename = key_actions.add_parser(
        "rename",
        help=(
            "change a stored identity's name. The name travels in the "
            "identity's adverts and in every channel post it makes"
        ),
    )
    keys_rename.add_argument(
        "reference", help="the stored entity, by exact name or hex public key prefix"
    )
    keys_rename.add_argument("name", help="the new name")

    keys_delete = key_actions.add_parser(
        "delete",
        help=(
            "remove a stored identity. Irreversible, refused for an identity a "
            "room or bot is bound to (needs a database)"
        ),
    )
    keys_delete.add_argument(
        "reference", help="the stored entity, by exact name or hex public key prefix"
    )
    keys_delete.add_argument(
        "--delete-key",
        action="store_true",
        help=(
            "accept losing this identity's stored key material, in place of "
            "confirming at a terminal. Needed when there is no terminal"
        ),
    )
    for store_parser in (keys_list, keys_import, keys_export, keys_rename, keys_delete):
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

    room_rename = room_actions.add_parser(
        "rename",
        help="change a room's name. A local label: it is never advertised",
    )
    room_rename.add_argument("room", help="the room, by name")
    room_rename.add_argument("name", help="the new name")

    room_delete = room_actions.add_parser(
        "delete",
        help=(
            "delete a room, with its members and its whole history. The "
            "identity it is bound to is kept, unbound"
        ),
    )
    room_delete.add_argument("room", help="the room, by name")
    room_delete.add_argument(
        "--delete-history",
        action="store_true",
        help=(
            "accept the loss without being asked, for use where there is no terminal to confirm at"
        ),
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

    room_post = room_actions.add_parser("post", help="post to a room as the room's own identity")
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
        room_rename,
        room_delete,
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

    bot_delete = bot_actions.add_parser(
        "delete",
        help=(
            "delete a bot and everything it has stored. The identity it is "
            "bound to is kept, unbound. To rename a bot, rename that identity "
            "with `sighop keys rename`: a bot has no name of its own"
        ),
        description=(
            "Delete a bot and everything it has stored. The identity it is bound "
            "to is kept, unbound, and can carry a new bot or be removed with "
            "`sighop keys delete`.\n\n"
            "There is no `sighop bot rename`: a bot has no name of its own and is "
            "named by the identity it runs on, so `sighop keys rename` is what "
            "renames one."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    bot_delete.add_argument("bot", help="the bot, by the name of the identity it runs on")
    bot_delete.add_argument(
        "--delete-state",
        action="store_true",
        help=(
            "accept the loss without being asked, for use where there is no terminal to confirm at"
        ),
    )

    bot_state = bot_actions.add_parser("state", help="inspect or clear a bot's durable state")
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
        bot_delete,
        bot_state,
    ):
        _add_database_url_argument(bot_parser)

    webhook = subparsers.add_parser(
        "webhook",
        help=(
            "add, inspect, change, test and remove the webhooks told about new "
            "repeaters and companions (needs a database)"
        ),
    )
    webhook_actions = webhook.add_subparsers(dest="webhook_command", required=True)
    trigger_names = ", ".join(trigger.value for trigger in Trigger)

    webhook_add = webhook_actions.add_parser(
        "add",
        help=("add an enabled webhook. " + URL_IS_READ_FROM_STDIN),
        description=URL_IS_READ_FROM_STDIN,
    )
    webhook_add.add_argument("name", help="a name for the webhook, unique, no whitespace")
    webhook_add.add_argument(
        "--format",
        required=True,
        choices=[kind.value for kind in WebhookFormat],
        help="json: sighop's documented event; discord: a Discord webhook message",
    )
    webhook_add.add_argument(
        "--trigger",
        action="append",
        required=True,
        dest="triggers",
        metavar="TRIGGER",
        help=f"an event to send; repeat for several ({trigger_names})",
    )
    webhook_add.add_argument(
        "--max-hops",
        type=int,
        default=None,
        help="send only for receptions of at most this many hops (default: no limit)",
    )

    webhook_actions.add_parser("list", help="list the webhooks, targets as scheme and host")

    webhook_show = webhook_actions.add_parser(
        "show", help="show one webhook, with its last successful and failed delivery"
    )
    webhook_show.add_argument("name", help="the webhook's name")
    webhook_enable = webhook_actions.add_parser("enable", help="deliver to a webhook again")
    webhook_enable.add_argument("name", help="the webhook's name")
    webhook_disable = webhook_actions.add_parser(
        "disable", help="stop delivering to a webhook; it stays configured"
    )
    webhook_disable.add_argument("name", help="the webhook's name")

    webhook_set = webhook_actions.add_parser(
        "set", help="change a webhook's format, triggers or hop limit"
    )
    webhook_set.add_argument("name", help="the webhook's name")
    webhook_set.add_argument(
        "--format", choices=[kind.value for kind in WebhookFormat], default=None
    )
    webhook_set.add_argument(
        "--trigger",
        action="append",
        dest="triggers",
        metavar="TRIGGER",
        default=None,
        help=f"replace the triggers; repeat for several ({trigger_names})",
    )
    hops = webhook_set.add_mutually_exclusive_group()
    hops.add_argument("--max-hops", type=int, default=None, help="set the hop limit")
    hops.add_argument("--no-max-hops", action="store_true", help="remove the hop limit")

    webhook_set_url = webhook_actions.add_parser(
        "set-url",
        help="replace a webhook's URL. " + URL_IS_READ_FROM_STDIN,
        description=URL_IS_READ_FROM_STDIN,
    )
    webhook_set_url.add_argument("name", help="the webhook's name")

    webhook_rename = webhook_actions.add_parser(
        "rename", help="change a webhook's name. Its target, format and triggers are untouched"
    )
    webhook_rename.add_argument("name", help="the webhook's name")
    webhook_rename.add_argument("new_name", help="the new name")

    webhook_remove = webhook_actions.add_parser("remove", help="delete a webhook")
    webhook_remove.add_argument("name", help="the webhook's name")

    webhook_test = webhook_actions.add_parser(
        "test",
        help=(
            "send one sample event, marked as a test, and report the HTTP outcome. "
            "Works on a disabled webhook and ignores its hop limit; never retried"
        ),
    )
    webhook_test.add_argument("name", help="the webhook's name")
    webhook_test.add_argument(
        "--trigger",
        required=True,
        choices=[trigger.value for trigger in Trigger],
        help="which event to send a sample of",
    )

    for webhook_parser in (
        webhook_add,
        webhook_actions.choices["list"],
        webhook_show,
        webhook_enable,
        webhook_disable,
        webhook_set,
        webhook_set_url,
        webhook_rename,
        webhook_remove,
        webhook_test,
    ):
        _add_database_url_argument(webhook_parser)

    channel = subparsers.add_parser(
        "channel",
        help=(
            "add, list, inspect, remove and read the group channels this station holds "
            "(needs a database)"
        ),
    )
    channel_actions = channel.add_subparsers(dest="channel_command", required=True)
    channel_add = channel_actions.add_parser(
        "add",
        help="add a channel: Public, a hashtag, or a pre-shared key. " + PSK_IS_READ_FROM_STDIN,
        description=PSK_IS_READ_FROM_STDIN,
    )
    channel_source = channel_add.add_mutually_exclusive_group(required=True)
    channel_source.add_argument(
        "--public", action="store_true", help="the stock MeshCore Public channel"
    )
    channel_source.add_argument(
        "--hashtag",
        metavar="HASHTAG",
        default=None,
        help="a channel whose key is derived from this hashtag, e.g. #dev-sighop",
    )
    channel_source.add_argument(
        "--psk-stdin",
        action="store_true",
        help="a 16- or 32-byte pre-shared key, base64, read from standard input",
    )
    channel_source.add_argument(
        "--generate",
        action="store_true",
        help="a newly generated 16-byte pre-shared key, printed once",
    )
    channel_add.add_argument(
        "--name",
        default=None,
        help=(
            "the channel's name (default: Public, or the hashtag); required for a pre-shared key"
        ),
    )
    channel_actions.add_parser("list", help="list the channels, with kind and hash, no keys")
    channel_show = channel_actions.add_parser(
        "show", help="show one channel and its recorded message count"
    )
    channel_show.add_argument("name", help="the channel's name")
    channel_rename = channel_actions.add_parser(
        "rename", help="change a channel's name. Its key and channel hash are untouched"
    )
    channel_rename.add_argument("name", help="the channel, by name")
    channel_rename.add_argument("new_name", help="the new name")

    channel_remove = channel_actions.add_parser(
        "remove", help="delete a channel and all of its recorded messages"
    )
    channel_remove.add_argument("name", help="the channel's name")
    channel_remove.add_argument(
        "--delete-history",
        action="store_true",
        help="accept that the channel's recorded messages are deleted, without a prompt",
    )
    channel_key = channel_actions.add_parser(
        "key", help="print a channel's key in base64, for sharing it"
    )
    channel_key.add_argument("name", help="the channel's name")
    channel_history = channel_actions.add_parser(
        "history", help="print a channel's most recent messages"
    )
    channel_history.add_argument("name", help="the channel's name")
    channel_history.add_argument(
        "--limit", type=int, default=20, help="how many messages (default: 20)"
    )
    for channel_parser in (
        channel_add,
        channel_actions.choices["list"],
        channel_show,
        channel_rename,
        channel_remove,
        channel_key,
        channel_history,
    ):
        _add_database_url_argument(channel_parser)

    web = subparsers.add_parser(
        "web", help="manage what the web interface needs from a terminal (accounts)"
    )
    web_actions = web.add_subparsers(dest="web_command", required=True)
    web_user = web_actions.add_parser(
        "user",
        help=(
            "add, list, re-password, disable, enable and remove the accounts that "
            "sign in to the web interface. Terminal only: the browser offers none "
            "of this (needs a database)"
        ),
    )
    user_actions = web_user.add_subparsers(dest="web_user_command", required=True)
    user_add = user_actions.add_parser(
        "add",
        help=(
            "add an enabled account. The password is read from a prompt (asked "
            "twice) or from standard input, never from an argument"
        ),
    )
    user_actions.add_parser("list", help="list accounts; never shows a hash")
    user_passwd = user_actions.add_parser(
        "passwd",
        help=(
            "set an account's password. Sessions signed in with the old one end "
            "within a minute on every run using this database"
        ),
    )
    user_disable = user_actions.add_parser(
        "disable", help="stop an account signing in; its sessions end within a minute"
    )
    user_enable = user_actions.add_parser("enable", help="let a disabled account sign in again")
    user_remove = user_actions.add_parser(
        "remove", help="delete an account; its sessions end within a minute"
    )
    for account_parser in (user_add, user_passwd, user_disable, user_enable, user_remove):
        account_parser.add_argument("username", help="the account's username (case-insensitive)")
        # A second positional exists only to be refused with a reason. Without
        # it, `sighop web user add alice hunter2` is an argparse usage error that
        # does not say why — and the why is the point (`runtime-cli`).
        account_parser.add_argument("refused_arguments", nargs="*", help=argparse.SUPPRESS)
        account_parser.add_argument(
            "--password", action="store_true", dest="password_flag", help=argparse.SUPPRESS
        )
    for secret_parser in (user_add, user_passwd):
        secret_parser.add_argument(
            "--password-stdin",
            action="store_true",
            help="read the password from standard input rather than prompting",
        )
    for last_parser in (user_disable, user_remove):
        last_parser.add_argument(
            "--allow-no-accounts",
            action="store_true",
            help=(
                "permit this change when it leaves no enabled account. No run can "
                "then start its web interface until one is added or enabled"
            ),
        )
    for account_parser in user_actions.choices.values():
        _add_database_url_argument(account_parser)

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
    args: argparse.Namespace, stored: Sequence[LoadedEntity] = (), *, replay: bool = False
) -> RuntimeConfig:
    return RuntimeConfig(
        replay=replay,
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
        if getattr(args, "migrate", False):
            raise ConfigError(
                "--migrate applies migrations to the configured database, and none is "
                "configured: set DATABASE_URL or pass --database-url"
            )
        return None, ()

    if getattr(args, "migrate", False):
        await migrate_on_start(config.database)

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


async def migrate_on_start(database: DatabaseConfig) -> None:
    """`run --migrate`: bring a database that is behind up to this build's head.

    Only *behind* is fixed here. A database ahead of the code is left untouched
    for the schema-version check to refuse: nothing in this build describes its
    schema, and Alembic would fail on it less legibly than that check does.
    """
    logger = get_logger(component="db")
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


# --- Key management (design D12) --------------------------------------------


def _keys_new(args: argparse.Namespace, out: IO[str]) -> int:
    """Create a keyfile and print the public key another node's contact list needs.

    With `--private-key`, the identity is the operator's rather than a new one.
    It is validated before the file is opened, so a refused key leaves no file
    behind — including no empty one at the path they meant to keep.
    """
    supplied: LocalIdentity | None = None
    if args.private_key is not None:
        try:
            supplied = private_key_from_hex(args.private_key)
            refuse_unusable_node_hash(supplied)
        except PrivateKeyError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        keyfile = create_keyfile(
            args.out,
            args.name,
            node_type=NodeType[args.node_type],
            identity=supplied,
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
    print(f"!! {PLAINTEXT_KEY_NOTICE}", file=out)
    for warning in keyfile.warnings:
        print(f"!! {warning}", file=out)
    return 0


def _keys_secret(out: IO[str]) -> int:
    """Generate the key entity private keys are sealed under, and what losing it costs.

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
    """Stored identities. No private key, no ciphertext, and no flag that prints one."""
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


def _secret_key() -> bytes:
    """`SIGHOP_SECRET_KEY`, through `Config` like every other read of it."""
    return Config.from_environment().secret_key_bytes()


IMPORT_NEEDS_ONE_SOURCE = (
    "an identity comes from a keyfile or from --private-key, and this was given "
    "both. They are alternatives: the keyfile carries a name and a node type, "
    "and a bare private key needs --name to supply one"
)

IMPORT_NEEDS_A_SOURCE = (
    "no identity to import: pass a keyfile, or --private-key with --name for an "
    "identity you already hold"
)

PRIVATE_KEY_NEEDS_A_NAME = (
    "--private-key needs --name: a keyfile carries the entity's advertised name "
    "and a bare private key does not"
)


def _keys_import(args: argparse.Namespace, out: IO[str]) -> int:
    """Milestone 4's promised one-function conversion (its design D1).

    Two ways in, never both (design D8): a keyfile, or a private key the
    operator already holds. The second writes nothing to disk at any point,
    which is the reason it exists — moving an identity into the store should not
    require leaving unencrypted key material in a file first.
    """
    keyfile: Keyfile | None = None
    identity: LocalIdentity | None = None

    if args.keyfile is not None and args.private_key is not None:
        print(IMPORT_NEEDS_ONE_SOURCE, file=sys.stderr)
        return 2
    if args.keyfile is None and args.private_key is None:
        print(IMPORT_NEEDS_A_SOURCE, file=sys.stderr)
        return 2
    if args.private_key is not None and not args.name:
        print(PRIVATE_KEY_NEEDS_A_NAME, file=sys.stderr)
        return 2

    database = _database_config(args, out)
    if database is None:
        return 2
    try:
        secret = _secret_key()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.keyfile is not None:
        try:
            keyfile = load_keyfile(args.keyfile)
        except KeyfileError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        name, identity, node_type = keyfile.name, keyfile.identity, keyfile.node_type
    else:
        try:
            identity = private_key_from_hex(args.private_key)
            refuse_unusable_node_hash(identity)
        except PrivateKeyError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        name, node_type = args.name, NodeType[args.node_type]

    async def store_it(store: EntityRepository) -> Outcome[EntityRecord]:
        # Neither the "already stored" refusal nor the "a bot adverts as CHAT"
        # one is this command's: the browser imports through the same call and
        # has to be refused in the same words. What stays here is the *source*
        # this one was asked about, which the browser has no equivalent of.
        return await store.store(
            name=name,
            identity=identity,
            secret=secret,
            node_type=node_type,
            entity_type=BOT_ENTITY_TYPE if args.bot else None,
            advert_config=advert_config_for(NodeType.CHAT) if args.bot else None,
        )

    source = str(keyfile.path) if keyfile is not None else "the supplied private key"
    try:
        outcome = asyncio.run(_with_store(database, store_it))
    except (EntityExistsError, EntityRoleError, EntityNameError) as exc:
        print(f"{source}: {exc}", file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    record = outcome.value
    print(f"imported   {source}", file=out)
    print(f"entity_id  {record.id}", file=out)
    print(f"name       {record.name}", file=out)
    print(f"type       {record.type}", file=out)
    print(f"node_type  {NodeType(record.node_type).name}", file=out)
    print(f"public_key {record.public_key.hex()}", file=out)
    print(f"node_hash  0x{record.node_hash:02x}", file=out)
    print(
        "the private key is sealed under SIGHOP_SECRET_KEY and is not stored in the clear",
        file=out,
    )
    for warning in keyfile.warnings if keyfile is not None else ():
        print(f"!! {warning}", file=out)
    return 0


def _one_identity[T: LoadedEntity | EntityRecord](
    candidates: Iterable[T], reference: str
) -> T | None:
    """The one identity `reference` names, or None with the reason printed.

    Exact name or hex public key prefix, and an ambiguous reference is refused
    rather than resolved by order — picking "the first match" would make which
    identity an operator exported or removed depend on `created_at`.

    Shared by `keys export` and `keys delete` so the two select identically.
    It takes records as well as opened entities, because a row under the removed
    seed format cannot be opened and is exactly what `keys delete` is for.
    """
    wanted = reference.removeprefix("0x").lower()
    matches = [
        candidate
        for candidate in candidates
        if candidate.name == reference or candidate.public_key.hex().startswith(wanted)
    ]
    if not matches:
        print(f"no stored identity matches {reference!r}", file=sys.stderr)
        return None
    if len(matches) > 1:
        listed = ", ".join(f"{m.name} ({m.public_key.hex()[:16]})" for m in matches)
        print(f"{reference!r} matches {len(matches)} identities: {listed}", file=sys.stderr)
        return None
    return matches[0]


def _keys_export(args: argparse.Namespace, out: IO[str]) -> int:
    """Deliberate export, which is what §6 asks for. Never a side effect."""
    database = _database_config(args, out)
    if database is None:
        return 2
    try:
        secret = _secret_key()
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

    entity = _one_identity(outcome.value, args.reference)
    if entity is None:
        return 2
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
    print(f"!! {PLAINTEXT_KEY_NOTICE}", file=out)
    return 0


def _removal_consequence(record: EntityRecord, database: DatabaseConfig) -> str:
    """What removing this identity costs, in the terms this row is actually in.

    A stranded row and an openable one cost very different things, and an
    operator clearing up after migration 0008 is looking at the first. Saying
    "unrecoverable" about key material nothing can read any more would be both
    false and discouraging at exactly the wrong moment.
    """
    opened = f"removing identity {record.name!r} ({record.public_key.hex()[:16]}) "
    try:
        secret = _secret_key()
    except ConfigError:
        # No secret configured. Removal still works — a row nobody here can open
        # is one an operator may well be clearing — but we cannot say which case
        # it is without opening it, and we will not guess.
        return (
            opened + "cannot be undone. Without SIGHOP_SECRET_KEY set, whether its "
            "stored key material can still be read is unknown from here"
        )

    async def load(store: EntityRepository) -> Outcome[OpenedEntities]:
        return await store.load_openable(secret)

    try:
        outcome = asyncio.run(_with_store(database, load))
    except (EntityLoadError, SealError):
        return opened + "cannot be undone"
    if isinstance(outcome, Failed):
        return opened + "cannot be undone"
    if any(record.public_key.hex()[:16] in refusal for refusal in outcome.value.stranded):
        return (
            opened + "loses nothing you can still use: its stored key material is a "
            "seed this build cannot read. Removing the row is what lets the same "
            "identity be imported again with `keys import --private-key`"
        )
    return (
        opened + "cannot be undone. Unless you hold this identity's private key "
        "elsewhere, it is gone: peers that know this public key will never reach it "
        "again"
    )


RENAME_IS_ON_THE_AIR = (
    "this name travels in the identity's adverts and is the sender name of "
    "every channel post it makes. Neighbours will keep showing the old name "
    "until it adverts again; `sighop run --web` offers an advert now, per "
    "identity, from the identities page"
)
"""Said wherever a stored identity is renamed.

A rename is the one configuration change whose effect is on other people's
screens rather than ours, and it does not take effect there until the next
advert — which is hours away by default, and is the part an operator would
otherwise discover by being asked why the old name is still showing.
"""


def _keys_rename(args: argparse.Namespace, out: IO[str]) -> int:
    """Rename a stored identity. Nothing is confirmed: nothing is lost.

    A process already running holds this identity in memory and keeps the old
    name until it restarts — said here rather than left to be discovered,
    because there is no way from this command to reach that process. The
    panel's own rename, served from inside the run, does reach it.
    """
    database = _database_config(args, out)
    if database is None:
        return 2

    async def listed(store: EntityRepository) -> Outcome[list[EntityRecord]]:
        return await store.list_all()

    outcome = asyncio.run(_with_store(database, listed))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    record = _one_identity(outcome.value, args.reference)
    if record is None:
        return 2

    async def rename(store: EntityRepository) -> Outcome[str | None]:
        return await store.rename(record.public_key, args.name)

    try:
        renamed = asyncio.run(_with_store(database, rename))
    except EntityNameError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(renamed, Failed):
        print(str(renamed.error), file=sys.stderr)
        return 2
    if renamed.value is None:
        print(f"no stored identity matches {args.reference!r}", file=sys.stderr)
        return 2
    print(f"renamed    {renamed.value} -> {args.name.strip()}", file=out)
    print(f"public_key {record.public_key.hex()}", file=out)
    print(f"node_hash  0x{record.node_hash:02x}", file=out)
    print(RENAME_IS_ON_THE_AIR, file=out)
    print(
        "a run already holding this identity keeps the old name until it restarts",
        file=out,
    )
    return 0


def _keys_delete(args: argparse.Namespace, out: IO[str]) -> int:
    """Remove one stored identity (design D10).

    The only destructive action on the `keys` surface, and it exists because a
    row stranded by migration 0008 blocks its own replacement: `store()` refuses
    the public key it still holds.

    Two things stand in front of it. An identity a room or a bot is bound to is
    refused outright — those foreign keys cascade, so removing it would take a
    room's members and its whole history without saying so. And the removal is
    confirmed the way `sighop channel remove` is confirmed, because an operator
    should not have to learn a second pattern for the same kind of act.
    """
    database = _database_config(args, out)
    if database is None:
        return 2

    async def listed(store: EntityRepository) -> Outcome[list[EntityRecord]]:
        return await store.list_all()

    outcome = asyncio.run(_with_store(database, listed))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    record = _one_identity(outcome.value, args.reference)
    if record is None:
        return 2

    async def bindings(store: EntityRepository) -> Outcome[list[str]]:
        return await store.bound_to(record.id)

    bound = asyncio.run(_with_store(database, bindings))
    if isinstance(bound, Failed):
        print(str(bound.error), file=sys.stderr)
        return 2
    if bound.value:
        print(
            f"identity {record.name!r} is serving {' and '.join(bound.value)}, which "
            "would be deleted with it. Remove them first; nothing was removed",
            file=sys.stderr,
        )
        return 2

    consequence = _removal_consequence(record, database)
    if not args.delete_key:
        if not sys.stdin.isatty():
            print(
                f"{consequence}. Nothing was removed: confirm at a terminal, or pass "
                "--delete-key to accept it",
                file=sys.stderr,
            )
            return 2
        print(consequence, file=out)
        answer = input(f"type the identity name ({record.name}) to remove it: ")
        if answer.strip() != record.name:
            print("not confirmed; nothing was removed", file=sys.stderr)
            return 2

    async def remove(store: EntityRepository) -> Outcome[bool]:
        return await store.remove(record.public_key)

    removed = asyncio.run(_with_store(database, remove))
    if isinstance(removed, Failed):
        print(str(removed.error), file=sys.stderr)
        return 2
    if not removed.value:
        print(f"no stored identity matches {args.reference!r}", file=sys.stderr)
        return 2
    print(f"removed    {record.name}", file=out)
    print(f"public_key {record.public_key.hex()}", file=out)
    print(f"node_hash  0x{record.node_hash:02x}", file=out)
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
    """Inspect an identity. The private key is not printed, and there is no flag for it."""
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
# Applying migrations is a deliberate act and never a side effect of a plain
# `run`: an old binary restarted after a failed deploy meets a schema it does not
# know, and a new binary racing another instance applies DDL twice. The one
# exception is asked for by name — `run --migrate`, which the compose deployment
# passes because there, starting the new image *is* the deploy (milestone 9).


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


class PasswordMismatchError(ValueError):
    """Two prompted entries of a password differed."""


def _read_password(prompt: str, *, from_stdin: bool, confirm: bool = False) -> str:
    """A password, from standard input or from a prompt that does not echo.

    `confirm` asks a second time at an interactive prompt and refuses a
    mismatch. Standard input is read once: a script piping a password in has
    already typed it correctly or not, and asking it twice proves nothing.
    """
    if from_stdin or not sys.stdin.isatty():
        return sys.stdin.readline().rstrip("\n")
    import getpass

    first = getpass.getpass(prompt)
    if confirm and getpass.getpass(f"again, {prompt}") != first:
        raise PasswordMismatchError("the two entries differ; nothing was changed")
    return first


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
        case "rename":
            return _room_rename(args, database, out)
        case "delete":
            return _room_delete(args, database, out)
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
    guest = _read_password("guest password: ", from_stdin=False) if args.guest_password else None

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
        # "This identity is not a room server" is the repository's refusal, not
        # this command's: the browser creates rooms through the same call.
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
    except (RoomExistsError, RoomNameError, EntityLoadError, EntityRoleError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    _render_room(outcome.value, members=0, messages=0, out=out)
    print("the passwords are stored as Argon2id hashes and cannot be recovered", file=out)
    return 0


def _room_rename(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    """Change a room's name. Nothing to confirm: nothing is lost by it."""

    async def work(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        return await persistence.rooms.rename(record.id, args.name)

    try:
        outcome = asyncio.run(_with_rooms(database, work))
    except (RoomNameError, RoomNameTakenError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None or outcome.value is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    print(f"renamed  {outcome.value} -> {args.name.strip()}", file=out)
    print(
        "a room's name is a local label; it is not advertised and members see no change", file=out
    )
    return 0


def _room_delete(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    """Delete a room, its members and its history, keeping its identity.

    Confirmed the way `sighop channel remove` and `sighop keys delete` are,
    because an operator should not have to learn a third pattern for the same
    kind of act. The counts come first: the foreign keys cascade, so the call
    that deletes says nothing about how much went with it.
    """

    async def count(persistence: Persistence) -> Any:
        record = await _find_room(persistence, args.room)
        if record is None or isinstance(record, Failed):
            return record
        members = await persistence.members.load_for_room(record.id)
        messages = await persistence.messages.count(record.id)
        if isinstance(members, Failed):
            return members
        if isinstance(messages, Failed):
            return messages
        entity = await persistence.entities.get_by_id(record.entity_id)
        held = entity.value if isinstance(entity, Succeeded) else None
        return (record, len(members.value), messages.value, held)

    outcome = asyncio.run(_with_rooms(database, count))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no room named {args.room!r}", file=sys.stderr)
        return 2
    record, members, messages, entity = outcome
    consequence = (
        f"deleting room {record.name!r} deletes its {members} member(s) and "
        f"{messages} stored message(s); they cannot be recovered"
    )
    if not args.delete_history:
        if not sys.stdin.isatty():
            print(
                f"{consequence}. Nothing was deleted: confirm at a terminal, or pass "
                "--delete-history to accept it",
                file=sys.stderr,
            )
            return 2
        print(consequence, file=out)
        answer = input(f"type the room name ({record.name}) to delete it: ")
        if answer.strip() != record.name:
            print("not confirmed; nothing was deleted", file=sys.stderr)
            return 2

    async def remove(persistence: Persistence) -> Any:
        return await persistence.rooms.delete(record.id)

    deleted = asyncio.run(_with_rooms(database, remove))
    if isinstance(deleted, Failed):
        print(str(deleted.error), file=sys.stderr)
        return 2
    if not deleted.value:
        print(f"no room named {record.name!r}", file=sys.stderr)
        return 2
    print(f"deleted  {record.name}", file=out)
    print(f"members  {members}", file=out)
    print(f"messages {messages}", file=out)
    if entity is not None:
        print(
            f"identity {entity.name!r} was not deleted and now serves no room; "
            "it can carry a new one, or be removed with `sighop keys delete`",
            file=out,
        )
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
        matches = [member for member in members.value if member.public_key.hex().startswith(wanted)]
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
        author = next((row.public_key for row in rows.value if row.id == record.entity_id), None)
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
    from sighop.bots.greeter import seeded_entries

    contacts = await persistence.contacts.load_all()
    if isinstance(contacts, Failed):
        return contacts
    # Which contacts, which key and which record are the greeter's decision and
    # live beside its key convention: the browser creates greeters too, and the
    # debt a new one starts with must be the same either way.
    return await persistence.bot_state.set_many(
        record.id, seeded_entries(contacts.value, at=_now_iso())
    )


def _now_iso() -> str:
    import datetime as dt

    return dt.datetime.now(dt.UTC).isoformat()


def _render_bot(record: BotRecord, out: IO[str]) -> None:
    """One bot's configuration. No key material appears here and none can:
    nothing on this path has ever held a private key or its ciphertext."""
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
        case "delete":
            return _bot_delete(args, database, out)
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


def _bot_enablement(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
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
            f"bot {outcome.entity_name!r} is enabled and will run, in {outcome.mode} mode",
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
    from sighop.bots.greeter import greeted_key, greeted_public_key, operator_entry
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
                record.id, key, operator_entry(contact, at=_now_iso())
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
                key: value for key, value in payload.items() if greeted_public_key(key) is not None
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


def _bot_delete(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    """Delete a bot and its durable state, keeping the identity it runs on.

    `bot_state.bot_id` cascades, so the keys go without the call mentioning
    them — counted first, in the terms `--clear` already uses, because what
    they record is the interesting part and not how many there are.
    """

    async def count(persistence: Persistence) -> Any:
        record = await _find_bot(persistence, args.bot)
        if record is None or isinstance(record, Failed):
            return record
        state = await persistence.bot_state.list(record.id)
        if isinstance(state, Failed):
            return state
        entity = await persistence.entities.get_by_id(record.entity_id)
        held = entity.value if isinstance(entity, Succeeded) else None
        return (record, len(state.value), held)

    outcome = asyncio.run(_with_rooms(database, count))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no bot named {args.bot!r}", file=sys.stderr)
        return 2
    record, keys, entity = outcome
    consequence = (
        f"deleting bot {record.entity_name!r} deletes the {keys} key(s) it has "
        f"stored; {CLEARING_STATE_FORGETS}"
    )
    if not args.delete_state:
        if not sys.stdin.isatty():
            print(
                f"{consequence}. Nothing was deleted: confirm at a terminal, or pass "
                "--delete-state to accept it",
                file=sys.stderr,
            )
            return 2
        print(consequence, file=out)
        answer = input(f"type the bot name ({record.entity_name}) to delete it: ")
        if answer.strip() != record.entity_name:
            print("not confirmed; nothing was deleted", file=sys.stderr)
            return 2

    async def remove(persistence: Persistence) -> Any:
        return await persistence.bots.delete(record.id)

    deleted = asyncio.run(_with_rooms(database, remove))
    if isinstance(deleted, Failed):
        print(str(deleted.error), file=sys.stderr)
        return 2
    if not deleted.value:
        print(f"no bot named {record.entity_name!r}", file=sys.stderr)
        return 2
    print(f"deleted  {record.entity_name}", file=out)
    print(f"driver   {record.driver}", file=out)
    print(f"state    {keys} key(s)", file=out)
    if entity is not None:
        print(
            f"identity {entity.name!r} was not deleted and now runs no bot; "
            "it can carry a new one, or be removed with `sighop keys delete`",
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


# --- Web interface accounts (milestone 9) ------------------------------------
#
# Terminal only (milestone 9 design D2). With no roles, anyone signed in to a
# browser that could create accounts could create a second one for themselves,
# and a stolen session would become a persistent credential. Terminal access to
# the host is the stronger proof of being the operator.

ACCOUNTS_NEED_A_DATABASE = (
    "no database is configured: web interface accounts are stored in the "
    "database, so this command needs one. Set DATABASE_URL "
    "or pass --database-url"
)

PASSWORD_IS_NEVER_AN_ARGUMENT = (
    "a password is never accepted as a command-line argument: process arguments "
    "are readable by every user on this host (`ps`). Run the command with the "
    "username only, and type the password at the prompt or pipe it to standard "
    "input with --password-stdin"
)

SESSIONS_END_WITHIN_A_MINUTE = (
    "sessions already signed in to it end within a minute on every run using this database"
)

NO_ACCOUNT_LEFT = (
    "no run could then start its web interface, because `run --web` refuses a "
    "database with no enabled account. Pass --allow-no-accounts to do it anyway"
)

NEXT_RUN_OFFERS_SETUP = (
    "the next `run --web` would then offer first-run setup, creating an account "
    "for whoever holds the one-time setup code printed in its output. Pass "
    "--allow-no-accounts to do it anyway"
)

SETUP_WILL_BE_OFFERED = (
    "no account is left: the next `run --web` will offer first-run setup, with a "
    "one-time setup code printed in its output"
)


def _web_user_command(args: argparse.Namespace, out: IO[str]) -> int:
    if getattr(args, "refused_arguments", None) or getattr(args, "password_flag", False):
        print(PASSWORD_IS_NEVER_AN_ARGUMENT, file=sys.stderr)
        return 2
    try:
        config = Config.from_environment(database_url=getattr(args, "database_url", None))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if config.database is None:
        print(ACCOUNTS_NEED_A_DATABASE, file=sys.stderr)
        return 2
    database = config.database
    try:
        match args.web_user_command:
            case "add":
                return _web_user_add(args, database, out)
            case "list":
                return _web_user_list(database, out)
            case "passwd":
                return _web_user_passwd(args, database, out)
            case "enable":
                return _web_user_enablement(args, database, out, enabled=True)
            case "disable":
                return _web_user_enablement(args, database, out, enabled=False)
            case _:
                return _web_user_remove(args, database, out)
    except (UsernameError, WebUserExistsError, PasswordMismatchError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except DatabaseError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _new_web_password(args: argparse.Namespace) -> str | None:
    password = _read_password(
        f"password for {normalise_username(args.username)}: ",
        from_stdin=args.password_stdin,
        confirm=True,
    )
    if not password:
        print(
            "a web account password cannot be empty: it is the only thing between "
            "the network and the transmit gate",
            file=sys.stderr,
        )
        return None
    return password


def _web_user_add(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    name = normalise_username(args.username)
    password = _new_web_password(args)
    if password is None:
        return 2
    hashed = hash_password(password)

    async def work(persistence: Persistence) -> Any:
        return await persistence.web_users.add(name, password_hash=hashed)

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    print(f"account {outcome.value.username!r} is added and enabled", file=out)
    print(
        "it can sign in to the web interface of any run using this database "
        f"({database.redacted_url}); the password is stored as an Argon2id hash",
        file=out,
    )
    return 0


def _web_user_list(database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        return await persistence.web_users.list()

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if not outcome.value:
        print(
            "no accounts: `run --web` will offer first-run setup in the browser, with "
            "a one-time setup code printed in its output; or add one from the "
            "terminal with `sighop web user add <username>`",
            file=out,
        )
        return 0
    width = max(len("username"), *(len(record.username) for record in outcome.value))
    print(f"{'username':<{width}}  enabled  created                    password_set", file=out)
    for record in outcome.value:
        print(
            f"{record.username:<{width}}  {'yes' if record.enabled else 'no ':<7}  "
            f"{record.created_at.isoformat(timespec='seconds'):<25}  "
            f"{record.password_set_at.isoformat(timespec='seconds')}",
            file=out,
        )
    return 0


def _web_user_passwd(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    name = normalise_username(args.username)
    password = _new_web_password(args)
    if password is None:
        return 2
    hashed = hash_password(password)

    async def work(persistence: Persistence) -> Any:
        return await persistence.web_users.set_password(name, password_hash=hashed)

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if not outcome.value:
        print(f"no account named {name!r}", file=sys.stderr)
        return 2
    print(f"the password for {name!r} is changed and stored as an Argon2id hash", file=out)
    print(f"{SESSIONS_END_WITHIN_A_MINUTE} with the old password", file=out)
    return 0


LAST_ENABLED = "last_enabled"
LAST_ACCOUNT = "last_account"


async def _leaves_no_account(persistence: Persistence, name: str, *, removing: bool) -> Any:
    """What disabling or removing `name` would leave behind.

    Returns a `Failed`, None for "no such account", `LAST_ACCOUNT` when removing
    the only account at all (the next run would offer first-run setup),
    `LAST_ENABLED` when nothing enabled would remain, or False.
    """
    account = await persistence.web_users.get(name)
    if isinstance(account, Failed) or account.value is None:
        return account if isinstance(account, Failed) else None
    if removing:
        total = await persistence.web_users.count()
        if isinstance(total, Failed):
            return total
        if total.value <= 1:
            return LAST_ACCOUNT
    enabled = await persistence.web_users.count_enabled()
    if isinstance(enabled, Failed):
        return enabled
    return LAST_ENABLED if account.value.enabled and enabled.value <= 1 else False


def _web_user_enablement(
    args: argparse.Namespace, database: DatabaseConfig, out: IO[str], *, enabled: bool
) -> int:
    name = normalise_username(args.username)

    async def work(persistence: Persistence) -> Any:
        if not enabled and not args.allow_no_accounts:
            last = await _leaves_no_account(persistence, name, removing=False)
            if last is not False:
                return last
        return await persistence.web_users.set_enabled(name, enabled)

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome == LAST_ENABLED:
        print(
            f"{name!r} is the only enabled account; disabling it is refused: {NO_ACCOUNT_LEFT}",
            file=sys.stderr,
        )
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None or not outcome.value:
        print(f"no account named {name!r}", file=sys.stderr)
        return 2
    if enabled:
        print(f"account {name!r} is enabled and can sign in again", file=out)
    else:
        print(f"account {name!r} is disabled; {SESSIONS_END_WITHIN_A_MINUTE}", file=out)
    return 0


def _web_user_remove(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    name = normalise_username(args.username)

    async def work(persistence: Persistence) -> Any:
        if not args.allow_no_accounts:
            last = await _leaves_no_account(persistence, name, removing=True)
            if last is not False:
                return last
        removed = await persistence.web_users.remove(name)
        if isinstance(removed, Failed) or not removed.value:
            return removed
        left = await persistence.web_users.count()
        return removed, isinstance(left, Succeeded) and left.value == 0

    outcome = asyncio.run(_with_rooms(database, work))
    if outcome == LAST_ACCOUNT:
        print(
            f"{name!r} is the only account; removing it is refused: {NEXT_RUN_OFFERS_SETUP}",
            file=sys.stderr,
        )
        return 2
    if outcome == LAST_ENABLED:
        print(
            f"{name!r} is the only enabled account; removing it is refused: {NO_ACCOUNT_LEFT}",
            file=sys.stderr,
        )
        return 2
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None or not isinstance(outcome, tuple):
        print(f"no account named {name!r}", file=sys.stderr)
        return 2
    _, none_left = outcome
    print(f"account {name!r} is removed; {SESSIONS_END_WITHIN_A_MINUTE}", file=out)
    if none_left:
        print(SETUP_WILL_BE_OFFERED, file=out)
    return 0


async def _attach_web(
    args: argparse.Namespace, runtime: Runtime, out: IO[str] | None
) -> WebInterface | None:
    """Bind the panel's socket and hang it off the run, or do nothing at all.

    Called after the runtime is composed and **before** it runs, which is what
    makes a port clash a startup failure: nothing has been received, nothing has
    been transmitted, and the run does not continue with a silently absent
    interface (`web-server`, `runtime-cli`).

    Milestone 9 puts three refusals in front of the bind, each before any socket
    exists: an allowed host name that is a wildcard or a URL, a run with no
    database (the accounts live there), and a database whose accounts are all
    disabled (nobody could sign in, and an operator locked it deliberately). A
    database with no account at all is served in first-run setup instead, with
    a one-time code generated here (web-first-run-setup design D1). The account
    counts are read here and reported.

    A run that was not asked for the interface returns here having listened on
    nothing and said nothing — the whole of "opt-in and off by default".
    """
    if not getattr(args, "web", False):
        return None
    allowed = validate_allowed_hosts(getattr(args, "web_allowed_host", ()) or ())
    persistence = runtime.persistence
    if persistence is None:
        raise WebStartupError(NO_DATABASE_FOR_WEB)
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
        host=args.web_host,
        port=args.web_port,
        allowed=allowed,
        feed=hub,
        conversations=conversations,
        sealing_secret=_web_sealing_secret(args, runtime),
        announce=runtime.say,
        channel_log=channel_log,
    )
    # The event first, then the output. Both are unconditional: there is no
    # option that serves a non-loopback bind without saying what it exposes.
    interface.report()
    stream = out if out is not None else sys.stdout
    for line in interface.startup_lines():
        print(line, file=stream)
    runtime.services = (*runtime.services, interface.service())
    return interface


def _webhook_secret(args: argparse.Namespace, persistence: Persistence | None) -> bytes | None:
    """`SIGHOP_SECRET_KEY` for opening webhook URLs, or `None` when unusable.

    Not demanded: a run with a database and no secret still runs, and its
    startup line says webhooks are off because the URLs cannot be opened.
    """
    if persistence is None:
        return None
    try:
        return Config.from_environment(
            database_url=getattr(args, "database_url", None)
        ).secret_key_bytes()
    except ConfigError:
        return None


def _web_sealing_secret(args: argparse.Namespace, runtime: Runtime) -> bytes | None:
    """`SIGHOP_SECRET_KEY` for the panel, or `None` when nothing is sealed.

    Design D1: the panel exports a *stored* identity, which means opening a
    sealed private key, which needs the key this module already reads. It is passed
    here rather than reached for inside `web/` because `cli.py` is the one
    module that composes both sides.

    Only when a database is configured: a run with no database has no sealed
    key to open, and demanding the variable would make the panel refuse to
    start for a capability that run does not have. The run itself has already
    failed by now if the variable was needed and missing, so this cannot be the
    place a bad value is first discovered.
    """
    if runtime.persistence is None:
        return None
    try:
        return Config.from_environment(
            database_url=getattr(args, "database_url", None)
        ).secret_key_bytes()
    except ConfigError:  # pragma: no cover - the run would already have failed
        return None


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
    interface: WebInterface | None = None
    try:
        runtime = Runtime(
            source=events,
            startup=lambda: _live_startup(modem, runtime),
            config=_run_config(args, stored),
            webhook_secret=_webhook_secret(args, persistence),
            sender=modem,
            out=out,
            capture_writer=writer,
            capture_probe=probe_result,
            persistence=persistence,
        )
        interface = await _attach_web(args, runtime, out)
        runtime.install_signal_handlers()
        await runtime.run()
    finally:
        if interface is not None:
            interface.close()
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
    config = _run_config(args, stored, replay=True)
    radio = _replay_radio(replay.provenance) or RADIO_PRESETS[args.radio_preset]

    runtime = Runtime(
        source=replay.events(),
        startup=lambda: _replay_startup(replay, args.replay),
        config=config,
        radio=radio,
        out=out,
        persistence=persistence,
    )
    interface = await _attach_web(args, runtime, out)
    runtime.install_signal_handlers()
    try:
        await runtime.run()
    finally:
        if interface is not None:
            interface.close()
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


# --- sighop webhook -----------------------------------------------------------

URL_IS_READ_FROM_STDIN = (
    "The URL is read from standard input, never from an argument: arguments are "
    "visible in process listings and shell history, and a webhook URL is the "
    "credential that lets anyone post to it"
)

WEBHOOKS_NEED_A_DATABASE = (
    "no database is configured: webhooks are stored configuration and require "
    "durable storage, so this command needs one. Set DATABASE_URL or pass "
    "--database-url"
)

CHANGES_REACH_A_RUNNING_PROCESS = (
    "a running process applies this from the next event, without a restart"
)


def _webhook_command(args: argparse.Namespace, out: IO[str]) -> int:
    try:
        config = Config.from_environment(database_url=getattr(args, "database_url", None))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if config.database is None:
        print(WEBHOOKS_NEED_A_DATABASE, file=sys.stderr)
        return 2
    try:
        match args.webhook_command:
            case "add":
                return _webhook_add(args, config, out)
            case "list":
                return _webhook_list(config.database, out)
            case "show":
                return _webhook_show(args, config.database, out)
            case "enable" | "disable":
                return _webhook_enablement(args, config.database, out)
            case "set":
                return _webhook_set(args, config.database, out)
            case "set-url":
                return _webhook_set_url(args, config, out)
            case "rename":
                return _webhook_rename(args, config.database, out)
            case "remove":
                return _webhook_remove(args, config.database, out)
            case _:
                return _webhook_test(args, config, out)
    except (WebhookConfigError, ConfigError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _read_webhook_url() -> str:
    """One line from standard input, or an unechoed prompt at a terminal."""
    if sys.stdin.isatty():
        import getpass

        return getpass.getpass("webhook URL: ").strip()
    return sys.stdin.readline().strip()


def _when(value: Any) -> str:
    return "never" if value is None else value.isoformat()


def _render_webhook(record: WebhookRecord, out: IO[str]) -> None:
    """One webhook. The target is scheme and host: the URL is never read back."""
    print(f"webhook    {record.name}", file=out)
    print(f"target     {record.url_host}", file=out)
    print(f"format     {record.format}", file=out)
    print(f"triggers   {', '.join(record.triggers)}", file=out)
    print(f"max_hops   {'none' if record.max_hops is None else record.max_hops}", file=out)
    print(f"enabled    {'yes' if record.enabled else 'no'}", file=out)
    print(f"last_ok    {_when(record.last_delivered_at)}", file=out)
    failure = _when(record.last_failed_at)
    if record.last_failure:
        failure = f"{failure} ({record.last_failure})"
    print(f"last_fail  {failure}", file=out)


async def _find_webhook(persistence: Persistence, name: str) -> Any:
    found = await persistence.webhooks.get_by_name(name)
    if isinstance(found, Failed):
        return found
    return found.value


def _webhook_outcome(outcome: Any, name: str) -> int | None:
    """Print a database failure or a missing webhook; `None` means carry on."""
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no webhook named {name!r}", file=sys.stderr)
        return 2
    return None


def _webhook_add(args: argparse.Namespace, config: Config, out: IO[str]) -> int:
    assert config.database is not None
    secret = config.secret_key_bytes()
    url = _read_webhook_url()

    async def work(persistence: Persistence) -> Any:
        return await persistence.webhooks.create(
            name=args.name,
            url=url,
            format=args.format,
            triggers=args.triggers,
            max_hops=args.max_hops,
            secret=secret,
        )

    outcome = asyncio.run(_with_rooms(config.database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    record = outcome.value
    _render_webhook(record, out)
    print(
        f"webhook {record.name!r} is added and enabled; {CHANGES_REACH_A_RUNNING_PROCESS}",
        file=out,
    )
    if record.plaintext_http:
        print(PLAINTEXT_HTTP_WARNING, file=out)
    print(FIRST_RUN_BURST, file=out)
    return 0


def _webhook_list(database: DatabaseConfig, out: IO[str]) -> int:
    outcome = asyncio.run(_with_rooms(database, lambda p: p.webhooks.list_all()))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if not outcome.value:
        print("no webhooks are configured", file=out)
        return 0
    for record in outcome.value:
        print(
            f"{record.name}  target={record.url_host}  format={record.format}  "
            f"triggers={','.join(record.triggers)}  "
            f"max_hops={'none' if record.max_hops is None else record.max_hops}  "
            f"{'enabled' if record.enabled else 'disabled'}",
            file=out,
        )
    return 0


def _webhook_show(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    outcome = asyncio.run(_with_rooms(database, lambda p: _find_webhook(p, args.name)))
    refused = _webhook_outcome(outcome, args.name)
    if refused is not None:
        return refused
    _render_webhook(outcome, out)
    return 0


def _webhook_enablement(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    enabled = args.webhook_command == "enable"

    async def work(persistence: Persistence) -> Any:
        record = await _find_webhook(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        changed = await persistence.webhooks.set_enabled(record.id, enabled)
        return changed if isinstance(changed, Failed) else record

    outcome = asyncio.run(_with_rooms(database, work))
    refused = _webhook_outcome(outcome, args.name)
    if refused is not None:
        return refused
    state = "enabled" if enabled else "disabled: it stays configured and receives nothing"
    print(f"webhook {outcome.name!r} is {state}; {CHANGES_REACH_A_RUNNING_PROCESS}", file=out)
    return 0


def _webhook_set(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    changes: dict[str, Any] = {}
    if args.format is not None:
        changes["format"] = args.format
    if args.triggers is not None:
        changes["triggers"] = args.triggers
    if args.no_max_hops:
        changes["max_hops"] = None
    elif args.max_hops is not None:
        changes["max_hops"] = args.max_hops
    if not changes:
        print(
            "nothing to change: give --format, --trigger, --max-hops or --no-max-hops",
            file=sys.stderr,
        )
        return 2

    async def work(persistence: Persistence) -> Any:
        record = await _find_webhook(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        changed = await persistence.webhooks.update(record.id, **changes)
        if isinstance(changed, Failed):
            return changed
        return await _find_webhook(persistence, args.name)

    outcome = asyncio.run(_with_rooms(database, work))
    refused = _webhook_outcome(outcome, args.name)
    if refused is not None:
        return refused
    _render_webhook(outcome, out)
    print(f"webhook {outcome.name!r} is changed; {CHANGES_REACH_A_RUNNING_PROCESS}", file=out)
    return 0


def _webhook_set_url(args: argparse.Namespace, config: Config, out: IO[str]) -> int:
    assert config.database is not None
    secret = config.secret_key_bytes()
    url = _read_webhook_url()

    async def work(persistence: Persistence) -> Any:
        record = await _find_webhook(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        host = await persistence.webhooks.set_url(record.id, url, secret=secret)
        if isinstance(host, Failed):
            return host
        return await _find_webhook(persistence, args.name)

    outcome = asyncio.run(_with_rooms(config.database, work))
    refused = _webhook_outcome(outcome, args.name)
    if refused is not None:
        return refused
    print(
        f"webhook {outcome.name!r} now targets {outcome.url_host}; "
        f"{CHANGES_REACH_A_RUNNING_PROCESS}",
        file=out,
    )
    if outcome.plaintext_http:
        print(PLAINTEXT_HTTP_WARNING, file=out)
    return 0


def _webhook_rename(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    """Change a webhook's name. Its target, format and triggers are untouched."""

    async def work(persistence: Persistence) -> Any:
        record = await _find_webhook(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        renamed = await persistence.webhooks.rename(record.id, args.new_name)
        return renamed if isinstance(renamed, Failed) else record

    outcome = asyncio.run(_with_rooms(database, work))
    refused = _webhook_outcome(outcome, args.name)
    if refused is not None:
        return refused
    print(f"renamed {outcome.name} -> {args.new_name.strip()}", file=out)
    print(
        "its target, format, triggers and hop limit are unchanged, and a running "
        "process delivers to it exactly as before",
        file=out,
    )
    return 0


def _webhook_remove(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        record = await _find_webhook(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        removed = await persistence.webhooks.remove(record.id)
        return removed if isinstance(removed, Failed) else record

    outcome = asyncio.run(_with_rooms(database, work))
    refused = _webhook_outcome(outcome, args.name)
    if refused is not None:
        return refused
    print(
        f"webhook {outcome.name!r} is removed; a running process stops delivering "
        "to it from the next event",
        file=out,
    )
    return 0


def _webhook_test(args: argparse.Namespace, config: Config, out: IO[str]) -> int:
    assert config.database is not None
    secret = config.secret_key_bytes()
    trigger = Trigger(args.trigger)

    async def work(persistence: Persistence) -> Any:
        record = await _find_webhook(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        opened = await persistence.webhooks.open_url(record.id, secret)
        if isinstance(opened, Failed) or opened.value is None:
            return opened if isinstance(opened, Failed) else None
        return opened.value, await send_sample(opened.value, trigger)

    outcome = asyncio.run(_with_rooms(config.database, work))
    refused = _webhook_outcome(outcome, args.name)
    if refused is not None:
        return refused
    opened, result = outcome
    target = f"{opened.record.name!r} ({opened.record.url_host})"
    if result.delivered:
        print(f"test {trigger.value} sent to {target}: delivered, {result.summary}", file=out)
        return 0
    detail = result.summary
    if result.status is not None and result.reason:
        detail = f"{detail}, {result.reason}"
    print(f"test {trigger.value} sent to {target}: failed, {detail}", file=out)
    return 1


# --- sighop channel -----------------------------------------------------------

PSK_IS_READ_FROM_STDIN = (
    "A pre-shared key is read from standard input or generated, never taken as an "
    "argument: arguments are visible in process listings and shell history, and "
    "the key is the credential for reading and posting in the channel"
)

CHANNELS_NEED_A_DATABASE = (
    "no database is configured: channels are stored configuration and require "
    "durable storage, so this command needs one. Set DATABASE_URL or pass "
    "--database-url"
)

CHANNEL_CHANGES_REACH_A_RUNNING_PROCESS = (
    "a running run applies this within 60 s; a change made in a run's own web "
    "interface applies at once"
)


def _channel_command(args: argparse.Namespace, out: IO[str]) -> int:
    try:
        config = Config.from_environment(database_url=getattr(args, "database_url", None))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if config.database is None:
        print(CHANNELS_NEED_A_DATABASE, file=sys.stderr)
        return 2
    try:
        match args.channel_command:
            case "add":
                return _channel_add(args, config, out)
            case "list":
                return _channel_list(config.database, out)
            case "show":
                return _channel_show(args, config.database, out)
            case "rename":
                return _channel_rename(args, config.database, out)
            case "remove":
                return _channel_remove(args, config.database, out)
            case "key":
                return _channel_key(args, config, out)
            case _:
                return _channel_history(args, config.database, out)
    except (ChannelConfigError, ConfigError, SealError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _optional_secret(config: Config) -> bytes | None:
    try:
        return config.secret_key_bytes()
    except ConfigError:
        return None


def _render_channel(record: ChannelRecord, out: IO[str], *, messages: int | None = None) -> None:
    print(f"channel    {record.name}", file=out)
    print(f"kind       {record.kind}", file=out)
    print(f"hash       {record.channel_hash:02x}", file=out)
    if record.hashtag is not None:
        print(f"hashtag    {record.hashtag}", file=out)
    if messages is not None:
        print(f"messages   {messages}", file=out)
    if record.guessable:
        print(f"guessable  yes — {GUESSABLE_STATEMENT}", file=out)
    else:
        print("guessable  no — readable only by whoever holds the pre-shared key", file=out)


def _outcome_or_refusal(outcome: Any, name: str) -> int | None:
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    if outcome is None:
        print(f"no channel named {name!r}", file=sys.stderr)
        return 2
    return None


def _channel_add(args: argparse.Namespace, config: Config, out: IO[str]) -> int:
    assert config.database is not None
    generated: str | None = None
    if args.psk_stdin or args.generate:
        if not args.name:
            print("a pre-shared-key channel needs --name", file=sys.stderr)
            return 2
        secret = config.secret_key_bytes()
        if args.generate:
            generated = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
            key = generated
        elif sys.stdin.isatty():
            import getpass

            key = getpass.getpass("pre-shared key (base64): ")
        else:
            key = sys.stdin.readline()

        async def work(persistence: Persistence) -> Any:
            return await persistence.channels.add_psk(key, name=args.name, secret=secret)

    elif args.hashtag is not None:
        optional = _optional_secret(config)

        async def work(persistence: Persistence) -> Any:
            return await persistence.channels.add_hashtag(
                args.hashtag, name=args.name, secret=optional
            )

    else:

        async def work(persistence: Persistence) -> Any:
            return await persistence.channels.add_public(name=args.name or PUBLIC_CHANNEL_NAME)

    outcome = asyncio.run(_with_rooms(config.database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    record = outcome.value
    _render_channel(record, out)
    if generated is not None:
        print(f"key        {generated}", file=out)
        print(
            "this key is the credential for reading and posting in the channel, and is "
            f"printed once; `sighop channel key {record.name}` prints it again",
            file=out,
        )
    if record.kind is ChannelKind.PUBLIC:
        print(
            "Public is flooded to the whole mesh; adding it transmits nothing, and "
            "posting still needs the run's transmit gate",
            file=out,
        )
    print(
        f"channel {record.name!r} is added; {CHANNEL_CHANGES_REACH_A_RUNNING_PROCESS}",
        file=out,
    )
    return 0


def _channel_list(database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        listed = await persistence.channels.list_all()
        if isinstance(listed, Failed):
            return listed
        counts = await persistence.channels.message_counts()
        if isinstance(counts, Failed):
            return counts
        return listed.value, counts.value

    outcome = asyncio.run(_with_rooms(database, work))
    if isinstance(outcome, Failed):
        print(str(outcome.error), file=sys.stderr)
        return 2
    records, counts = outcome
    if not records:
        print("no channels are configured; group text is left undecrypted", file=out)
        return 0
    for record in records:
        marking = "guessable" if record.guessable else "private"
        print(
            f"{record.name}  kind={record.kind}  hash={record.channel_hash:02x}  "
            f"{marking}  messages={counts.get(record.id, 0)}",
            file=out,
        )
    if any(record.guessable for record in records):
        print(f"guessable: {GUESSABLE_STATEMENT}", file=out)
    return 0


async def _find_channel(persistence: Persistence, name: str) -> Any:
    found = await persistence.channels.get(name)
    if isinstance(found, Failed):
        return found
    return found.value


def _channel_rename(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    """Change a channel's name. Its key and channel hash are untouched."""

    async def work(persistence: Persistence) -> Any:
        record = await _find_channel(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        renamed = await persistence.channels.rename(record.id, args.new_name)
        return renamed if isinstance(renamed, Failed) else record

    outcome = asyncio.run(_with_rooms(database, work))
    refused = _outcome_or_refusal(outcome, args.name)
    if refused is not None:
        return refused
    print(f"renamed {outcome.name} -> {args.new_name.strip()}", file=out)
    print(
        "its kind, key and channel hash are unchanged, and a running process "
        "decrypts on it exactly as before",
        file=out,
    )
    return 0


def _channel_show(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def work(persistence: Persistence) -> Any:
        record = await _find_channel(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        counted = await persistence.channels.message_count(record.id)
        return counted if isinstance(counted, Failed) else (record, counted.value)

    outcome = asyncio.run(_with_rooms(database, work))
    refused = _outcome_or_refusal(outcome, args.name)
    if refused is not None:
        return refused
    record, messages = outcome
    _render_channel(record, out, messages=messages)
    return 0


def _channel_remove(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    async def count(persistence: Persistence) -> Any:
        record = await _find_channel(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        counted = await persistence.channels.message_count(record.id)
        return counted if isinstance(counted, Failed) else (record, counted.value)

    outcome = asyncio.run(_with_rooms(database, count))
    refused = _outcome_or_refusal(outcome, args.name)
    if refused is not None:
        return refused
    record, messages = outcome
    consequence = (
        f"removing channel {record.name!r} deletes its {messages} recorded message(s); "
        "they cannot be recovered"
    )
    if not args.delete_history:
        if not sys.stdin.isatty():
            print(
                f"{consequence}. Nothing was removed: confirm at a terminal, or pass "
                "--delete-history to accept it",
                file=sys.stderr,
            )
            return 2
        print(consequence, file=out)
        answer = input(f"type the channel name ({record.name}) to remove it: ")
        if answer.strip() != record.name:
            print("not confirmed; nothing was removed", file=sys.stderr)
            return 2

    async def remove(persistence: Persistence) -> Any:
        return await persistence.channels.remove(record.id)

    removed = asyncio.run(_with_rooms(database, remove))
    if isinstance(removed, Failed):
        print(str(removed.error), file=sys.stderr)
        return 2
    if removed.value is None:
        print(f"no channel named {record.name!r}", file=sys.stderr)
        return 2
    print(
        f"channel {record.name!r} is removed with {removed.value} recorded message(s); "
        f"{CHANNEL_CHANGES_REACH_A_RUNNING_PROCESS}",
        file=out,
    )
    return 0


def _channel_key(args: argparse.Namespace, config: Config, out: IO[str]) -> int:
    assert config.database is not None
    secret = _optional_secret(config)

    async def work(persistence: Persistence) -> Any:
        record = await _find_channel(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        key = await persistence.channels.key_of(record.name, secret)
        return key if isinstance(key, Failed) else (record, key.value)

    outcome = asyncio.run(_with_rooms(config.database, work))
    refused = _outcome_or_refusal(outcome, args.name)
    if refused is not None:
        return refused
    record, key = outcome
    print(base64.b64encode(key).decode("ascii"), file=out)
    if record.guessable:
        print(
            f"this key is derivable by anyone: {GUESSABLE_STATEMENT}",
            file=out,
        )
    return 0


def _channel_history(args: argparse.Namespace, database: DatabaseConfig, out: IO[str]) -> int:
    if args.limit <= 0:
        print("--limit must be positive", file=sys.stderr)
        return 2

    async def work(persistence: Persistence) -> Any:
        record = await _find_channel(persistence, args.name)
        if record is None or isinstance(record, Failed):
            return record
        history = await persistence.channel_messages.recent(record.id, limit=args.limit)
        if isinstance(history, Failed):
            return history
        entities = await persistence.entities.list_all()
        names = (
            {entity.public_key: entity.name for entity in entities.value}
            if isinstance(entities, Succeeded)
            else {}
        )
        return record, history.value, names

    outcome = asyncio.run(_with_rooms(database, work))
    refused = _outcome_or_refusal(outcome, args.name)
    if refused is not None:
        return refused
    record, messages, names = outcome
    if not messages:
        print(f"channel {record.name!r} has no recorded messages", file=out)
        return 0
    print(
        f"channel {record.name!r}, oldest first. Sender names are claims: anyone "
        "holding the channel key can write any name",
        file=out,
    )
    for message in reversed(messages):
        print(_render_channel_history_line(message, names), file=out)
    return 0


def _render_channel_history_line(message: ChannelMessageRecord, names: dict[bytes, str]) -> str:
    when = message.handled_at.isoformat(timespec="seconds")
    text = message.rendered().text
    if message.inbound:
        sender = (
            "no sender"
            if message.unverified_sender_name is None
            else f"claimed {message.unverified_sender_name!r}"
        )
        hops = "?" if message.hop_count is None else str(message.hop_count)
        return f"{when}  ✗ {sender} (unverified)  h{hops}: {text}"
    # The entity store names identities it holds; a `run --entity keyfile.json`
    # identity was never in it, so "no longer stored" said the one thing that is
    # not true of the commonest case. The key is printed instead, which is what
    # tells the two apart: it is the same key `sighop keys list` shows.
    key = message.entity_public_key
    if key is None:
        identity = "an identity whose key was not recorded"
    else:
        identity = names.get(
            key,
            f"{key.hex()[:16]}… (not in the entity store: a keyfile identity, or one removed)",
        )
    match message.outcome:
        case ChannelOutcome.TRANSMITTED:
            state = (
                f"transmitted, {message.repeats_heard} repeat(s) heard "
                "(repeater evidence, not delivery)"
            )
        case ChannelOutcome.AWAITING:
            state = "awaiting transmission"
        case ChannelOutcome.NOT_TRANSMITTED:
            state = f"not transmitted: {message.outcome_reason or 'no reason recorded'}"
        case _:
            state = "outcome unknown — the run stopped before it resolved"
    return f"{when}  -> posted as {identity}: {text}  [{state}]"


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
            case "rename":
                return _keys_rename(args, stream)
            case "delete":
                return _keys_delete(args, stream)
            case _:
                return _keys_export(args, stream)

    if args.command == "room":
        configure_logging(stream=sys.stderr)
        return _room_command(args, stream)

    if args.command == "bot":
        configure_logging(stream=sys.stderr)
        return _bot_command(args, stream)

    if args.command == "web":
        configure_logging(stream=sys.stderr)
        return _web_user_command(args, stream)

    if args.command == "webhook":
        configure_logging(stream=sys.stderr)
        return _webhook_command(args, stream)

    if args.command == "channel":
        configure_logging(stream=sys.stderr)
        return _channel_command(args, stream)

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
    except (
        ConfigError,
        DatabaseError,
        EntityLoadError,
        SealError,
        WebBindError,
        WebStartupError,
    ) as exc:
        # A configured database that cannot be reached, is unauthenticated, is
        # at the wrong revision, or whose keys will not open is a *startup*
        # failure that applies nothing and transmits nothing (`database` and
        # `entity-store` specs). It is reported here rather than as a traceback,
        # and the message names both revisions or the variable at fault.
        #
        # A web interface that cannot bind joins them for the same reason: the
        # run was asked to serve a panel on a port, and continuing without one
        # would present a node whose interface an operator had already assumed.
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
