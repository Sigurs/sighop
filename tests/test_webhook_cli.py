"""The `sighop webhook` command surface (webhook-notifications task 7.1)."""

from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

from sighop.cli import URL_IS_READ_FROM_STDIN, WEBHOOKS_NEED_A_DATABASE, build_parser, main
from sighop.config import (
    DATABASE_SCHEMA_VARIABLE,
    SECRET_KEY_VARIABLE,
    DatabaseConfig,
    generate_secret_key,
)
from sighop.db.engine import Database, Succeeded
from sighop.db.repositories import WebhookRepository
from tests.test_webhooks_transport import Receiver, receiver  # noqa: F401 - fixture

URL = "https://discord.com/api/webhooks/42/s3cr3t-token?wait=true"
SECRET_PARTS = ("/api/webhooks", "s3cr3t-token", "wait=true")
VERBS = ("add", "list", "show", "enable", "disable", "set", "set-url", "remove", "test")


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
        case "list":
            return ["webhook", "list"]
        case "add":
            return ["webhook", "add", "dev-hook", "--format", "json", "--trigger", "new_repeater"]
        case "set":
            return ["webhook", "set", "dev-hook", "--format", "json"]
        case "test":
            return ["webhook", "test", "dev-hook", "--trigger", "new_repeater"]
        case _:
            return ["webhook", verb, "dev-hook"]


def _no_secret(text: str) -> None:
    for part in SECRET_PARTS:
        assert part not in text


async def _add(
    url: str, *extra: str, name: str = "dev-hook", target: str = URL
) -> tuple[int, str, str]:
    return await _cli(
        [
            "webhook",
            "add",
            name,
            "--format",
            "discord",
            "--trigger",
            "new_repeater",
            *extra,
            "--database-url",
            url,
        ],
        f"{target}\n",
    )


# --- Without a database ---------------------------------------------------------


@pytest.mark.parametrize("verb", VERBS)
def test_each_verb_refuses_without_a_database(verb: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    code, out, err = _run(_argv(verb), f"{URL}\n")
    assert code == 2
    assert WEBHOOKS_NEED_A_DATABASE in err
    assert "durable storage" in err
    assert out == ""


def test_every_verb_is_registered_and_no_url_argument_exists() -> None:
    parser = build_parser()
    webhook = parser._subparsers._group_actions[0].choices["webhook"]
    actions = webhook._subparsers._group_actions[0].choices
    assert set(actions) == set(VERBS)
    for verb, action in actions.items():
        dests = {argument.dest for argument in action._actions}
        assert "url" not in dests, verb
        assert not any(
            "url" in option and option != "--database-url"
            for argument in action._actions
            for option in argument.option_strings
        )
    for verb in ("add", "set-url"):
        assert "standard input" in actions[verb].format_help()
    assert "standard input" in URL_IS_READ_FROM_STDIN


def test_a_url_given_as_an_argument_is_not_accepted() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["webhook", "add", "dev-hook", URL, "--format", "json", "--trigger", "new_repeater"]
        )


# --- With a database ------------------------------------------------------------


@pytest.mark.database
async def test_add_states_enablement_format_triggers_limit_and_host_only(
    database: Database, url: str
) -> None:
    code, out, err = await _add(url)
    assert code == 0, err
    assert "webhook    dev-hook" in out
    assert "target     https://discord.com" in out
    assert "format     discord" in out
    assert "triggers   new_repeater" in out
    assert "max_hops   none" in out
    assert "enabled    yes" in out
    assert "is added and enabled" in out
    assert "unencrypted" not in out
    _no_secret(out + err)
    stored = await WebhookRepository(database=database).get_by_name("dev-hook")
    assert isinstance(stored, Succeeded) and stored.value is not None


@pytest.mark.database
async def test_a_plain_http_url_is_stored_with_a_warning(database: Database, url: str) -> None:
    code, out, err = await _add(url, target="http://10.0.0.5:5678/webhook/abc")
    assert code == 0, err
    assert "cross the network unencrypted" in out
    assert "http://10.0.0.5:5678" in out and "/webhook/abc" not in out


@pytest.mark.database
@pytest.mark.parametrize(
    ("extra", "target", "reason"),
    [
        ((), "file:///etc/passwd", "http or https"),
        ((), "ftp://host/", "http or https"),
        (("--trigger", "new_chatter"), URL, "new_companion"),
        (("--max-hops", "-1"), URL, "negative"),
    ],
)
async def test_refusals_give_the_reason_and_store_nothing(
    database: Database, url: str, extra: tuple[str, ...], target: str, reason: str
) -> None:
    code, out, err = await _add(url, *extra, target=target)
    assert code == 2
    assert reason in err
    _no_secret(out + err)
    listed = await WebhookRepository(database=database).list_all()
    assert isinstance(listed, Succeeded) and listed.value == []


@pytest.mark.database
async def test_a_duplicate_name_is_refused(database: Database, url: str) -> None:
    assert (await _add(url))[0] == 0
    code, _, err = await _add(url)
    assert code == 2 and "already exists" in err


@pytest.mark.database
async def test_list_and_show_never_print_the_path(database: Database, url: str) -> None:
    await _add(url)
    repository = WebhookRepository(database=database)
    record = (await repository.get_by_name("dev-hook")).value
    import datetime as dt

    await repository.record_delivery(record.id, dt.datetime(2026, 9, 13, 10, tzinfo=dt.UTC))
    await repository.record_failure(
        record.id, dt.datetime(2026, 9, 13, 11, tzinfo=dt.UTC), "HTTP 404"
    )

    code, out, err = await _cli(["webhook", "list", "--database-url", url])
    assert code == 0, err
    assert "dev-hook  target=https://discord.com  format=discord" in out
    _no_secret(out)

    code, out, err = await _cli(["webhook", "show", "dev-hook", "--database-url", url])
    assert code == 0, err
    assert "last_ok    2026-09-13T10:00:00+00:00" in out
    assert "last_fail  2026-09-13T11:00:00+00:00 (HTTP 404)" in out
    assert "enabled    yes" in out
    _no_secret(out)


@pytest.mark.database
async def test_enable_disable_set_set_url_and_remove(database: Database, url: str) -> None:
    await _add(url)
    repository = WebhookRepository(database=database)

    code, out, _ = await _cli(["webhook", "disable", "dev-hook", "--database-url", url])
    assert code == 0 and "disabled" in out and "without a restart" in out
    assert not (await repository.get_by_name("dev-hook")).value.enabled
    code, out, _ = await _cli(["webhook", "enable", "dev-hook", "--database-url", url])
    assert code == 0 and "is enabled" in out

    code, out, err = await _cli(
        [
            "webhook",
            "set",
            "dev-hook",
            "--format",
            "json",
            "--trigger",
            "new_companion",
            "--trigger",
            "new_repeater",
            "--max-hops",
            "2",
            "--database-url",
            url,
        ]
    )
    assert code == 0, err
    assert "triggers   new_companion, new_repeater" in out and "max_hops   2" in out
    code, out, _ = await _cli(
        ["webhook", "set", "dev-hook", "--no-max-hops", "--database-url", url]
    )
    assert code == 0 and "max_hops   none" in out
    code, _, err = await _cli(["webhook", "set", "dev-hook", "--database-url", url])
    assert code == 2 and "nothing to change" in err
    code, _, err = await _cli(
        ["webhook", "set", "dev-hook", "--trigger", "nope", "--database-url", url]
    )
    assert code == 2 and "new_repeater" in err

    code, out, err = await _cli(
        ["webhook", "set-url", "dev-hook", "--database-url", url],
        "https://hooks.example.org/x/y?z=1\n",
    )
    assert code == 0, err
    assert "now targets https://hooks.example.org" in out and "/x/y" not in out

    code, out, _ = await _cli(["webhook", "remove", "dev-hook", "--database-url", url])
    assert code == 0 and "stops delivering" in out
    assert (await repository.get_by_name("dev-hook")).value is None
    code, _, err = await _cli(["webhook", "show", "dev-hook", "--database-url", url])
    assert code == 2 and "no webhook named" in err


@pytest.mark.database
async def test_test_sends_one_sample_and_reports_the_status(
    database: Database,
    url: str,
    receiver: Receiver,  # noqa: F811
) -> None:
    await _add(url, target=f"{receiver.base}/ok?token=hunter2")
    await _cli(["webhook", "disable", "dev-hook", "--database-url", url])

    code, out, err = await _cli(
        ["webhook", "test", "dev-hook", "--trigger", "new_companion", "--database-url", url]
    )
    assert code == 0, err
    assert "delivered, HTTP 204" in out
    assert "hunter2" not in out + err and "/ok" not in out + err
    [(_, _, body)] = receiver.requests
    embed = json.loads(body)["embeds"][0]
    assert embed["title"] == "[test] New companion heard"


@pytest.mark.database
async def test_test_reports_a_rejection_once(
    database: Database,
    url: str,
    receiver: Receiver,  # noqa: F811
) -> None:
    await _add(url, target=f"{receiver.base}/missing")
    code, out, _ = await _cli(
        ["webhook", "test", "dev-hook", "--trigger", "new_repeater", "--database-url", url]
    )
    assert code == 1
    assert "failed, HTTP 404" in out
    assert len(receiver.requests) == 1
