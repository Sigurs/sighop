# Design

## Context

See proposal.md for motivation. What shapes the approach:

- **sighop has no client side.** Every existing exchange has sighop answering: the room server
  (`net/room.py`) parses `ANON_REQ` logins and `REQ` bodies and *builds* `RESPONSE`s; nothing
  decrypts an inbound `RESPONSE`. `DirectMessenger` owns `TXT_MSG`/`ACK`, `PathBodyReader` owns
  `PATH` and hands only a bundled `ACK` onward (to `AckRegistry`); any other bundled payload is
  reported by type and dropped.
- **Repeater firmware ground truth** (`related-repos/MeshCore/examples/simple_repeater/MyMesh.cpp`):
  - Login (`handleLoginReq`, :90-143): body is `timestamp(4) + password\0`. A blank password
    first looks the sender up in the ACL; failing that it is compared with `guest_password`, which
    is empty by default, so blank admits as `PERM_ACL_GUEST`. Failure is **silence**. The timestamp
    must exceed the client's `last_timestamp` (replay guard). The answer is 13 bytes whose first 4
    are the *server's* clock — not an echo of ours — so a login answer cannot be matched by tag.
  - `onPeerDataRecv` (:655-700): a `REQ` is honoured only if its timestamp exceeds the last one
    seen from that client, and the answer's first 4 bytes **echo the request timestamp** (:214).
    Flooded requests are answered by a path return with the `RESPONSE` bundled; direct ones by a
    `RESPONSE` datagram (direct if the repeater has an out-path, else flood).
  - `GET_STATUS` (:216, "guests can also access this now") returns `RepeaterStats`, 56 bytes
    (`MyMesh.h:44-60`). `GET_NEIGHBOURS` (:276) has no admin check; entries are
    `prefix + u32 heard_seconds_ago + i8 snr×4` into a 130-byte buffer, preceded by `u16 total,
    u16 returned`.
- The existing `ServerStats` codec is the **room** layout (52 bytes, last 4 bytes are posted/push
  counters); a repeater's struct diverges exactly there and adds 4 more bytes.
- There is no station-settings table; the system page is read-only. Webhooks established the
  pattern of reading configuration from the database per use with a last-good fallback.
- Migration head in the working tree is `0010` (room delivery settings, uncommitted on this
  branch); this change takes `0011`.

## Goals / Non-Goals

**Goals:**
- One collector component that owns the whole client exchange end to end: scheduling, login,
  requests, response matching, recording.
- No change in how any existing component decrypts or answers traffic, except `PathBodyReader`
  forwarding a bundled `RESPONSE` it would otherwise drop.
- Settings and selection edits apply without restart.

**Non-Goals:**
- Any password other than blank; admin login; remote CLI; telemetry (`GET_TELEMETRY_DATA`) or ACL
  requests; `ANON_REQ` regions/owner/clock requests.
- Charts, rates or alerting on collected values; webhooks for metrics.
- Being a room-server *client* (keep-alive, pushes) — still out of scope.
- Keeping a login session alive between cycles (see D4).

## Decisions

### D1. A new bus subscriber, `RepeaterCollector` in `net/collect.py`
It runs its own loop task (like `webhooks.run()`) and subscribes to the bus for `RESPONSE`
envelopes. It is the only component that decrypts `RESPONSE`, so no ownership split is needed the
way room servers claim `TXT_MSG`/`PATH` (milestone 6 D10). *Alternative:* extending
`DirectMessenger` — rejected, its state (conversations, ACK expectations, DM records) has nothing
in common with a request/response exchange and it would grow a second responsibility.

### D2. One outstanding exchange at a time; match by (repeater key, step, tag)
The collector polls repeaters sequentially, so at most one request is outstanding. Held as a
single `_Outstanding(contact, step, tag, future)`:
- **Inbound direct `RESPONSE`**: accepted only if `dest_hash` equals the login identity's hash and
  `src_hash` equals the outstanding repeater's hash; decrypt with exactly that one shared secret
  (no candidate trial — we know who we asked). For `STATUS`/`NEIGHBOURS`, the first 4 plaintext
  bytes must equal `tag`. For `LOGIN` there is no tag (firmware writes its clock), so acceptance is
  MAC match + body parses as a 12/13-byte login answer with result `0`.
- **Bundled in a `PATH`**: `PathBodyReader` gains an optional `on_bundled_response(entity,
  contact, payload, record)` callback, called when the decrypted path body's extra type is
  `RESPONSE`. The reader still adopts the route (so the next request goes direct). The collector
  applies the same matching.
- Anything else: counter `responses_unmatched` + debug log, discarded.
*Alternative:* a registry like `AckRegistry` keyed by tag — rejected as unnecessary with one
outstanding exchange, and useless for login which has no tag.

### D3. Per-repeater strictly increasing timestamps
Firmware drops a login or request whose timestamp is not greater than the last it saw from us.
The collector keeps `last_sent[public_key]` in memory and uses `max(now_epoch, last_sent + 1)`.
Across restarts wall-clock time has moved on (cycles are ≥5 minutes apart), so persisting it is
not needed. Login, status and each neighbour page each take a fresh value.

### D4. Log in every poll; no session reuse
Firmware keeps guest entries only in RAM, the ACL holds 32 clients, and entries are evicted; a
reused session would fail silently on the first request after a repeater reboot or eviction, and
detecting that costs a timeout anyway. One extra small packet per repeater per hour is cheaper
than that complexity. *Alternative:* try request first, log in on timeout — rejected for the
reason above plus worse worst-case airtime.

### D5. Routing
`dm.choose_route(paths, contact, path_hash_size=…, allow_flood=True)` but only a route keyed by the
repeater's **public key** is used directly; an ambiguous node-hash route is ignored in favour of a
flood, because a direct packet down the wrong repeater's path is simply lost. A flooded login is
answered by a path return, `PathBodyReader` adopts the route, and the following status request
goes direct. Timeouts reuse `dm.ack_timeout_ms(airtime, route)` plus the firmware's
`SERVER_RESPONSE_DELAY` (300 ms) and the reply's own airtime — computed once per step.

### D6. Priority and gate
Submissions use `PriorityClass.ADVERT` (lowest, "always yields"). Before each cycle and before
each repeater the collector checks `scheduler.transmit_enabled`; disabled means no submission and
no record (spec: *yields… respects the transmit gate*). A submission resolved `dropped`/`failed`/
`suppressed` ends that poll with outcome `not_sent` and the scheduler's reason. Submission
`deadline` = now + step timeout, as `DirectMessenger` does, so a stale request is dropped rather
than sent into an expired window.

### D7. Settings read from the database each tick, last-good fallback
The loop wakes every 60 s, reads the settings row and decides whether a cycle is due
(`last_cycle_started + interval <= now`). A DB read failure keeps the last good settings and logs.
This gives the "applies without restart" property with no reconcile call from the web routes, the
same way webhook configuration works. The selection set and contacts' `last_heard` are read at
cycle start. The login identity is resolved against the loaded entities (`adverts.stubs`) at cycle
start; missing or room-claimed → cycle skipped with a logged reason, surfaced on the system page.

### D8. Schema (migration `0011`)
- `repeater_collection` — single row (`id = 1` with a check constraint): `enabled bool`,
  `entity_id uuid NULL → entity ON DELETE SET NULL`, `interval_minutes int`, `recent_days int`,
  `retention_days int`, `last_cycle_started_at`, `last_cycle_polled int`, `last_cycle_succeeded
  int`. Absent row = defaults (spec: fresh database). Check constraints mirror the form ranges.
- `repeater_target` — `public_key bytea PK`, `selected_at`. Separate from `contact` so contact
  upserts from adverts can never clear a selection. No FK to `contact` (contacts may be written
  lazily/backfilled); an orphan selection is harmless and shown nowhere.
- `repeater_poll` — `id`, `public_key`, `entity_id NULL (SET NULL)`, `started_at`, `outcome`
  (text enum: `succeeded`, `not_sent`, `login_unanswered`, `status_unanswered`,
  `neighbours_incomplete`), `reason text NULL`, `route text`, the 18 status columns (nullable;
  the two newest nullable for older firmware), `neighbours_total int NULL`. Index
  `(public_key, started_at desc)` and `(started_at)` for pruning.
- `repeater_neighbour` — `poll_id → repeater_poll ON DELETE CASCADE`, `prefix bytea`,
  `heard_seconds_ago int`, `snr_db real`. Pruning deletes polls; cascade removes neighbours.

The `ON DELETE SET NULL` on `entity_id` is the "identity removed → collection stops" rule, in the
database, as migration 0009 did for default chat identity.

### D9. Neighbour paging
Request prefix length 6 (entry = 11 bytes → 11 per 130-byte buffer), order newest-first, count 11,
offset advancing by entries received. Stop at total, empty page, or 8 pages (88 neighbours, above
any realistic `MAX_NEIGHBOURS`). A missing page after the first → `neighbours_incomplete` with
entries so far.

### D10. Web
- **System page** gains a "repeater collection" section with a POST form (`/system/collection`),
  validated strictly like the room delivery form (400 + reason, nothing stored — the recorded
  decision against `_optional_int`). Identity choices: stored identities minus room-serving ones.
  Shows selected count, in-window count, last cycle summary, and the transmit-disabled warning.
- **Contacts page** (`web/app.py` route): repeater rows get a checkbox posting via htmx
  (`hx-post="/contacts/{key}/collect"`, CSRF header already on `<body>`) that returns the
  re-rendered cell; plus last-poll outcome and a link. The page then reads the DB (selection and
  latest poll per selected key, one query each) — acceptable now that the web requires a database.
- **Metrics page** `/contacts/{key}/metrics`: latest status (from the latest poll that has one),
  latest neighbour list, history (polls in retention, newest first, capped at 200 rows). Neighbour
  attribution via `ContactStore` prefix match: exactly one → name, more → "ambiguous".

### D11. Retention
The collector prunes at the end of each cycle and at least daily (the 60 s tick checks
`last_pruned`), deleting `repeater_poll` rows older than `retention_days`. Pruning runs even when
collection is disabled, so disabling does not freeze old data forever.

### D12. After a flooded answer: settle, then return our route (added after the live exercise)
The first live run polled six repeaters for 16 hours: every one of 110 answers arrived by
flood and 71 of 181 requests went unanswered, usually the request sent ~1 s after a flooded
answer. A repeater answers a direct request by flood when it holds no route to the client
(`MyMesh.cpp:692-696`), and learns one only from a `PATH` the client sends
(`onPeerPathRecv`, `:766-783`); stock clients send it after every flooded response
(`BaseChatMesh::handleReturnPathRetry`, 3 s delay). sighop sent none, so every answer
flooded and the next request was transmitted into the re-floods. Now, after an answer heard by
flood, the collector waits `max(3 s, 2 × (2.6 + 1) × airtime)` — two waves of
`getRetransmitDelay`'s up-to-five half-airtimes plus the resend — then sends a direct path
return (`0xFF` + 4 random bytes as the extra, as `createPathReturn` does) along its own route.
The repeater's guest entry keeps the route, so later polls are answered direct. *Not done*
(considered and declined for now): a retry per step, and flooding the login after a failed
poll to reset a stale stored route.

## Risks / Trade-offs

- **Firmware variance in `RepeaterStats`** → parse by length: 48 bytes of struct minimum, 56 full;
  anything else is a decode failure recorded as `status_unanswered` with reason `unparsable`.
- **A repeater with a guest password looks the same as one out of range** (both silence) →
  outcome is named `login_unanswered`, not "refused", and the metrics page explains both causes.
- **Flood logins cost network-wide airtime** → only until a route is learned (first path return);
  lowest priority; recency filter keeps absent repeaters out; interval floor of 5 minutes.
- **Occupying guest slots** on busy repeaters (32 entries) → one slot per station, re-used by key
  on each login (`putClient` finds the existing entry).
- **Sequential polling of many repeaters can exceed a short interval** → cycles never overlap
  (spec); the system page shows last-cycle duration so the operator can see it.
- **Shared-secret derivation per poll** → uses the existing `SharedSecretCache`.
- **`PathBodyReader` change touches the room push path** → the new callback is invoked only when
  the extra type is `RESPONSE`; the `ACK` path is unchanged and covered by existing tests.

## Migration Plan

1. `alembic upgrade` to `0011` (applied at start as usual). New tables only; no data migration.
   Collection is disabled until an operator enables it.
2. Rollback: `downgrade` drops the four tables, losing settings, selection and history; nothing
   else references them.

## Open Questions

- Exact wording and placement of the metrics-page link from the contacts table (column vs. name
  link) — cosmetic, settle during implementation.
