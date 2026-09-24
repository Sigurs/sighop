# Design

## Context

See proposal.md — Why. Current shape of a push (`RoomServer.deliver`, `src/sighop/net/room.py`):

1. Compose body with a random `attempt`, compute `expected` ack.
2. `now = clock.now()`; `_Delivery(deadline=now + _push_timeout_ms(route))`; register `expected`.
3. `submit(...)` with the **same** deadline as the queue deadline, then `await handle`. The handle
   resolves only when the scheduler's `_dispatch` has the modem's `TX_DONE`, so the await covers
   queue wait, LBT/duty-cycle holds, and the push's own airtime.
4. Next `push_once` (1.2 s later) calls `_expire_deliveries`, which releases `expected`, bumps
   `failures`, and lets the round-robin compose a retry with a fresh random `attempt` — a different
   checksum. An ack for the old attempt is now `ack_unmatched` in `AckRegistry.deliver`.

Other constraints found:
- `AckRegistry` expectations are keyed by checksum and released by their owner (DM sends already
  hold every attempt's checksum until the exchange ends — `acks.py` docstring).
- Push state is in-memory per process (`_MemberState`), not persisted.
- `RoomServer.room` is the `RoomRecord` read at `_serve_room` time. `reconcile_rooms` only adopts
  and stops rooms; it never refreshes a served one. Access (`policy`) and retention edits made in
  the panel therefore do not reach the running process until restart.
- Room settings pages follow one pattern: `GET/POST /admin/rooms/{id}/<setting>`, a template in
  `templates/admin/`, a `RoomRepository.set_*` method, redirect to `/rooms`.
- `MemberRecord.last_activity` is updated in memory by `_note_activity` on every request from the
  member. `Contact.last_heard` (`ContactStore`, on the runtime) moves on adverts.

## Goals / Non-Goals

**Goals:**
- Ack window counted from end of transmission; any attempt of the pending post can confirm it.
- Two per-room delivery settings, editable in the panel, applied live.
- Served rooms pick up every stored room setting without a restart.
- Evidence in logs to confirm the diagnosis on a live mesh.

**Non-Goals:**
- Changing the firmware default windows, `SYNC_PUSH_INTERVAL`, or `MAX_PUSH_FAILURES`.
- Deterministic `attempt` values (random draw is deliberate so repeaters do not dedup retries —
  `_push_body` docstring).
- A CLI for the new settings (retention has none either).
- Unblocking the push loop while one transmission is in flight.
- Suppressing a member's duplicate reception of a retry already on air when a late ack arrives —
  the client dedups by timestamp; its second ack is harmless.

## Decisions

**D1 — Deadline set on transmit completion.** `_Delivery.deadline` becomes optional: `None` while
the push is queued/on air; set to `transmitted_at + window` after `await handle` returns `sent`,
with `transmitted_at = clock.now()` taken immediately after (the scheduler resolves at `TX_DONE`).
`_expire_deliveries` skips deliveries with no deadline.
- Alt: add `queue_wait_ms + airtime_ms` from `TxOutcome` to the old deadline. Rejected: same
  result, more arithmetic, still wrong if the push loop resumes late.

**D2 — Queue deadline kept separate.** Submission `deadline` stays `compose + window`, so a push
that cannot get on air in time is dropped by the scheduler rather than sent stale. A dropped push
is not `sent`, so no window starts and no failure is counted (existing `not outcome.sent` branch).

**D3 — Earlier attempts' checksums kept per member for the current post.** `_MemberState` gains
`prior: dict[bytes, _Attempt]` (checksum → post_timestamp, transmitted_at). On expiry the checksum
moves there instead of being released. `_on_delivery_ack` matches the live delivery first, then
`prior`, requiring the prior's post to still be the member's pending one (`> sync_since`). On
confirmation, or when a delivery is composed for a different post (cursor forced by login /
keep-alive, retention, revoke), `prior` is released from the registry and cleared. `attempt` ∈
0..3, so at most four checksums per member — bounded without a timer.
- Alt: timed grace per checksum. Rejected: needs its own sweep and still loses a very late ack.
- Alt: "recent acks" ring in `AckRegistry`. Rejected: the registry is owner-agnostic; the
  member/post binding lives in the room.

**D4 — Late match ends the outstanding retry.** A prior match while `state.delivery` is a retry of
the same post releases the retry's checksum and clears `state.delivery`, then runs the normal
confirmation path (cursor advance, `failures = 0`, `backed_off = False`, persist). `deliver` must
tolerate the delivery being cleared under its `await handle`: only set the deadline if
`state.delivery` is still the same object.

**D5 — Repeated checksums.** A retry can draw the same `attempt` as an earlier one and so the same
checksum. Registering again is idempotent for the same owner; the live delivery wins the match.
When moving to or releasing `prior`, skip a checksum equal to the live delivery's.

**D6 — Reporting.** `room_delivery_acknowledged` gains `ack_after_tx_ms` (match `received_at`,
falling back to now, minus the matched attempt's `transmitted_at`) and `late: bool`.
`room_push_sent` already carries `queue_wait_ms`/`airtime_ms`; add `ack_window_ms`.
`RoomServer.as_json` gains `members_not_recent` and the two settings.

**D7 — Storage: two nullable integer columns on `room`.** `push_ack_window_seconds` and
`push_recent_days`, NULL = default, via migration `0010` (no data migration; downgrade drops them
and every room returns to defaults). `RoomRecord` gains both fields plus display properties
(`push_ack_window` → "firmware" / "30 s", `push_recency` → "all members" / "heard ≤ 7 d").
`RoomRepository.set_delivery(room_id, *, push_ack_window_seconds, push_recent_days)` mirrors
`set_retention`.
- Alt: one JSON "delivery" column. Rejected: every other room setting is a typed column.

**D8 — Window semantics: override, not floor.** A set value replaces the formula for flooded and
direct pushes alike (`_push_timeout_ms(route, override)`). Range 5–300 s keeps a typo from
re-creating the bug (too short) or stalling a member for many minutes per attempt (too long).
- Alt: floor (`max(formula, setting)`). Rejected: harder to explain in the UI, and the operator
  asked for a number they control.
- Alt: a multiplier. Rejected: operators think in seconds.

**D9 — "Heard" = latest of room activity and advert.** `RoomServer` takes an optional
`last_heard: Callable[[bytes], dt.datetime | None]` (runtime passes a lookup on `ContactStore`).
Eligibility in `push_once`: `max(member.last_activity, last_heard(pk)) >= now - days`. Checked per
round, so a fresh advert or request makes the member eligible on the next round with no extra
event wiring. A skipped member is not a failure and not "backed off"; `members_not_recent` is
computed on demand for status.
- Alt: `last_activity` only. Rejected: a member in range that simply hasn't opened the room
  should still get pushes — an advert proves reachability.
- Alt: skip filter at the `next_for_member` query. Rejected: recency is in memory, not in SQL.

**D10 — Served rooms refresh their record.** `reconcile_rooms` replaces `server.room` with the
stored `RoomRecord` for every served room whose record differs (`RoomServer.apply_record`, which
also logs `room_settings_applied` with changed fields). Access, retention and delivery all come
from `self.room`, so all three become live. The panel's delivery, access and retention POSTs call
`page.state.reconcile_rooms()` after writing, as room creation already does; other processes' edits
arrive on the periodic re-read. An outstanding delivery keeps its deadline (spec: applies to pushes
composed after the change).

**D11 — Panel.** New `GET/POST /admin/rooms/{id}/delivery` + `admin/room_delivery.html`, following
the retention page. Unlike `_optional_int` (which silently turns junk into "unlimited"), the new
fields are validated: invalid input re-renders the form with a 400 and the reason; nothing stored.
Rooms list gains a "delivery" column and a "delivery…" link.

## Risks / Trade-offs

- [Push loop stalled after `await handle` delays `transmitted_at`, lengthening the window] →
  bounded by one loop iteration; over-waiting is the safe direction.
- [Late ack advances the cursor while a retry is on air → member receives the post twice] →
  client dedups by timestamp; its ack for the duplicate is `ack_unmatched`, harmless.
- [Refreshing `server.room` changes live behaviour of access/retention edits, previously deferred to
  restart] → this is what the specs already promise; logged as `room_settings_applied`.
- [Long window × 3 attempts stalls one member's queue] → per-member only; round-robin serves
  others. Upper bound 300 s.
- [Recency limit hides members that are reachable but silent (no adverts, no activity)] → opt-in,
  default off; skipped count is shown in status.
- [Holding extra checksums could mask another component's ack] → bounded to four per member,
  collision odds as for any expectation.

## Migration Plan

Alembic `0010` adds two nullable columns; existing rooms keep current behaviour. Deploy by
`alembic upgrade` + restart. Rollback: revert commit, `alembic downgrade 0009` (drops the settings).
