"""The `sighop bot` command surface, and bot identities (groups 3 and 10).

Two rules here are load-bearing rather than stylistic:

* **every output that changes what a bot may do says what it may now do.**
  Creating one says it will transmit nothing until it is made active; making it
  active says the run's transmit flag still applies; clearing its state says
  what the bot will do again. A posture an operator has to infer gets inferred
  wrongly, and this one ends in an unsolicited message to a stranger.
* **being a bot is an explicit choice at creation**, never a side effect of
  binding a bot to an identity — which is what keeps `sighop keys list` an
  honest answer to "what does this node run".
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
from sighop.keystore import create_keyfile
from sighop.net.dm import MAX_TEXT_LEN
from sighop.protocol.identity import generate_identity
from sighop.protocol.payloads import NodeType

SECRET = base64.b64decode(generate_secret_key())


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
def store_environment(
    database: Database, database_config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch
) -> str:
    """The command line pointed at an emptied throwaway schema.

    `database` is depended on for its truncation rather than for the handle: a
    bot is looked up by the name of the identity it runs on, so a previous
    test's leftover identity would make an exact name ambiguous.
    """
    monkeypatch.setenv(SECRET_KEY_VARIABLE, base64.b64encode(SECRET).decode())
    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    return database_config.url


async def _bot_identity(
    url: str, tmp_path, *, name: str = "greeter-bot", node_type: str = "CHAT", as_bot: bool = True
) -> str:
    keyfile = create_keyfile(tmp_path / f"{name}.json", name, node_type=NodeType[node_type])
    out = io.StringIO()
    argv = ["keys", "import", str(keyfile.path), "--database-url", url]
    if as_bot:
        argv.append("--bot")
    assert await _cli(argv, out) == 0, out.getvalue()
    return name


async def _created_bot(url: str, tmp_path, *, name: str = "greeter-bot") -> str:
    await _bot_identity(url, tmp_path, name=name)
    out = io.StringIO()
    assert (
        await _cli(["bot", "create", name, "--driver", "greeter", "--database-url", url], out) == 0
    ), out.getvalue()
    return name


# --- The parser itself ------------------------------------------------------


def test_the_bot_noun_offers_every_action_the_spec_asks_for() -> None:
    parser = build_parser()

    for action in (
        "create",
        "list",
        "show",
        "enable",
        "disable",
        "mode",
        "set",
        "greeted",
        "state",
    ):
        args = parser.parse_args(
            ["bot", action, *(["greeter-bot"] if action not in ("list",) else [])]
            + (["--driver", "greeter"] if action == "create" else [])
            + (["observe"] if action == "mode" else [])
            + (["greeting", "hi"] if action == "set" else [])
        )
        assert args.command == "bot"
        assert args.bot_command == action


def test_a_mode_outside_observe_and_active_is_refused_by_the_parser() -> None:
    """10.3: the two modes are a closed set, and the parser is where that is
    cheapest to enforce."""
    with pytest.raises(SystemExit):
        build_parser().parse_args(["bot", "mode", "greeter-bot", "chatty"])


# --- 3.1 / 3.2 A bot identity -----------------------------------------------


@pytest.mark.database
async def test_an_identity_can_be_imported_as_a_bot_and_reports_both_types(
    store_environment: str, tmp_path
) -> None:
    """3.1: the stored type says bot, the advert configuration says chat node."""
    out = io.StringIO()
    keyfile = create_keyfile(tmp_path / "b.json", "greeter-bot", node_type=NodeType.CHAT)

    code = await _cli(
        ["keys", "import", str(keyfile.path), "--bot", "--database-url", store_environment], out
    )

    assert code == 0
    assert "type       bot" in out.getvalue()
    assert "node_type  CHAT" in out.getvalue(), (
        "a bot presents itself to the mesh as a chat node, indistinguishable from a companion"
    )

    listed = io.StringIO()
    assert await _cli(["keys", "list", "--database-url", store_environment], listed) == 0
    assert "type=bot" in listed.getvalue()
    assert "node_type=CHAT" in listed.getvalue()


@pytest.mark.database
async def test_an_identity_that_adverts_as_something_else_cannot_be_a_bot(
    store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """3.1: forcing the node type silently would make the stored identity
    disagree with the keyfile it came from."""
    keyfile = create_keyfile(tmp_path / "r.json", "rs", node_type=NodeType.ROOM_SERVER)

    code = await _cli(
        ["keys", "import", str(keyfile.path), "--bot", "--database-url", store_environment],
        io.StringIO(),
    )

    assert code == 2
    assert "chat node" in capsys.readouterr().err


@pytest.mark.database
async def test_importing_without_the_flag_stores_an_ordinary_companion(
    store_environment: str, tmp_path
) -> None:
    """3.1: an entity does not become a bot by accident."""
    await _bot_identity(store_environment, tmp_path, name="plain", as_bot=False)
    out = io.StringIO()

    assert await _cli(["keys", "list", "--database-url", store_environment], out) == 0
    assert "type=companion" in out.getvalue()


@pytest.mark.database
async def test_no_bot_output_discloses_key_material(store_environment: str, tmp_path) -> None:
    """3.2: the same rule every other surface follows, asserted against output.

    The seed is sealed exactly as any other entity's — being a bot changes
    nothing about how the identity is stored — and there is no flag anywhere
    that would print one.
    """
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    assert await _cli(["bot", "show", name, "--database-url", store_environment], out) == 0
    assert await _cli(["bot", "list", "--database-url", store_environment], out) == 0
    assert await _cli(["keys", "list", "--database-url", store_environment], out) == 0

    text = out.getvalue().lower()
    for forbidden in ("seed", "sealed", "private"):
        assert forbidden not in text


# --- 10.1 Creating ----------------------------------------------------------


@pytest.mark.database
async def test_creating_a_bot_states_that_it_will_transmit_nothing_yet(
    store_environment: str, tmp_path
) -> None:
    """10.1: enabled, observing, and saying so — the safety posture, printed."""
    await _bot_identity(store_environment, tmp_path)
    out = io.StringIO()

    code = await _cli(
        [
            "bot",
            "create",
            "greeter-bot",
            "--driver",
            "greeter",
            "--database-url",
            store_environment,
        ],
        out,
    )

    assert code == 0
    text = out.getvalue()
    assert "driver     greeter" in text
    assert "mode       observe" in text
    assert "enabled    yes" in text
    assert "transmit nothing until" in text
    assert "--enable-transmit" in text
    assert "greeting" in text, "the driver's default configuration is shown"
    assert "seeded     nothing" in text, "an empty contact table seeds nothing, and says so"


@pytest.mark.database
async def test_creating_a_bot_with_an_unknown_driver_lists_the_ones_that_exist(
    store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    await _bot_identity(store_environment, tmp_path)

    code = await _cli(
        [
            "bot",
            "create",
            "greeter-bot",
            "--driver",
            "weather",
            "--database-url",
            store_environment,
        ],
        io.StringIO(),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "weather" in error
    assert "greeter" in error


@pytest.mark.database
async def test_a_second_bot_on_one_identity_is_refused(
    store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = await _created_bot(store_environment, tmp_path)

    code = await _cli(
        ["bot", "create", name, "--driver", "greeter", "--database-url", store_environment],
        io.StringIO(),
    )

    assert code == 2
    assert "already carries" in capsys.readouterr().err


@pytest.mark.database
async def test_a_bot_on_a_room_servers_identity_is_refused_saying_it_has_a_role(
    store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """10.1: the identity already plays a role, and the refusal says which."""
    keyfile = create_keyfile(tmp_path / "rs.json", "rs-1", node_type=NodeType.ROOM_SERVER)
    assert (
        await _cli(
            ["keys", "import", str(keyfile.path), "--database-url", store_environment],
            io.StringIO(),
        )
        == 0
    )

    code = await _cli(
        ["bot", "create", "rs-1", "--driver", "greeter", "--database-url", store_environment],
        io.StringIO(),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert "not a bot" in error
    assert "keys import --bot" in error


# --- 10.2 Listing and showing -----------------------------------------------


@pytest.mark.database
async def test_listing_says_none_rather_than_printing_nothing(
    store_environment: str,
) -> None:
    out = io.StringIO()

    assert await _cli(["bot", "list", "--database-url", store_environment], out) == 0
    assert "no bots are configured" in out.getvalue()


@pytest.mark.database
async def test_showing_a_bot_reports_every_field_the_spec_names(
    store_environment: str, tmp_path
) -> None:
    """10.2: identity, driver, mode, enablement, configuration and limits."""
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    assert await _cli(["bot", "show", name, "--database-url", store_environment], out) == 0

    text = out.getvalue()
    assert f"bot        {name}" in text
    assert "driver     greeter" in text
    assert "mode       observe" in text
    assert "enabled    yes" in text
    for setting in ("greeting", "max_hops", "node_types", "min_snr_db", "rate_per_hour", "burst"):
        assert setting in text
    assert "state_keys 0" in text
    assert "counters are reported by the run" in text


@pytest.mark.database
async def test_showing_a_bot_that_does_not_exist_says_so(
    store_environment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _cli(["bot", "show", "nobody", "--database-url", store_environment], io.StringIO())

    assert code == 2
    assert "no bot named" in capsys.readouterr().err


# --- 10.3 Enablement and mode -----------------------------------------------


@pytest.mark.database
async def test_disabling_and_enabling_change_the_stored_value_and_say_what_follows(
    store_environment: str, tmp_path
) -> None:
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    assert await _cli(["bot", "disable", name, "--database-url", store_environment], out) == 0
    assert "receives no events" in out.getvalue()
    assert "reports it as not running" in out.getvalue()

    shown = io.StringIO()
    await _cli(["bot", "show", name, "--database-url", store_environment], shown)
    assert "enabled    no" in shown.getvalue()

    back = io.StringIO()
    assert await _cli(["bot", "enable", name, "--database-url", store_environment], back) == 0
    assert "will run" in back.getvalue()


@pytest.mark.database
async def test_making_a_bot_active_states_the_run_flag_still_applies(
    store_environment: str, tmp_path
) -> None:
    """10.3, design D4: two gates, and the operator who just opened one is
    exactly the person who needs telling about the other."""
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    assert (
        await _cli(["bot", "mode", name, "active", "--database-url", store_environment], out) == 0
    )

    assert "may transmit" in out.getvalue()
    assert "--enable-transmit" in out.getvalue()

    shown = io.StringIO()
    await _cli(["bot", "show", name, "--database-url", store_environment], shown)
    assert "mode       active" in shown.getvalue()


# --- 10.4 Configuration -----------------------------------------------------


@pytest.mark.database
async def test_an_accepted_value_is_stored_as_the_driver_parsed_it(
    store_environment: str, tmp_path
) -> None:
    """10.4: `max_hops` comes back a number, not the string that was typed."""
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    assert (
        await _cli(["bot", "set", name, "max_hops", "3", "--database-url", store_environment], out)
        == 0
    )

    assert "max_hops       3" in out.getvalue()


@pytest.mark.database
async def test_a_rejected_value_carries_the_drivers_reason_and_changes_nothing(
    store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """10.4, design D14: refused when it is typed, and the stored row is
    untouched — which is the half a message alone would not prove."""
    name = await _created_bot(store_environment, tmp_path)

    code = await _cli(
        [
            "bot",
            "set",
            name,
            "greeting",
            "x" * (MAX_TEXT_LEN + 1),
            "--database-url",
            store_environment,
        ],
        io.StringIO(),
    )

    assert code == 2
    error = capsys.readouterr().err
    assert str(MAX_TEXT_LEN) in error
    assert "unchanged" in error

    shown = io.StringIO()
    await _cli(["bot", "show", name, "--database-url", store_environment], shown)
    assert "x" * 20 not in shown.getvalue()


@pytest.mark.database
async def test_the_runtimes_own_limits_are_settable_and_validated_here(
    store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """10.4: `rate_per_hour` and `burst` are the runtime's, not the driver's, and
    a driver is never asked to answer for them."""
    name = await _created_bot(store_environment, tmp_path)

    assert (
        await _cli(
            ["bot", "set", name, "burst", "5", "--database-url", store_environment], io.StringIO()
        )
        == 0
    )
    assert (
        await _cli(
            ["bot", "set", name, "burst", "0", "--database-url", store_environment], io.StringIO()
        )
        == 2
    )
    assert "disabling the bot" in capsys.readouterr().err


# --- 8.10 / 10.6 The greeting records -----------------------------------------


async def _seed_contacts(database_config: DatabaseConfig, *names: str) -> list[bytes]:
    """Put contacts in the durable table, as an earlier milestone would have."""
    from sighop.db.persistence import Persistence
    from sighop.net.contacts import Contact
    from sighop.protocol.payloads import WireText

    handle = Database(config=database_config)
    await handle.open()
    try:
        persistence = Persistence(database=handle)
        contacts = [
            Contact(
                public_key=generate_identity().public_key,
                name=WireText.from_bytes(name.encode()),
                advert_verified=True,
            )
            for name in names
        ]
        assert isinstance(await persistence.contacts.upsert_many(contacts), Succeeded)
        return [contact.public_key for contact in contacts]
    finally:
        await handle.dispose()


@pytest.mark.database
async def test_creating_a_greeter_seeds_the_contacts_this_node_already_knows(
    store_environment: str, tmp_path, database_config: DatabaseConfig
) -> None:
    """8.10, design D7: a new greeter starts owing nothing.

    Without the seed, dropping the `created` gate would oblige a greeter created
    on an established node to work through the whole contact table.
    """
    await _seed_contacts(database_config, "skogen", "hilltop", "kajen")
    await _bot_identity(store_environment, tmp_path)
    out = io.StringIO()

    code = await _cli(
        [
            "bot",
            "create",
            "greeter-bot",
            "--driver",
            "greeter",
            "--database-url",
            store_environment,
        ],
        out,
    )

    assert code == 0
    text = out.getvalue()
    assert "seeded     3 existing contacts" in text
    assert "owing nothing" in text
    assert "--clear" in text, "and how to release one"


@pytest.mark.database
async def test_a_seeded_contact_is_listed_as_seeded_rather_than_sent(
    store_environment: str, tmp_path, database_config: DatabaseConfig
) -> None:
    """10.6: the operator reading the record has to be able to tell them apart."""
    await _seed_contacts(database_config, "skogen")
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    assert await _cli(["bot", "greeted", name, "--database-url", store_environment], out) == 0

    text = out.getvalue()
    assert "seeded" in text
    assert "skogen" in text


@pytest.mark.database
async def test_a_greeter_with_no_records_says_it_owes_everybody(
    store_environment: str, tmp_path
) -> None:
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    assert await _cli(["bot", "greeted", name, "--database-url", store_environment], out) == 0
    assert "has greeted nobody" in out.getvalue()


@pytest.mark.database
async def test_clearing_one_contact_states_that_it_will_be_greeted(
    store_environment: str, tmp_path, database_config: DatabaseConfig
) -> None:
    """10.6: releasing a seeded contact is one command and one contact."""
    await _seed_contacts(database_config, "skogen", "hilltop")
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    code = await _cli(
        ["bot", "greeted", name, "skogen", "--clear", "--database-url", store_environment], out
    )

    assert code == 0
    text = out.getvalue()
    assert "skogen" in text, "the command names the contact it resolved"
    assert "will greet it the next time it adverts" in text

    shown = io.StringIO()
    await _cli(["bot", "greeted", name, "skogen", "--database-url", store_environment], shown)
    assert "no greeting record" in shown.getvalue()
    assert "will be greeted" in shown.getvalue()


@pytest.mark.database
async def test_setting_one_contact_states_that_it_never_will_be(
    store_environment: str, tmp_path, database_config: DatabaseConfig
) -> None:
    """10.6: excusing a contact, recorded as the operator's act rather than a send."""
    await _seed_contacts(database_config, "skogen")
    name = await _created_bot(store_environment, tmp_path)
    assert (
        await _cli(
            ["bot", "greeted", name, "skogen", "--clear", "--database-url", store_environment],
            io.StringIO(),
        )
        == 0
    )
    out = io.StringIO()

    code = await _cli(
        ["bot", "greeted", name, "skogen", "--set", "--database-url", store_environment], out
    )

    assert code == 0
    assert "will never greet it" in out.getvalue()

    shown = io.StringIO()
    await _cli(["bot", "greeted", name, "skogen", "--database-url", store_environment], shown)
    assert "operator" in shown.getvalue()


@pytest.mark.database
async def test_clearing_and_setting_at_once_is_refused(
    store_environment: str,
    tmp_path,
    database_config: DatabaseConfig,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _seed_contacts(database_config, "skogen")
    name = await _created_bot(store_environment, tmp_path)

    code = await _cli(
        [
            "bot",
            "greeted",
            name,
            "skogen",
            "--clear",
            "--set",
            "--database-url",
            store_environment,
        ],
        io.StringIO(),
    )

    assert code == 2
    assert "opposite things" in capsys.readouterr().err


@pytest.mark.database
async def test_an_unresolvable_peer_says_so_rather_than_guessing(
    store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    """10.6: the store's own resolution, ambiguity error included."""
    name = await _created_bot(store_environment, tmp_path)

    code = await _cli(
        ["bot", "greeted", name, "nobody", "--clear", "--database-url", store_environment],
        io.StringIO(),
    )

    assert code == 2
    assert "no contact matches" in capsys.readouterr().err


@pytest.mark.database
async def test_seeding_again_leaves_existing_records_alone(
    store_environment: str, tmp_path, database_config: DatabaseConfig
) -> None:
    """8.10: `--seed` is the remedy for a seed that did not complete, and it must
    never overwrite a record that says a greeting was actually sent."""
    await _seed_contacts(database_config, "skogen")
    name = await _created_bot(store_environment, tmp_path)
    # A contact heard after creation, which the greeter owes a greeting to.
    await _seed_contacts(database_config, "kajen")
    out = io.StringIO()

    code = await _cli(["bot", "greeted", name, "--seed", "--database-url", store_environment], out)

    assert code == 0
    assert "seeded 1 contacts" in out.getvalue()
    assert "already existed were left alone" in out.getvalue()


# --- 10.5 Durable state -----------------------------------------------------


@pytest.mark.database
async def test_inspecting_state_lists_what_is_stored(
    store_environment: str, tmp_path, database_config: DatabaseConfig
) -> None:
    from sighop.db.engine import Database, Succeeded
    from sighop.db.persistence import Persistence

    name = await _created_bot(store_environment, tmp_path)
    handle = Database(config=database_config)
    await handle.open()
    try:
        persistence = Persistence(database=handle)
        found = await persistence.bots.get_by_name(name)
        assert isinstance(found, Succeeded) and found.value is not None
        await persistence.bot_state.set(found.value.id, "greeted:aabb", {"outcome": "acknowledged"})
    finally:
        await handle.dispose()

    out = io.StringIO()
    assert await _cli(["bot", "state", name, "--database-url", store_environment], out) == 0
    assert "greeted:aabb" in out.getvalue()
    assert "acknowledged" in out.getvalue()


@pytest.mark.database
async def test_an_empty_state_says_so(store_environment: str, tmp_path) -> None:
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    assert await _cli(["bot", "state", name, "--database-url", store_environment], out) == 0
    assert "has stored nothing" in out.getvalue()


@pytest.mark.database
async def test_clearing_state_states_what_the_bot_will_do_again(
    store_environment: str, tmp_path, database_config: DatabaseConfig
) -> None:
    """10.5: forgetting a greeting record is how a stranger gets greeted twice,
    so the command says that rather than reporting a row count and stopping."""
    from sighop.db.engine import Database, Succeeded
    from sighop.db.persistence import Persistence

    name = await _created_bot(store_environment, tmp_path)
    handle = Database(config=database_config)
    await handle.open()
    try:
        persistence = Persistence(database=handle)
        found = await persistence.bots.get_by_name(name)
        assert isinstance(found, Succeeded) and found.value is not None
        await persistence.bot_state.set(found.value.id, "greeted:aabb", {})
    finally:
        await handle.dispose()

    out = io.StringIO()
    assert (
        await _cli(["bot", "state", name, "--clear", "--database-url", store_environment], out) == 0
    )

    text = out.getvalue()
    assert "cleared 1 keys" in text
    assert "will greet a previously greeted node again" in text


# --- Deleting a bot ----------------------------------------------------------


class _TypedAnswer:
    """`sys.stdin` as a terminal, and what the operator typed at it."""

    def __init__(self, answer: str) -> None:
        self._answer = answer

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        return self._answer + "\n"


async def _cli_at_terminal(argv: list[str], out: io.StringIO, answer: str) -> int:
    """`_cli`, but with a stdin that claims to be a terminal."""
    import sys

    def run() -> int:
        original = sys.stdin
        sys.stdin = _TypedAnswer(answer)
        try:
            return main(argv, out=out)
        finally:
            sys.stdin = original

    return await asyncio.to_thread(run)


def test_the_bot_noun_has_no_rename_and_says_where_one_lives() -> None:
    """A bot has no name of its own: it is named by its identity."""
    parser = build_parser()
    [bot_action] = [
        action
        for action in parser._subparsers._group_actions[0].choices["bot"]._actions
        if getattr(action, "choices", None)
    ]
    assert "rename" not in bot_action.choices
    assert "delete" in bot_action.choices
    assert "keys rename" in bot_action.choices["delete"].format_help()


@pytest.mark.database
async def test_bot_delete_states_what_it_forgets_and_keeps_the_identity(
    database: Database, store_environment: str, tmp_path
) -> None:
    name = await _created_bot(store_environment, tmp_path)
    persistence = Persistence(database=database)
    record = (await persistence.bots.list_all()).value[0]
    await persistence.bot_state.set(record.id, "greeted:aa", {})
    out = io.StringIO()

    code = await _cli_at_terminal(
        ["bot", "delete", name, "--database-url", store_environment], out, name
    )

    printed = out.getvalue()
    assert code == 0, printed
    assert "1 key(s)" in printed
    assert "greet a previously greeted node again" in printed
    assert f"deleted  {name}" in printed
    assert f"identity '{name}' was not deleted" in printed
    assert (await persistence.bots.list_all()).value == []
    assert (await persistence.bot_state.list(record.id)).value == {}
    assert [row.name for row in (await persistence.entities.list_all()).value] == [name]


@pytest.mark.database
async def test_bot_delete_with_no_terminal_and_no_flag_deletes_nothing(
    database: Database, store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = await _created_bot(store_environment, tmp_path)

    code = await _cli(["bot", "delete", name, "--database-url", store_environment], io.StringIO())

    assert code == 2
    error = capsys.readouterr().err
    assert "--delete-state" in error
    assert "Nothing was deleted" in error
    assert len((await Persistence(database=database).bots.list_all()).value) == 1


@pytest.mark.database
async def test_bot_delete_refuses_when_the_typed_name_is_wrong(
    database: Database, store_environment: str, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    name = await _created_bot(store_environment, tmp_path)

    code = await _cli_at_terminal(
        ["bot", "delete", name, "--database-url", store_environment], io.StringIO(), "nope"
    )

    assert code == 2
    assert "not confirmed" in capsys.readouterr().err
    assert len((await Persistence(database=database).bots.list_all()).value) == 1


@pytest.mark.database
async def test_bot_delete_accepts_the_flag_where_there_is_no_terminal(
    database: Database, store_environment: str, tmp_path
) -> None:
    name = await _created_bot(store_environment, tmp_path)
    out = io.StringIO()

    code = await _cli(
        ["bot", "delete", name, "--delete-state", "--database-url", store_environment], out
    )

    assert code == 0, out.getvalue()
    assert (await Persistence(database=database).bots.list_all()).value == []


@pytest.mark.database
async def test_deleting_a_bot_that_does_not_exist_says_so(
    database: Database, store_environment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    code = await _cli(
        ["bot", "delete", "nobody", "--delete-state", "--database-url", store_environment],
        io.StringIO(),
    )

    assert code == 2
    assert "no bot named 'nobody'" in capsys.readouterr().err
