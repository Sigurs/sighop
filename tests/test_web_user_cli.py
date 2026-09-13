"""The `sighop web user` command surface (tasks 4.1-4.5, milestone 9 design D2).

One test per verb for success and one per verb for the no-database refusal — a
test for the noun would pass while a verb that forgot the check did not.
"""

from __future__ import annotations

import asyncio
import io
import sys

import pytest

from sighop.cli import (
    ACCOUNTS_NEED_A_DATABASE,
    NO_ACCOUNT_LEFT,
    PASSWORD_IS_NEVER_AN_ARGUMENT,
    SESSIONS_END_WITHIN_A_MINUTE,
    build_parser,
    main,
)
from sighop.config import DATABASE_SCHEMA_VARIABLE, DatabaseConfig
from sighop.db.engine import Database, Succeeded
from sighop.db.repositories import WebUserRepository
from sighop.passwords import verify_password

PASSWORD = "a-web-password"
VERBS = ("add", "list", "passwd", "disable", "enable", "remove")


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
    """`main` owns its own loop, so it runs in a thread beside the test's."""
    return await asyncio.to_thread(_run, argv, stdin)


@pytest.fixture
def url(database_config: DatabaseConfig, monkeypatch: pytest.MonkeyPatch) -> str:
    assert database_config.schema is not None
    monkeypatch.setenv(DATABASE_SCHEMA_VARIABLE, database_config.schema)
    return database_config.url


@pytest.fixture
def no_database(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)


async def _add(url: str, name: str = "dev-operator") -> None:
    code, _, err = await _cli(
        ["web", "user", "add", name, "--password-stdin", "--database-url", url], f"{PASSWORD}\n"
    )
    assert code == 0, err


def _argv(verb: str, *extra: str) -> list[str]:
    return ["web", "user", verb, *(() if verb == "list" else ("dev-operator",)), *extra]


# --- 4.1 Each verb without a database ---------------------------------------


@pytest.mark.parametrize("verb", VERBS)
def test_each_verb_refuses_without_a_database(verb: str, no_database: None) -> None:
    code, out, err = _run(_argv(verb), f"{PASSWORD}\n")
    assert code == 2
    assert ACCOUNTS_NEED_A_DATABASE in err
    assert "stored in the database" in err
    assert out == ""


def test_every_verb_is_registered() -> None:
    parser = build_parser()
    web = parser._subparsers._group_actions[0].choices["web"]
    user = web._subparsers._group_actions[0].choices["user"]
    assert set(user._subparsers._group_actions[0].choices) == set(VERBS)


# --- 4.1 / 4.3 Each verb's success and its stated consequence ---------------


@pytest.mark.database
async def test_add_stores_an_enabled_account_and_says_where_it_can_sign_in(
    database: Database, url: str
) -> None:
    code, out, err = await _cli(
        ["web", "user", "add", "Dev-Operator", "--password-stdin", "--database-url", url],
        f"{PASSWORD}\n",
    )
    assert code == 0, err
    assert "account 'dev-operator' is added and enabled" in out
    assert "can sign in to the web interface of any run using this database" in out
    assert PASSWORD not in out + err
    stored = await WebUserRepository(database=database).get("dev-operator")
    assert isinstance(stored, Succeeded) and stored.value is not None
    assert stored.value.enabled
    assert verify_password(stored.value.password_hash, PASSWORD)


@pytest.mark.database
async def test_add_refuses_a_case_only_duplicate(database: Database, url: str) -> None:
    await _add(url)
    code, _, err = await _cli(
        ["web", "user", "add", "DEV-OPERATOR", "--password-stdin", "--database-url", url],
        f"{PASSWORD}\n",
    )
    assert code == 2
    assert "'dev-operator' already exists" in err


@pytest.mark.database
async def test_list_shows_the_columns_and_never_a_hash(database: Database, url: str) -> None:
    await _add(url)
    await _add(url, "dev-second")
    await _cli(["web", "user", "disable", "dev-second", "--database-url", url])
    code, out, err = await _cli(["web", "user", "list", "--database-url", url])
    assert code == 0, err
    header, *rows = out.strip().splitlines()
    assert header.split() == ["username", "enabled", "created", "password_set"]
    assert len(rows) == 2
    first = rows[0].split()
    assert first[0] == "dev-operator" and first[1] == "yes"
    assert rows[1].split()[:2] == ["dev-second", "no"]
    assert "$argon2id$" not in out + err


@pytest.mark.database
async def test_list_with_no_accounts_says_what_that_means(database: Database, url: str) -> None:
    code, out, _ = await _cli(["web", "user", "list", "--database-url", url])
    assert code == 0
    assert "sighop web user add" in out


@pytest.mark.database
async def test_passwd_changes_the_hash_and_says_sessions_end(database: Database, url: str) -> None:
    await _add(url)
    code, out, err = await _cli(
        ["web", "user", "passwd", "dev-operator", "--password-stdin", "--database-url", url],
        "another-password\n",
    )
    assert code == 0, err
    assert "the password for 'dev-operator' is changed" in out
    assert SESSIONS_END_WITHIN_A_MINUTE in out
    stored = await WebUserRepository(database=database).get("dev-operator")
    assert isinstance(stored, Succeeded) and stored.value is not None
    assert verify_password(stored.value.password_hash, "another-password")


@pytest.mark.database
async def test_disable_says_sessions_end(database: Database, url: str) -> None:
    await _add(url)
    await _add(url, "dev-second")
    code, out, err = await _cli(["web", "user", "disable", "dev-operator", "--database-url", url])
    assert code == 0, err
    assert "account 'dev-operator' is disabled" in out
    assert SESSIONS_END_WITHIN_A_MINUTE in out


@pytest.mark.database
async def test_enable_lets_the_account_sign_in_again(database: Database, url: str) -> None:
    await _add(url)
    await _add(url, "dev-second")
    await _cli(["web", "user", "disable", "dev-operator", "--database-url", url])
    code, out, err = await _cli(["web", "user", "enable", "dev-operator", "--database-url", url])
    assert code == 0, err
    assert "account 'dev-operator' is enabled" in out
    stored = await WebUserRepository(database=database).get("dev-operator")
    assert isinstance(stored, Succeeded) and stored.value is not None and stored.value.enabled


@pytest.mark.database
async def test_remove_says_sessions_end(database: Database, url: str) -> None:
    await _add(url)
    await _add(url, "dev-second")
    code, out, err = await _cli(["web", "user", "remove", "dev-operator", "--database-url", url])
    assert code == 0, err
    assert "account 'dev-operator' is removed" in out
    assert SESSIONS_END_WITHIN_A_MINUTE in out
    stored = await WebUserRepository(database=database).get("dev-operator")
    assert isinstance(stored, Succeeded) and stored.value is None


@pytest.mark.database
@pytest.mark.parametrize("verb", ["passwd", "disable", "enable", "remove"])
async def test_an_unknown_account_is_named(verb: str, database: Database, url: str) -> None:
    await _add(url, "dev-second")
    code, _, err = await _cli(
        _argv(verb, "--database-url", url, *(["--password-stdin"] if verb == "passwd" else [])),
        f"{PASSWORD}\n",
    )
    assert code == 2
    assert "no account named 'dev-operator'" in err


# --- 4.4 The last enabled account -------------------------------------------


@pytest.mark.database
@pytest.mark.parametrize("verb", ["disable", "remove"])
async def test_the_last_enabled_account_is_not_given_up_without_acknowledgement(
    verb: str, database: Database, url: str
) -> None:
    await _add(url)
    code, _, err = await _cli(["web", "user", verb, "dev-operator", "--database-url", url])
    assert code == 2
    assert NO_ACCOUNT_LEFT in err
    assert "no run could then start its web interface" in err
    count = await WebUserRepository(database=database).count_enabled()
    assert isinstance(count, Succeeded) and count.value == 1, "nothing was changed"

    code, _, err = await _cli(
        ["web", "user", verb, "dev-operator", "--allow-no-accounts", "--database-url", url]
    )
    assert code == 0, err
    count = await WebUserRepository(database=database).count_enabled()
    assert isinstance(count, Succeeded) and count.value == 0


@pytest.mark.database
async def test_a_disabled_account_can_be_removed_while_another_is_enabled(
    database: Database, url: str
) -> None:
    await _add(url)
    await _add(url, "dev-second")
    await _cli(["web", "user", "disable", "dev-second", "--database-url", url])
    code, _, err = await _cli(["web", "user", "remove", "dev-second", "--database-url", url])
    assert code == 0, err


# --- 4.2 Passwords: stdin, the double prompt, and never argv -----------------


@pytest.mark.database
async def test_a_password_is_read_from_standard_input(database: Database, url: str) -> None:
    await _add(url)
    stored = await WebUserRepository(database=database).get("dev-operator")
    assert isinstance(stored, Succeeded) and stored.value is not None
    assert verify_password(stored.value.password_hash, PASSWORD)


def test_a_mismatched_double_entry_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    """At a terminal the password is asked twice, and two different entries
    change nothing. No database is reached: the refusal comes first."""
    import getpass

    entries = iter(["first-entry", "second-entry"])
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": next(entries))
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://role:pw@127.0.0.1:1/unreachable")

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    original_in, original_err = sys.stdin, sys.stderr
    err = io.StringIO()
    sys.stdin, sys.stderr = Terminal(), err
    try:
        code = main(["web", "user", "add", "dev-operator"], out=io.StringIO())
    finally:
        sys.stdin, sys.stderr = original_in, original_err
    assert code == 2
    assert "the two entries differ" in err.getvalue()


def test_the_prompt_asks_twice_and_accepts_matching_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import getpass

    from sighop.cli import _read_password

    prompts: list[str] = []

    def fake(prompt: str = "") -> str:
        prompts.append(prompt)
        return "same"

    monkeypatch.setattr(getpass, "getpass", fake)

    class Terminal(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    assert _read_password("password: ", from_stdin=False, confirm=True) == "same"
    assert len(prompts) == 2


@pytest.mark.parametrize("verb", ["add", "passwd", "disable", "enable", "remove"])
def test_a_password_as_a_positional_argument_is_refused_saying_why(
    verb: str, no_database: None
) -> None:
    code, out, err = _run(["web", "user", verb, "dev-operator", "hunter2"])
    assert code == 2
    assert PASSWORD_IS_NEVER_AN_ARGUMENT in err
    assert "hunter2" not in out + err


def test_a_password_option_is_refused_saying_why(no_database: None) -> None:
    code, _, err = _run(["web", "user", "add", "dev-operator", "--password", "hunter2"])
    assert code == 2
    assert PASSWORD_IS_NEVER_AN_ARGUMENT in err
    assert "hunter2" not in err


def test_an_empty_password_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://role:pw@127.0.0.1:1/unreachable")
    code, _, err = _run(["web", "user", "add", "dev-operator", "--password-stdin"], "\n")
    assert code == 2
    assert "cannot be empty" in err


def test_no_web_user_option_takes_a_value_that_could_be_a_password() -> None:
    parser = build_parser()
    web = parser._subparsers._group_actions[0].choices["web"]
    user = web._subparsers._group_actions[0].choices["user"]
    for name, sub in user._subparsers._group_actions[0].choices.items():
        for action in sub._actions:
            if "password" in " ".join(action.option_strings):
                assert action.nargs == 0, f"web user {name} {action.option_strings} takes a value"
