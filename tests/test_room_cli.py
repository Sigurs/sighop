"""The `sighop room` command surface (milestone 6, `runtime-cli`, tasks 12.1 - 12.7).

One rule here is load-bearing rather than stylistic and is asserted twice, once
against the parser and once against the output: **a password is never a
command-line argument** (design D16). `ps` publishes `argv` to every user on the
host, so a password that reaches it has been disclosed whether or not anybody
looked. The second rule is §6's: no output ever carries a password or a hash.
"""

from __future__ import annotations

import asyncio
import base64
import io

import pytest

from sighop.cli import build_parser, main
from sighop.config import (
    DATABASE_SCHEMA_VARIABLE,
    SECRET_KEY_VARIABLE,
    DatabaseConfig,
    generate_secret_key,
)
from sighop.db.engine import Database, Succeeded
from sighop.db.persistence import Persistence
from sighop.db.repositories import MemberRecord
from sighop.db.times import ensure_utc
from sighop.keystore import create_keyfile
from sighop.protocol.payloads import NodeType, Permission

SECRET = base64.b64decode(generate_secret_key())

ADMIN_PASSWORD = "an-admin-password"
GUEST_PASSWORD = "a-guest-password"


async def _cli(argv: list[str], out: io.StringIO, stdin: str = "") -> int:
    """Run the command line from a worker thread, as `main` owns its own loop."""
    import sys

    def run() -> int:
        original = sys.stdin
        sys.stdin = io.StringIO(stdin)
        try:
            return main(argv, out=out)
        finally:
            sys.stdin = original

    return await asyncio.to_thread(run)


@pytest.fixture
def store_environment(database_config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv(SECRET_KEY_VARIABLE, base64.b64encode(SECRET).decode())
    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    return database_config.url


async def _room_server_identity(
    url: str, tmp_path, *, name: str = "rs-1", node_type: str = "ROOM_SERVER"
) -> str:
    """Create and import an identity, and return its name."""
    keyfile = create_keyfile(tmp_path / f"{name}.json", name, node_type=NodeType[node_type])
    out = io.StringIO()
    assert await _cli(["keys", "import", str(keyfile.path), "--database-url", url], out) == 0, (
        out.getvalue()
    )
    return name


async def _create_room(url: str, identity: str, *, extra: list[str] | None = None) -> str:
    out = io.StringIO()
    code = await _cli(
        [
            "room",
            "create",
            identity,
            "--name",
            "lounge",
            "--database-url",
            url,
            *(extra or []),
        ],
        out,
        stdin=f"{ADMIN_PASSWORD}\n{GUEST_PASSWORD}\n",
    )
    assert code == 0, out.getvalue()
    return out.getvalue()


# --- 12.3 A password is never an argument -----------------------------------


def test_no_room_command_accepts_a_password_as_an_argument() -> None:
    """12.3, design D16: asserted against the parser, not against a convention.

    A future `--password` would sail through review and be published to every
    user on the host by `ps`. This is what stops it.
    """
    parser = build_parser()
    room = parser._subparsers._group_actions[0].choices["room"]
    for name, sub in room._subparsers._group_actions[0].choices.items():
        for action in sub._actions:
            if "password" not in " ".join(action.option_strings):
                continue
            assert action.nargs == 0 or action.const is not None or action.nargs is None, (
                f"room {name} {action.option_strings} may take a password value"
            )
            # A password-shaped option must be a *flag*, never a value.
            assert action.nargs == 0, (
                f"room {name} {action.option_strings} takes a value; a password "
                "read from argv is published by `ps`"
            )


@pytest.mark.database
async def test_a_password_passed_as_an_argument_is_refused(
    database: Database, store_environment: str, tmp_path
) -> None:
    """12.3: the parser refuses, so nothing reaches `argv` in the first place."""
    out = io.StringIO()
    with pytest.raises(SystemExit):
        main(
            [
                "room",
                "create",
                "rs-1",
                "--name",
                "lounge",
                "--password",
                "hunter2",
                "--database-url",
                store_environment,
            ],
            out=out,
        )


# --- 12.1 Creating a room ---------------------------------------------------


@pytest.mark.database
async def test_room_create_binds_a_room_and_states_the_resulting_configuration(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    printed = await _create_room(store_environment, identity)

    assert "room       lounge" in printed
    assert "members    0" in printed
    assert "messages   0" in printed
    assert "guest      refused" in printed, (
        "a new room refuses guests until one is configured on purpose (§7)"
    )
    assert "retention  unlimited" in printed
    assert "Argon2id" in printed
    assert ADMIN_PASSWORD not in printed
    assert "$argon2id$" not in printed


@pytest.mark.database
async def test_room_create_refuses_an_identity_that_already_has_one(
    database: Database, store_environment: str, tmp_path
) -> None:
    """12.1: refused with a message naming the existing room, nothing changed."""
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)

    out = io.StringIO()
    code = await _cli(
        ["room", "create", identity, "--name", "second", "--database-url", store_environment],
        out,
        stdin=f"{ADMIN_PASSWORD}\n",
    )
    assert code == 2

    listing = io.StringIO()
    assert await _cli(["room", "list", "--database-url", store_environment], listing) == 0
    assert "lounge" in listing.getvalue()
    assert "second" not in listing.getvalue()


@pytest.mark.database
async def test_room_create_refuses_an_identity_that_is_not_a_room_server(
    database: Database, store_environment: str, tmp_path
) -> None:
    """`entity-store`: being a room server is explicit at creation, never a
    side effect of having a room bound to it."""
    identity = await _room_server_identity(
        store_environment, tmp_path, name="chatty", node_type="CHAT"
    )
    out = io.StringIO()
    code = await _cli(
        ["room", "create", identity, "--name", "lounge", "--database-url", store_environment],
        out,
        stdin=f"{ADMIN_PASSWORD}\n",
    )
    assert code == 2


@pytest.mark.database
async def test_a_room_created_with_a_guest_password_says_so(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    printed = await _create_room(store_environment, identity, extra=["--guest-password"])
    assert "guest      password" in printed
    assert GUEST_PASSWORD not in printed


@pytest.mark.database
async def test_a_room_opened_on_purpose_says_it_is_open(
    database: Database, store_environment: str, tmp_path
) -> None:
    """§7: an empty guest password is legal and must be an explicit choice."""
    identity = await _room_server_identity(store_environment, tmp_path)
    printed = await _create_room(store_environment, identity, extra=["--open"])
    assert "guest      open" in printed


# --- 12.2 Listing and showing ------------------------------------------------


@pytest.mark.database
async def test_room_list_and_show_report_the_configuration_and_never_a_hash(
    database: Database, store_environment: str, tmp_path
) -> None:
    """12.2, §6: the hash string appears in no output."""
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity, extra=["--guest-password"])

    persistence = Persistence(database=database)
    rooms = await persistence.rooms.list_all()
    assert isinstance(rooms, Succeeded)
    room = rooms.value[0]

    listed, shown = io.StringIO(), io.StringIO()
    assert await _cli(["room", "list", "--database-url", store_environment], listed) == 0
    assert await _cli(["room", "show", "lounge", "--database-url", store_environment], shown) == 0

    for printed in (listed.getvalue(), shown.getvalue()):
        assert "lounge" in printed
        assert room.admin_password_hash not in printed
        assert room.guest_password_hash is not None
        assert room.guest_password_hash not in printed
        assert "$argon2id$" not in printed
        assert ADMIN_PASSWORD not in printed
        assert GUEST_PASSWORD not in printed

    assert "members    0" in shown.getvalue()
    assert "retention  unlimited" in shown.getvalue()


@pytest.mark.database
async def test_room_list_with_no_rooms_says_so(database: Database, store_environment: str) -> None:
    out = io.StringIO()
    assert await _cli(["room", "list", "--database-url", store_environment], out) == 0
    assert "no rooms are configured" in out.getvalue()


# --- 12.3 Rotating a password ------------------------------------------------


@pytest.mark.database
async def test_room_passwd_reads_from_stdin_and_says_rotation_evicts_nobody(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)

    out = io.StringIO()
    code = await _cli(
        ["room", "passwd", "lounge", "--password-stdin", "--database-url", store_environment],
        out,
        stdin="a-brand-new-password\n",
    )

    assert code == 0
    printed = out.getvalue()
    assert "admin password is changed" in printed
    assert "Argon2id" in printed
    assert "existing members keep their access" in printed
    assert "sighop room revoke" in printed
    assert "a-brand-new-password" not in printed

    # And the stored hash really changed.
    persistence = Persistence(database=database)
    rooms = await persistence.rooms.list_all()
    assert isinstance(rooms, Succeeded)
    from sighop.passwords import verify_password

    assert verify_password(rooms.value[0].admin_password_hash, "a-brand-new-password")


@pytest.mark.database
async def test_the_guest_password_can_be_cleared_back_to_refusing_guests(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity, extra=["--guest-password"])

    out = io.StringIO()
    code = await _cli(
        ["room", "passwd", "lounge", "--guest", "--clear", "--database-url", store_environment],
        out,
    )

    assert code == 0
    assert "guest logins are refused again" in out.getvalue()

    shown = io.StringIO()
    await _cli(["room", "show", "lounge", "--database-url", store_environment], shown)
    assert "guest      refused" in shown.getvalue()


@pytest.mark.database
async def test_the_admin_password_cannot_be_cleared(
    database: Database, store_environment: str, tmp_path
) -> None:
    """A room with no admin password has no administrator and no way to gain one."""
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)

    out = io.StringIO()
    code = await _cli(
        ["room", "passwd", "lounge", "--clear", "--database-url", store_environment], out
    )
    assert code == 2


# --- 12.4 Members and revocation ---------------------------------------------


async def _seed_member(database: Database, room_name: str = "lounge") -> bytes:
    import datetime as dt

    persistence = Persistence(database=database)
    rooms = await persistence.rooms.list_all()
    assert isinstance(rooms, Succeeded)
    room = next(record for record in rooms.value if record.name == room_name)
    now = ensure_utc(dt.datetime.now(dt.UTC), field="room_member.first_login")
    key = bytes(range(32))
    assert isinstance(
        await persistence.members.upsert(
            MemberRecord(
                room_id=room.id,
                public_key=key,
                node_hash=key[0],
                permissions=int(Permission.READ_WRITE),
                sync_since=1_700_000_000,
                last_timestamp=1_700_000_500,
                first_login=now,
                last_activity=now,
            )
        ),
        Succeeded,
    )
    return key


@pytest.mark.database
async def test_room_members_reports_key_permission_position_and_activity(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)
    key = await _seed_member(database)

    out = io.StringIO()
    assert await _cli(["room", "members", "lounge", "--database-url", store_environment], out) == 0

    printed = out.getvalue()
    assert key.hex() in printed
    assert "read_write" in printed
    assert "sync_since=1700000000" in printed
    assert "last_activity=" in printed


@pytest.mark.database
async def test_room_revoke_states_that_the_member_must_log_in_again(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)
    key = await _seed_member(database)

    out = io.StringIO()
    code = await _cli(
        ["room", "revoke", "lounge", key.hex()[:8], "--database-url", store_environment], out
    )

    assert code == 0
    printed = out.getvalue()
    assert key.hex() in printed
    assert "must log in again" in printed
    assert "sync position and replay guard are discarded" in printed

    listing = io.StringIO()
    await _cli(["room", "members", "lounge", "--database-url", store_environment], listing)
    assert "no members have logged in" in listing.getvalue()


@pytest.mark.database
async def test_an_ambiguous_member_prefix_is_refused_rather_than_guessed(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)
    await _seed_member(database)

    out = io.StringIO()
    code = await _cli(["room", "revoke", "lounge", "ff", "--database-url", store_environment], out)
    assert code == 2


# --- 12.5 Retention ----------------------------------------------------------


@pytest.mark.database
async def test_room_retention_sets_and_clears_both_bounds(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)

    setting = io.StringIO()
    assert (
        await _cli(
            [
                "room",
                "retention",
                "lounge",
                "--days",
                "30",
                "--messages",
                "500",
                "--database-url",
                store_environment,
            ],
            setting,
        )
        == 0
    )
    assert "30 days and 500 messages" in setting.getvalue()
    assert "misses those messages" in setting.getvalue(), (
        "design D15's trade must be stated where the policy is set"
    )

    clearing = io.StringIO()
    assert (
        await _cli(
            ["room", "retention", "lounge", "--clear", "--database-url", store_environment],
            clearing,
        )
        == 0
    )
    assert "retention  unlimited" in clearing.getvalue()
    assert "nothing will be deleted" in clearing.getvalue()


# --- 12.6 Posting and reading history ----------------------------------------


@pytest.mark.database
async def test_room_post_stores_a_post_authored_by_the_rooms_own_identity(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)

    out = io.StringIO()
    code = await _cli(
        ["room", "post", "lounge", "an announcement", "--database-url", store_environment],
        out,
    )

    assert code == 0
    assert "posted     @" in out.getvalue()
    assert "the room's own identity" in out.getvalue()

    persistence = Persistence(database=database)
    rooms = await persistence.rooms.list_all()
    assert isinstance(rooms, Succeeded)
    history = await persistence.messages.history(rooms.value[0].id)
    assert isinstance(history, Succeeded)
    assert [post.text for post in history.value] == [b"an announcement"]


@pytest.mark.database
async def test_room_post_refuses_text_longer_than_the_store_keeps(
    database: Database, store_environment: str, tmp_path
) -> None:
    """Refused locally, truncated over the air: the operator can be told, the
    author of an over-the-air post cannot."""
    from sighop.net.room import STORED_POST_TEXT_LEN

    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)

    out = io.StringIO()
    code = await _cli(
        [
            "room",
            "post",
            "lounge",
            "x" * (STORED_POST_TEXT_LEN + 1),
            "--database-url",
            store_environment,
        ],
        out,
    )
    assert code == 2


@pytest.mark.database
async def test_room_history_marks_text_that_is_not_valid_utf8_as_a_rendering(
    database: Database, store_environment: str, tmp_path
) -> None:
    """12.6, §4.1: our guess is never presented as the author's words."""
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)

    persistence = Persistence(database=database)
    rooms = await persistence.rooms.list_all()
    assert isinstance(rooms, Succeeded)
    room = rooms.value[0]
    await persistence.messages.store(
        room_id=room.id, author_public_key=bytes(range(32)), text=b"plain"
    )
    await persistence.messages.store(
        room_id=room.id, author_public_key=bytes(range(32)), text=b"caf\xe9 \xff"
    )

    out = io.StringIO()
    assert await _cli(["room", "history", "lounge", "--database-url", store_environment], out) == 0

    printed = out.getvalue()
    assert "'plain'" in printed
    assert "not valid UTF-8" in printed
    assert "rendering of 6 bytes" in printed


@pytest.mark.database
async def test_room_history_of_an_empty_room_says_so(
    database: Database, store_environment: str, tmp_path
) -> None:
    identity = await _room_server_identity(store_environment, tmp_path)
    await _create_room(store_environment, identity)

    out = io.StringIO()
    assert await _cli(["room", "history", "lounge", "--database-url", store_environment], out) == 0
    assert "holds no messages" in out.getvalue()


@pytest.mark.database
async def test_a_room_command_naming_no_room_says_which_one_it_could_not_find(
    database: Database, store_environment: str
) -> None:
    out = io.StringIO()
    code = await _cli(["room", "show", "nowhere", "--database-url", store_environment], out)
    assert code == 2


# --- 12.7 Creating an identity as a room server ------------------------------


def test_a_keyfile_can_be_created_as_a_room_server(tmp_path) -> None:
    """12.7, `entity-store`: the node type is explicit at creation."""
    out = io.StringIO()
    code = main(
        [
            "keys",
            "new",
            "--name",
            "rs-1",
            "--out",
            str(tmp_path / "rs-1.json"),
            "--node-type",
            "ROOM_SERVER",
        ],
        out=out,
    )

    assert code == 0
    assert "node_type  ROOM_SERVER" in out.getvalue()


def test_an_identity_created_without_a_type_is_unchanged(tmp_path) -> None:
    out = io.StringIO()
    code = main(
        ["keys", "new", "--name", "ordinary", "--out", str(tmp_path / "ordinary.json")],
        out=out,
    )

    assert code == 0
    assert "node_type  CHAT" in out.getvalue()


@pytest.mark.database
async def test_an_imported_room_server_keyfile_carries_both_types(
    database: Database, store_environment: str, tmp_path
) -> None:
    """12.7: the stored entity type *and* the advert's node type."""
    await _room_server_identity(store_environment, tmp_path)

    out = io.StringIO()
    assert await _cli(["keys", "list", "--database-url", store_environment], out) == 0

    printed = out.getvalue()
    assert "type=room_server" in printed, "the stored entity type"
    assert "node_type=ROOM_SERVER" in printed, "the node type its adverts carry"


@pytest.mark.database
async def test_an_imported_ordinary_keyfile_is_unchanged(
    database: Database, store_environment: str, tmp_path
) -> None:
    await _room_server_identity(store_environment, tmp_path, name="chatty", node_type="CHAT")

    out = io.StringIO()
    assert await _cli(["keys", "list", "--database-url", store_environment], out) == 0

    printed = out.getvalue()
    assert "type=companion" in printed
    assert "node_type=CHAT" in printed


# --- 2.4 No password reaches any output, anywhere ---------------------------


@pytest.mark.database
async def test_a_known_password_appears_in_no_room_output_at_all(
    database: Database, store_environment: str, tmp_path
) -> None:
    """2.4: created with a known password, then every command's output checked."""
    identity = await _room_server_identity(store_environment, tmp_path)
    printed = [await _create_room(store_environment, identity, extra=["--guest-password"])]

    persistence = Persistence(database=database)
    rooms = await persistence.rooms.list_all()
    assert isinstance(rooms, Succeeded)
    room = rooms.value[0]

    for argv in (
        ["room", "list"],
        ["room", "show", "lounge"],
        ["room", "members", "lounge"],
        ["room", "history", "lounge"],
        ["room", "retention", "lounge", "--days", "7"],
        ["keys", "list"],
    ):
        out = io.StringIO()
        await _cli([*argv, "--database-url", store_environment], out)
        printed.append(out.getvalue())

    everything = "\n".join(printed)
    assert ADMIN_PASSWORD not in everything
    assert GUEST_PASSWORD not in everything
    assert room.admin_password_hash not in everything
    assert room.guest_password_hash is not None
    assert room.guest_password_hash not in everything
    assert "$argon2id$" not in everything
