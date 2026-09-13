"""Sessions, revalidation, the throttle and the sign-in decision (tasks 5.1-5.5).

Everything here runs with an injected clock, an in-memory account store and a
hasher that counts rather than spends 64 MiB — milestone 9 design D16. The real
Argon2id path is `tests/test_web_login.py`'s.
"""

from __future__ import annotations

import datetime as dt

import pytest

from sighop.web.auth import (
    FREE_FAILURES,
    IDLE_SECONDS,
    LIFETIME_SECONDS,
    MAX_DELAY_SECONDS,
    MAX_SESSIONS,
    REVALIDATE_SECONDS,
    THROTTLE_KEYS,
    UNVERIFIED,
    Authenticator,
    LoginThrottle,
    SessionStore,
    delay_for,
    safe_next,
    token_key,
)
from tests.test_web_state import RecordingLogger
from tests.webfixtures import (
    OPERATOR,
    OPERATOR_PASSWORD,
    CountingHasher,
    ManualClock,
    MemoryAccounts,
    as_account_store,
    authenticator,
)

CLIENT = "192.0.2.10"
EPOCH = dt.datetime(2026, 9, 12, tzinfo=dt.UTC)


def _store(clock: ManualClock, logger: RecordingLogger | None = None) -> SessionStore:
    return SessionStore(clock=clock, logger=logger or RecordingLogger())


# --- 5.1 The session store --------------------------------------------------


def test_a_session_resolves_until_it_has_been_idle_for_the_limit() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    store = _store(clock, logger)
    token, _ = store.issue(OPERATOR, password_set_at=EPOCH)
    clock.advance(IDLE_SECONDS - 1)
    assert store.resolve(token) is not None
    clock.advance(IDLE_SECONDS)
    assert store.resolve(token) is None
    assert store.resolve(token) is None, "an ended session stays ended"
    ended = logger.named("web_session_ended")
    assert [event["reason"] for event in ended] == ["idle"]
    assert ended[0]["actor"] == OPERATOR


def test_continuous_activity_does_not_outlive_the_absolute_lifetime() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    store = _store(clock, logger)
    token, _ = store.issue(OPERATOR, password_set_at=EPOCH)
    # A chat pane polling every few seconds, for a day.
    elapsed = 0.0
    while elapsed + 3600 < LIFETIME_SECONDS:
        clock.advance(3600)
        elapsed += 3600
        assert store.resolve(token) is not None
    clock.advance(LIFETIME_SECONDS - elapsed)
    assert store.resolve(token) is None
    assert [event["reason"] for event in logger.named("web_session_ended")] == ["lifetime"]


def test_the_store_evicts_the_oldest_session_at_its_bound() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    store = _store(clock, logger)
    first, _ = store.issue(OPERATOR, password_set_at=EPOCH)
    tokens = [store.issue(OPERATOR, password_set_at=EPOCH)[0] for _ in range(MAX_SESSIONS - 1)]
    assert len(store) == MAX_SESSIONS
    newest, _ = store.issue(OPERATOR, password_set_at=EPOCH)
    assert len(store) == MAX_SESSIONS
    assert store.resolve(first) is None
    assert store.resolve(tokens[0]) is not None
    assert store.resolve(newest) is not None
    assert [event["reason"] for event in logger.named("web_session_ended")] == ["evicted"]


def test_the_store_never_holds_or_renders_a_raw_token() -> None:
    store = _store(ManualClock())
    token, session = store.issue(OPERATOR, password_set_at=EPOCH)
    assert session.key == token_key(token) != token
    assert token not in repr(store)
    assert token not in repr(session)
    assert session.csrf_token not in repr(session)
    for key, held in store._sessions.items():
        assert token not in key
        assert token not in repr(held)
        assert all(token != value for value in vars_of(held))


def vars_of(session: object) -> list[object]:
    return [getattr(session, name) for name in session.__slots__]  # type: ignore[attr-defined]


def test_each_session_has_its_own_csrf_token_and_identifier() -> None:
    store = _store(ManualClock())
    one_token, one = store.issue(OPERATOR, password_set_at=EPOCH)
    two_token, two = store.issue(OPERATOR, password_set_at=EPOCH)
    assert one_token != two_token
    assert one.csrf_token != two.csrf_token


def test_ending_a_session_makes_its_token_grant_nothing() -> None:
    logger = RecordingLogger()
    store = _store(ManualClock(), logger)
    token, session = store.issue(OPERATOR, password_set_at=EPOCH)
    store.end(session, reason="logout")
    assert store.resolve(token) is None
    assert logger.named("web_session_ended")[0]["reason"] == "logout"


def test_issuing_over_a_presented_token_retires_it() -> None:
    store = _store(ManualClock())
    old, _ = store.issue(OPERATOR, password_set_at=EPOCH)
    new, _ = store.issue(OPERATOR, password_set_at=EPOCH, replacing=old)
    assert store.resolve(old) is None
    assert store.resolve(new) is not None


def test_a_new_store_honours_no_token_from_an_old_one() -> None:
    """A restart: the process's sessions are its memory, and a new process has none."""
    token, _ = _store(ManualClock()).issue(OPERATOR, password_set_at=EPOCH)
    assert _store(ManualClock()).resolve(token) is None


# --- 5.2 Revalidation -------------------------------------------------------


async def _signed_in(
    accounts: MemoryAccounts, clock: ManualClock, logger: RecordingLogger
) -> tuple[Authenticator, str]:
    auth = authenticator(accounts, clock=clock, logger=logger)
    result = await auth.sign_in(OPERATOR, OPERATOR_PASSWORD, client=CLIENT)
    assert result.token is not None
    return auth, result.token


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ("remove", "removed"),
        ("disable", "disabled"),
        ("password", "password_changed"),
    ],
)
async def test_an_account_change_ends_the_session_at_the_next_revalidation(
    change: str, detail: str
) -> None:
    accounts = MemoryAccounts()
    accounts.add(OPERATOR, OPERATOR_PASSWORD)
    clock = ManualClock()
    logger = RecordingLogger()
    auth, token = await _signed_in(accounts, clock, logger)

    if change == "remove":
        del accounts.accounts[OPERATOR]
    elif change == "disable":
        accounts.update(OPERATOR, enabled=False)
    else:
        accounts.update(OPERATOR, password_set_at=EPOCH + dt.timedelta(days=1))

    assert await auth.resolve(token) is not None, "not yet due: within the minute"
    clock.advance(REVALIDATE_SECONDS)
    assert await auth.resolve(token) is None
    assert await auth.resolve(token) is None
    ended = logger.named("web_session_ended")
    assert [(event["reason"], event["detail"]) for event in ended] == [
        ("account_changed", detail)
    ]


async def test_revalidation_reads_the_account_at_most_once_a_minute() -> None:
    accounts = MemoryAccounts()
    accounts.add(OPERATOR, OPERATOR_PASSWORD)
    clock = ManualClock()
    auth, token = await _signed_in(accounts, clock, RecordingLogger())
    reads = accounts.reads
    for _ in range(59):
        clock.advance(1)
        assert await auth.resolve(token) is not None
    assert accounts.reads == reads, "no read inside the minute"
    clock.advance(1)
    assert await auth.resolve(token) is not None
    assert accounts.reads == reads + 1
    for _ in range(30):
        assert await auth.resolve(token) is not None
    assert accounts.reads == reads + 1


async def test_a_degraded_store_keeps_the_session_but_marks_it_unverified() -> None:
    accounts = MemoryAccounts()
    accounts.add(OPERATOR, OPERATOR_PASSWORD)
    clock = ManualClock()
    logger = RecordingLogger()
    auth, token = await _signed_in(accounts, clock, logger)
    accounts.degraded = True
    clock.advance(REVALIDATE_SECONDS)
    session = await auth.resolve(token)
    assert session is not None
    assert not session.verified
    assert logger.named("web_session_ended") == []

    refusal = await auth.reauthenticate(session, OPERATOR_PASSWORD, client=CLIENT)
    assert not refusal.ok and refusal.message == UNVERIFIED

    accounts.degraded = False
    session = await auth.resolve(token)
    assert session is not None and session.verified, "retried on the next request"


# --- 5.3 The throttle -------------------------------------------------------


def test_the_delay_is_free_for_five_failures_then_doubles_to_a_ceiling() -> None:
    assert [delay_for(n) for n in range(FREE_FAILURES)] == [0.0] * FREE_FAILURES
    assert delay_for(5) == 1.0
    assert delay_for(6) == 2.0
    assert delay_for(10) == 32.0
    assert delay_for(40) == MAX_DELAY_SECONDS


def test_one_username_is_throttled_across_clients() -> None:
    clock = ManualClock()
    throttle = LoginThrottle(clock=clock)
    for index in range(FREE_FAILURES):
        assert throttle.retry_after(OPERATOR, f"198.51.100.{index}") == 0
        throttle.record_failure(OPERATOR, f"198.51.100.{index}")
    assert throttle.retry_after(OPERATOR, "203.0.113.99") == 1.0
    clock.advance(1.0)
    assert throttle.retry_after(OPERATOR, "203.0.113.99") == 0


def test_one_client_is_throttled_across_usernames() -> None:
    throttle = LoginThrottle(clock=ManualClock())
    for index in range(FREE_FAILURES):
        throttle.record_failure(f"guess-{index}", CLIENT)
    assert throttle.retry_after("another-guess", CLIENT) > 0
    assert throttle.retry_after("another-guess", "203.0.113.1") == 0


def test_a_success_resets_both_counts() -> None:
    throttle = LoginThrottle(clock=ManualClock())
    for _ in range(FREE_FAILURES - 1):
        throttle.record_failure(OPERATOR, CLIENT)
    throttle.record_success(OPERATOR, CLIENT)
    assert throttle.failures(username=OPERATOR) == 0
    assert throttle.failures(client=CLIENT) == 0
    throttle.record_failure(OPERATOR, CLIENT)
    assert throttle.retry_after(OPERATOR, CLIENT) == 0


def test_tracking_stays_bounded_after_ten_thousand_distinct_keys() -> None:
    throttle = LoginThrottle(clock=ManualClock())
    for index in range(10_000):
        throttle.record_failure(f"user-{index}", f"10.{index // 65536}.{index // 256 % 256}.{index % 256}")
    assert throttle.tracked == (THROTTLE_KEYS, THROTTLE_KEYS)


async def test_unknown_usernames_are_throttled_exactly_like_real_ones() -> None:
    clock = ManualClock()
    real = authenticator(clock=clock, logger=RecordingLogger())
    unknown = authenticator(clock=ManualClock(), logger=RecordingLogger())
    for _ in range(FREE_FAILURES):
        await real.sign_in(OPERATOR, "wrong", client=CLIENT)
        await unknown.sign_in("nobody-here", "wrong", client=CLIENT)
    assert real.throttle.retry_after(OPERATOR, "other") == unknown.throttle.retry_after(
        "nobody-here", "other"
    ) == 1.0


# --- 5.4 The sign-in decision -----------------------------------------------


async def test_a_correct_password_for_an_enabled_account_signs_in() -> None:
    logger = RecordingLogger()
    auth = authenticator(logger=logger)
    result = await auth.sign_in("Dev-Operator", OPERATOR_PASSWORD, client=CLIENT)
    assert result.succeeded and result.token is not None
    assert result.username == OPERATOR
    session = await auth.resolve(result.token)
    assert session is not None and session.username == OPERATOR


@pytest.mark.parametrize(
    ("username", "password", "enabled", "reason"),
    [
        ("nobody-here", OPERATOR_PASSWORD, True, "unknown_user"),
        (OPERATOR, OPERATOR_PASSWORD, False, "disabled"),
        (OPERATOR, "a-wrong-password", True, "bad_password"),
        ("two words", OPERATOR_PASSWORD, True, "unknown_user"),
    ],
)
async def test_each_failure_runs_exactly_one_verification(
    username: str, password: str, enabled: bool, reason: str
) -> None:
    accounts = MemoryAccounts()
    accounts.add(OPERATOR, OPERATOR_PASSWORD, enabled=enabled)
    logger = RecordingLogger()
    auth = authenticator(accounts, logger=logger)
    await auth.start()
    hasher = auth.hasher
    assert isinstance(hasher, CountingHasher)
    before = hasher.verifications
    result = await auth.sign_in(username, password, client=CLIENT)
    assert not result.succeeded
    assert result.reason == reason
    assert hasher.verifications == before + 1


async def test_a_throttled_attempt_runs_no_verification() -> None:
    logger = RecordingLogger()
    auth = authenticator(logger=logger)
    for _ in range(FREE_FAILURES):
        await auth.sign_in(OPERATOR, "wrong", client=CLIENT)
    hasher = auth.hasher
    assert isinstance(hasher, CountingHasher)
    before = hasher.verifications
    result = await auth.sign_in(OPERATOR, OPERATOR_PASSWORD, client=CLIENT)
    assert result.reason == "throttled"
    assert not result.succeeded
    assert hasher.verifications == before, "a throttled attempt must not reach Argon2id"


async def test_the_dummy_hash_is_computed_once_through_the_hasher() -> None:
    auth = authenticator(logger=RecordingLogger())
    hasher = auth.hasher
    assert isinstance(hasher, CountingHasher)
    await auth.start()
    await auth.start()
    await auth.sign_in("nobody-here", "x", client=CLIENT)
    assert hasher.hashes == 1


async def test_sign_in_rotates_a_presented_session() -> None:
    auth = authenticator(logger=RecordingLogger())
    first = await auth.sign_in(OPERATOR, OPERATOR_PASSWORD, client=CLIENT)
    assert first.token is not None
    second = await auth.sign_in(
        OPERATOR, OPERATOR_PASSWORD, client=CLIENT, presented=first.token
    )
    assert second.token is not None and second.token != first.token
    assert await auth.resolve(first.token) is None
    assert await auth.resolve(second.token) is not None


async def test_the_panel_has_its_own_hasher_instance() -> None:
    """Design D8: a burst of room logins must not queue an operator's sign-in."""
    one = Authenticator(accounts=as_account_store(MemoryAccounts()))
    two = Authenticator(accounts=as_account_store(MemoryAccounts()))
    assert one.hasher is not two.hasher


# --- 5.5 The events ---------------------------------------------------------


@pytest.mark.parametrize(
    ("username", "password", "enabled", "outcome", "reason"),
    [
        (OPERATOR, OPERATOR_PASSWORD, True, "success", "success"),
        ("nobody-here", "secret-guess-1", True, "refused", "unknown_user"),
        (OPERATOR, OPERATOR_PASSWORD, False, "refused", "disabled"),
        (OPERATOR, "secret-guess-2", True, "refused", "bad_password"),
    ],
)
async def test_every_sign_in_attempt_is_one_event_without_the_password(
    username: str, password: str, enabled: bool, outcome: str, reason: str
) -> None:
    accounts = MemoryAccounts()
    accounts.add(OPERATOR, OPERATOR_PASSWORD, enabled=enabled)
    logger = RecordingLogger()
    auth = authenticator(accounts, logger=logger)
    await auth.sign_in(username, password, client=CLIENT)
    events = logger.named("web_login")
    assert len(events) == 1
    assert events[0]["outcome"] == outcome
    assert events[0]["reason"] == reason
    assert events[0]["username"] == username.casefold()
    for value in events[0].values():
        assert password not in str(value)


async def test_a_throttled_attempt_is_its_own_event() -> None:
    logger = RecordingLogger()
    auth = authenticator(logger=logger)
    for _ in range(FREE_FAILURES):
        await auth.sign_in(OPERATOR, "wrong", client=CLIENT)
    await auth.sign_in(OPERATOR, "secret-while-throttled", client=CLIENT)
    last = logger.named("web_login")[-1]
    assert last["reason"] == "throttled"
    assert all("secret-while-throttled" not in str(value) for value in last.values())


async def test_session_end_events_name_each_reason() -> None:
    clock = ManualClock()
    logger = RecordingLogger()
    store = _store(clock, logger)

    _, logged_out = store.issue(OPERATOR, password_set_at=EPOCH)
    store.end(logged_out, reason="logout")
    idle, _ = store.issue(OPERATOR, password_set_at=EPOCH)
    clock.advance(IDLE_SECONDS)
    store.resolve(idle)

    small = SessionStore(clock=clock, logger=logger, max_sessions=1)
    small.issue(OPERATOR, password_set_at=EPOCH)
    small.issue(OPERATOR, password_set_at=EPOCH)

    long_lived = SessionStore(clock=clock, logger=logger, idle_seconds=LIFETIME_SECONDS * 2)
    lived, _ = long_lived.issue(OPERATOR, password_set_at=EPOCH)
    clock.advance(LIFETIME_SECONDS)
    long_lived.resolve(lived)

    reasons = [event["reason"] for event in logger.named("web_session_ended")]
    assert reasons == ["logout", "idle", "evicted", "lifetime"]
    assert all(event["actor"] == OPERATOR for event in logger.named("web_session_ended"))


# --- `next`, which a sign-in redirects to -----------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("/admin/identities", "/admin/identities"),
        ("/chat?peer=ab", "/chat?peer=ab"),
        (None, "/"),
        ("", "/"),
        ("https://evil.example/", "/"),
        ("//evil.example/", "/"),
        ("/\\evil.example/", "/"),
        ("javascript:alert(1)", "/"),
        ("/ok\r\nSet-Cookie: x", "/"),
    ],
)
def test_next_is_only_ever_a_same_origin_path(given: str | None, expected: str) -> None:
    assert safe_next(given) == expected
