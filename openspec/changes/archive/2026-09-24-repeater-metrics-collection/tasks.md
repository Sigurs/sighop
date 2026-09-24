# Tasks

## 1. Codecs (`protocol/payloads.py`)

- [x] 1.1 Add `RepeaterStatusBody` + `parse_repeater_status_body` (52- and 60-byte bodies, signed noise floor/RSSI/SNR, SNR÷4, optional rx airtime/rx errors, truncation failures); verify with new `tests/protocol/test_repeater_payloads.py` covering every `payload-codec` status scenario, and that `ServerStats` tests still pass
- [x] 1.2 Add `RequestType.GET_NEIGHBOURS = 0x06`, `NeighboursRequest` builder and `parse_neighbours_response` (total, returned, entries with prefix/seconds/SNR; lying count fails); verify building/parsing/lying-count scenarios in the same test file
- [x] 1.3 Add `RepeaterLoginBody` builder (timestamp + password, no sync timestamp) and a client-side login-answer parse accepting 12 or 13 bytes (protocol level absent at 12); verify blank-password and 12-byte scenarios, and existing room login tests unchanged

## 2. Schema and repositories

- [x] 2.1 Add models `RepeaterCollectionRow`, `RepeaterTargetRow`, `RepeaterPollRow`, `RepeaterNeighbourRow` in `db/models.py` per design D8 (single-row check, range checks, SET NULL entity FK, cascade neighbours, indexes)
- [x] 2.2 Write `alembic/versions/0011_repeater_metrics.py` (down_revision `0010`) with upgrade/downgrade and docstring in the style of 0009/0010; verify `tests/test_db_schema.py` model/migration parity and upgrade→downgrade→upgrade round trip
- [x] 2.3 Add `RepeaterCollectionRepository` (get-with-defaults, save validated settings, record cycle summary), `RepeaterTargetRepository` (select, deselect, list keys), `RepeaterPollRepository` (record poll with neighbours, latest per keys, latest-with-status, history capped, prune older than); verify with new `tests/test_repeater_repositories.py` incl. fresh-DB defaults, identity delete → `entity_id` NULL, cascade prune

## 3. Path body forwarding

- [x] 3.1 Add optional `on_bundled_response(entity, contact, payload, record)` to `PathBodyReader`, called only when the bundled extra type is `RESPONSE`, after route adoption; verify in `tests/test_path_bodies.py` that a bundled RESPONSE reaches the callback with the route learned, and bundled ACK behaviour is unchanged

## 4. Collector (`net/collect.py`)

- [x] 4.1 Define settings/target/poll-sink Protocols and an in-memory fixture (`tests/collectfixtures.py`) so collector tests need no Postgres, following `roomfixtures.py`
- [x] 4.2 Implement eligibility: repeater type, selected, `last_heard` within `recent_days`; verify heard-yesterday / silent-a-week / unselected / non-repeater scenarios in `tests/test_collect.py`
- [x] 4.3 Implement the poll exchange: blank-password `ANON_REQ` login → `GET_STATUS` → paged `GET_NEIGHBOURS` (prefix 6, count 11, ≤8 pages), per-repeater strictly increasing timestamps (D3), route choice using public-key routes only (D5), per-step timeout, `ADVERT` priority with deadline; verify with a scripted fake repeater: complete poll, 25 neighbours in 3 pages, login unanswered, status unanswered, neighbours incomplete, known-route-goes-direct
- [x] 4.4 Implement response matching (D2): direct `RESPONSE` by dest/src hash + single secret + echoed tag; login by MAC + parse; bundled via 3.1 callback; unmatched counted and dropped; verify late-answer-to-earlier-poll and flood-answered-by-path-return scenarios
- [x] 4.5 Implement the loop (D7): 60 s tick, per-tick settings read with last-good fallback, interval due check, no overlapping cycles, disable mid-cycle stops before next repeater, transmit-gate check before cycle and each repeater (no record when disabled), dropped/suppressed submission → `not_sent` with reason, identity missing/room-claimed → skip with logged reason, cycle summary recorded; verify default/changed interval, long cycle, disabled mid-cycle, transmit disabled, suppressed-by-ceiling scenarios with a fake clock
- [x] 4.6 Implement retention pruning at cycle end and at least daily, also while disabled; verify old polls and neighbours deleted and newer kept

- [x] 4.7 After an answer heard by flood, wait for re-floods to subside and send the repeater a direct path return with the route the flood took (D12); verify with the fake repeater routing as firmware does: flooded answer → path return → direct answers, settle gap ≥ 3 s, no path return once the repeater holds a route

## 5. Runtime wiring

- [x] 5.1 Construct the collector in `runtime.py` only for live runs with a database, subscribe it to the bus, pass the path-body callback, start its task, stop it on shutdown; verify in `tests/test_runtime_collect.py` that a replay run with enabled settings submits nothing and records nothing, and a live run starts the task

## 6. Web

- [x] 6.1 System page: collection section in `system.html` + `POST /system/collection` with strict validation (ranges 5–1440 / 1–365 / 1–365, junk refused with 400 + reason, enable without identity refused, room identity refused), selected/in-window counts, last-cycle summary, transmit-disabled notice, blank-guest-password statement; verify in `tests/test_web_admin.py` for every `web-admin` scenario and that the existing system page tests still pass
- [x] 6.2 Contacts page: checkbox column for repeater rows only, `POST /contacts/{key}/collect` (htmx, CSRF header) returning the re-rendered cell, refusal for non-repeaters, last-poll outcome + metrics link, "skipped — not heard within N days" marker; verify in `tests/test_web_dashboard.py` (tick persists across reload, companion has no checkbox, non-repeater POST refused)
- [x] 6.3 Metrics page `/contacts/{key}/metrics` (template `metrics.html`): latest status with time, absent fields shown as "not reported", neighbours with exact-one-prefix attribution / ambiguous marker, history newest first capped at 200, never-polled state with next-due time; verify all `web-dashboard` metrics-page scenarios
- [x] 6.4 Add new write routes to the write-parity/guard coverage (`tests/test_web_write_parity.py`) so CSRF and sign-in are enforced on both POSTs

## 7. Docs and verification

- [x] 7.1 Update `DESIGN.md` (§6 persistence tables, §7 new "Repeater collection" entity behaviour with firmware references, §8 WebUI pages) and verify the text names migration `0011`
- [x] 7.2 Run lint and type checks (`ruff check`, `mypy`) and the full test suite; all pass
- [ ] 7.3 Live exercise against a stock repeater with blank guest password: enable collection, tick the repeater, confirm a `succeeded` poll with status and neighbours on its metrics page, and a `login_unanswered` poll after setting a guest password on it; record findings in `DESIGN.md` §12
