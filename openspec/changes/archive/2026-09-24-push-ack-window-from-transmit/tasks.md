# Tasks

## 1. Ack window from end of transmission (room-history)

- [x] 1.1 Make `_Delivery.deadline` optional and add `transmitted_at`; in `RoomServer.deliver` set both after `await handle` returns `sent`, only if `state.delivery` is still the same object (design D1, D4). Verify: new test in `tests/test_room_sync.py` — a push whose handle resolves after the old window has elapsed is not expired on the next `push_once`, and an ack within window-after-transmit advances the cursor
- [x] 1.2 `_expire_deliveries` skips deliveries with no deadline. Verify: test that a delivery still awaiting its handle is never counted as a failure
- [x] 1.3 Keep the submission (queue) deadline at compose + window (design D2). Verify: `test_a_delivery_is_submitted_at_the_reply_class_with_the_peers_own_window` still passes unchanged, and a dropped/suppressed push starts no window and counts no failure (existing `test_a_suppressed_delivery_advances_nothing` passes)

## 2. Late acknowledgements of earlier attempts (room-history)

- [x] 2.1 Add `prior` attempts to `_MemberState`; on expiry move the checksum there instead of releasing it, skipping one equal to the live delivery's (design D3, D5). Verify: `AckRegistry.outstanding()` includes the expired checksum after expiry
- [x] 2.2 In `_on_delivery_ack`, match live delivery first then `prior` for the member's pending post; on a prior match release and clear any outstanding retry, reset failures and backoff, advance and persist the cursor (design D4). Verify: test — expire attempt 1, submit retry, deliver attempt-1 ack → cursor advances, no `ack_unmatched`, `state.delivery` is None, `failures == 0`
- [x] 2.3 Release and clear `prior` on confirmation, on composing a delivery for a different post, and on `revoke`. Verify: test — after confirmation a second prior-attempt ack is `AckUnowned` and the cursor does not move; after a forced cursor change the old checksums are no longer registered
- [x] 2.4 Test that a backed-off member whose earlier attempt's ack arrives late resumes (failures 0, not backed off). Verify: test passes
- [x] 2.5 Add `ack_after_tx_ms` and `late` to `room_delivery_acknowledged`, and `ack_window_ms` to `room_push_sent` (design D6). Verify: tests assert the fields via the captured logger or events

## 3. Room delivery settings storage

- [x] 3.1 Add nullable `push_ack_window_seconds` and `push_recent_days` to `Room` in `src/sighop/db/models.py` and migration `alembic/versions/0010_room_delivery_settings.py` with a docstring in the style of `0009` (design D7). Verify: `tests/test_room_schema.py` / `tests/test_db_schema.py` cover the columns, upgrade and downgrade
- [x] 3.2 Add both fields and display properties to `RoomRecord`, map them in the row→record conversion, include them in `as_json`, default `None` on create. Verify: `tests/test_room_repositories.py` round-trip test
- [x] 3.3 Add `RoomRepository.set_delivery(...)` mirroring `set_retention`. Verify: repository test sets, reads back, clears

## 4. Using the settings in the push loop

- [x] 4.1 `_push_timeout_ms(route, override_seconds)` returns the override for flooded and direct when set; `deliver` passes `self.room.push_ack_window_seconds` (design D8). Verify: test — room with 30 s window gets a 30 s delivery deadline after transmit for both a flood and a multi-hop direct route; unset keeps firmware values
- [x] 4.2 Add optional `last_heard` lookup to `RoomServer.__init__`; in `push_once` skip a member whose `max(last_activity, last_heard)` is older than `push_recent_days` without touching failures or cursor (design D9). Verify: tests — 10-day-old member with 7-day limit gets no submission; after `_note_activity` or a newer `last_heard` it is pushed on the next round; unset limit pushes regardless
- [x] 4.3 Add `members_not_recent` and both settings to `RoomServer.as_json`, and to the runtime's periodic room status line. Verify: test on `as_json`; `tests/test_runtime_rooms.py` status-line assertion
- [x] 4.4 Pass a `ContactStore`-backed `last_heard` lookup from `Runtime._serve_room`. Verify: `tests/test_runtime_rooms.py` — a member heard only by advert is eligible

## 5. Live application of room settings

- [x] 5.1 Add `RoomServer.apply_record(record)` that replaces `self.room`, logs `room_settings_applied` with changed field names, and leaves an outstanding delivery's deadline alone (design D10). Verify: unit test — a new window applies to the next composed push only
- [x] 5.2 In `Runtime.reconcile_rooms`, call `apply_record` for every served room whose stored record differs. Verify: `tests/test_runtime_rooms.py` — changing retention, guest access and delivery in the store then reconciling changes `server.room`, the login policy and the next push's window, without restart
- [x] 5.3 Call `page.state.reconcile_rooms()` after the access, retention and delivery POSTs in `src/sighop/web/routes/admin.py`. Verify: `tests/test_web_admin.py` — after each POST the served room reflects the new value

## 6. Panel

- [x] 6.1 Add `GET/POST /admin/rooms/{id}/delivery` and `templates/admin/room_delivery.html`: shows both fields, the firmware windows used while blank, and the recency note; blank clears; invalid (non-integer, window outside 5–300, days < 1) re-renders with 400 and the reason and stores nothing (design D11). Verify: `tests/test_web_admin.py` tests for set, clear, each invalid case, unknown room 404, CSRF token required like the retention form
- [x] 6.2 Add a "delivery" column and "delivery…" link to `templates/rooms/index.html`, showing "firmware window · all members" at defaults. Verify: `tests/test_web_rooms.py` asserts column text and link; `tests/test_web_write_parity.py` still passes (update its route inventory if it enumerates room writes)

## 7. Verification

- [x] 7.1 Run `uv run ruff format --check`, `uv run ruff check`, `uv run mypy` and `uv run pytest` with the test database; all pass
- [x] 7.2 `openspec validate push-ack-window-from-transmit --strict` passes
