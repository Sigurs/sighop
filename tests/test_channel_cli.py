"""The `sighop channel` command surface (channel-messaging task 7.1, design D9)."""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import io
import sys

import pytest

from sighop.cli import CHANNELS_NEED_A_DATABASE, PSK_IS_READ_FROM_STDIN, build_parser, main
from sighop.config import (
    DATABASE_SCHEMA_VARIABLE,
    SECRET_KEY_VARIABLE,
    DatabaseConfig,
    generate_secret_key,
)
from sighop.db.engine import Database, Succeeded
from sighop.db.repositories import ChannelMessageRepository, ChannelRepository
from sighop.net.channels import ChannelMessageRecord, ChannelOutcome
from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY, channel_key_from_hashtag

KEY = base64.b64encode(bytes(range(40, 56))).decode()
VERBS = ("add", "list", "show", "remove", "key", "history")
NOW = dt.datetime(2026, 9, 14, 18, 0, tzinfo=dt.UTC)


def _run(argv: list[str], stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    original_in, original_err = sys.stdin, sys.stderr
    sys.stdin, sys.stderr = io.StringIO(stdin), err
    try:
        code = main(argv, out=out)
    finally:
        sys.stdin, sys.stderr = original_in, original_err
    return code, out.getvalue(), err.getvalue()


async def _cli(argv: list[str], stdin: str = "") -> tuple[int, str, str]:
    return await asyncio.to_thread(_run, argv, stdin)


@pytest.fixture
def url(database_config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch) -> str:
    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    monkeypatch.setenv(SECRET_KEY_VARIABLE, generate_secret_key())
    return database_config.url


def _argv(verb: str) -> list[str]:
    match verb:
        case "add":
            return ["channel", "add", "--hashtag", "#dev-sighop"]
        case "list":
            return ["channel", "list"]
        case _:
            return ["channel", verb, "Public"]


# --- Without a database -------------------------------------------------------


@pytest.mark.parametrize("verb", VERBS)
def test_each_verb_refuses_without_a_database(verb: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code, out, err = _run(_argv(verb))
    assert code == 2
    assert CHANNELS_NEED_A_DATABASE in err and "durable storage" in err
    assert out == ""


def test_a_key_cannot_be_given_as_an_argument() -> None:
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["channel", "add", "--psk", KEY, "--name", "private"])
    add = parser._subparsers._group_actions[0].choices["channel"]
    add_parser = add._subparsers._group_actions[0].choices["add"]
    options = {option for action in add_parser._actions for option in action.option_strings}
    assert "--psk" not in options
    assert "read from standard input or generated" in add_parser.format_help().replace("\n", " ")
    assert PSK_IS_READ_FROM_STDIN.startswith("A pre-shared key is read from standard input")


# --- With a database ----------------------------------------------------------


@pytest.mark.database
async def test_adding_a_hashtag_states_hash_and_that_it_is_guessable(url: str) -> None:
    code, out, err = await _cli(
        ["channel", "add", "--hashtag", "#dev-sighop", "--database-url", url]
    )
    assert code == 0, err
    assert "channel    #dev-sighop" in out
    assert "kind       hashtag" in out
    assert f"hash       {channel_key_from_hashtag('#dev-sighop').channel_hash:02x}" in out
    assert "anyone who knows or guesses its name" in out
    assert "within 60 s" in out


@pytest.mark.database
async def test_a_psk_from_stdin_is_added_and_never_printed(url: str) -> None:
    code, out, err = await _cli(
        ["channel", "add", "--psk-stdin", "--name", "private", "--database-url", url], f"{KEY}\n"
    )
    assert code == 0, err
    assert KEY not in out and KEY not in err
    assert "guessable  no" in out

    code, listed, _ = await _cli(["channel", "list", "--database-url", url])
    assert code == 0
    assert "private  kind=psk" in listed and "private  messages=0" in listed
    assert "Public  kind=public  hash=11  guessable" in listed
    assert KEY not in listed


@pytest.mark.database
async def test_a_refused_psk_says_why(url: str) -> None:
    short = base64.b64encode(bytes(8)).decode()
    code, _out, err = await _cli(
        ["channel", "add", "--psk-stdin", "--name", "x", "--database-url", url], f"{short}\n"
    )
    assert code == 2 and "decodes to 8 bytes" in err and short not in err
    public = base64.b64encode(PUBLIC_CHANNEL_KEY.key).decode()
    code, _out, err = await _cli(
        ["channel", "add", "--psk-stdin", "--name", "x", "--database-url", url], f"{public}\n"
    )
    assert code == 2 and "already stored as the channel 'Public'" in err


@pytest.mark.database
async def test_a_generated_key_is_printed_once_with_how_to_print_it_again(url: str) -> None:
    code, out, err = await _cli(
        ["channel", "add", "--generate", "--name", "crew", "--database-url", url]
    )
    assert code == 0, err
    [line] = [line for line in out.splitlines() if line.startswith("key ")]
    generated = line.split()[-1]
    assert len(base64.b64decode(generated)) == 16
    assert "credential for reading and posting" in out
    assert "sighop channel key crew" in out

    code, printed, _ = await _cli(["channel", "key", "crew", "--database-url", url])
    assert code == 0 and printed.strip() == generated


@pytest.mark.database
async def test_printing_a_guessable_key_says_it_is_derivable(url: str) -> None:
    code, out, _ = await _cli(["channel", "key", "Public", "--database-url", url])
    assert code == 0
    assert out.splitlines()[0] == "izOH6cXN6mrJ5e26oRXNcg=="
    assert "derivable by anyone" in out


async def _history(database: Database, channel_id: int, count: int) -> None:
    history = ChannelMessageRepository(database=database)
    records = [
        ChannelMessageRecord(
            channel_id=channel_id,
            direction="in",
            ref=f"p{index}",
            text=b"hello",
            wire_timestamp=1,
            handled_at=NOW + dt.timedelta(seconds=index),
            outcome=ChannelOutcome.RECEIVED,
            unverified_sender_name="Sigurs",
            hop_count=2,
        )
        for index in range(count)
    ]
    assert isinstance(await history.upsert_many(records), Succeeded)


@pytest.mark.database
async def test_removing_states_the_count_and_refuses_non_interactively_without_the_flag(
    url: str, database: Database
) -> None:
    await _history(database, 1, 40)

    code, _out, err = await _cli(["channel", "remove", "Public", "--database-url", url])
    assert code == 2
    assert "deletes its 40 recorded message(s)" in err
    assert "--delete-history" in err
    assert isinstance(found := await ChannelRepository(database=database).get("Public"), Succeeded)
    assert found.value is not None, "a refused removal removed the channel"

    code, out, err = await _cli(
        ["channel", "remove", "Public", "--delete-history", "--database-url", url]
    )
    assert code == 0, err
    assert "removed with 40 recorded message(s)" in out
    listed = await ChannelRepository(database=database).list_all()
    assert isinstance(listed, Succeeded) and listed.value == []


@pytest.mark.database
async def test_history_renders_claims_as_unverified_and_posts_with_their_identity(
    url: str, database: Database
) -> None:
    await _history(database, 1, 1)
    history = ChannelMessageRepository(database=database)
    assert isinstance(
        await history.upsert(
            ChannelMessageRecord(
                channel_id=1,
                direction="out",
                ref="post",
                text=b"hi all",
                wire_timestamp=2,
                handled_at=NOW + dt.timedelta(seconds=10),
                outcome=ChannelOutcome.TRANSMITTED,
                entity_public_key=b"\x09" * 32,
                repeats_heard=2,
            )
        ),
        Succeeded,
    )

    code, out, err = await _cli(["channel", "history", "Public", "--database-url", url])
    assert code == 0, err
    lines = out.splitlines()
    assert "Sender names are claims" in lines[0]
    assert "✗ claimed 'Sigurs' (unverified)  h2: hello" in lines[1]
    # The key, not a claim about the store's history: an identity loaded from a
    # keyfile was never in the store, and is the commonest case on this line.
    assert "-> posted as 0909090909090909… (not in the entity store" in lines[2]
    assert "a keyfile identity, or one removed): hi all" in lines[2]
    assert "transmitted, 2 repeat(s) heard" in lines[2]


@pytest.mark.database
async def test_show_includes_the_message_count(url: str, database: Database) -> None:
    await _history(database, 1, 3)
    code, out, _ = await _cli(["channel", "show", "Public", "--database-url", url])
    assert code == 0 and "messages   3" in out
    code, _, err = await _cli(["channel", "show", "absent", "--database-url", url])
    assert code == 2 and "no channel named 'absent'" in err
