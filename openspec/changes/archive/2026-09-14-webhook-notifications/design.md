## Context

See proposal.md for motivation. Current state that shapes the approach:

- `ContactStore.observe_advert` returns a `ContactObservation` with `created`, and `_report` hands it,
  with the `RxRecord`, to **one** synchronous listener slot (`set_observation_listener`). The bot host
  holds that slot (`runtime.py`, `self.contacts.set_observation_listener(self.bots.on_observation)`).
  The listener is called after the store is updated, is expected to *offer* work rather than do it,
  and a raise is caught and counted (`listener_failures`).
- `created` is only true for a key the in-memory store has never held. With a database, contacts are
  restored before traffic, so a restart does not re-create them. A contact added from a public key
  alone exists already, so its first advert is `created=False`.
- Stored configuration pattern: `bot`/`room` tables, a repository returning `Outcome[...]`, refusal
  exceptions raised by the repository, `sighop bot …` CLI, `/admin/bots` routes calling the same
  repository methods (`test_web_write_parity.py` holds that). Migrations are hand-written Alembic
  revisions `0001`–`0005` with tested downgrades.
- `db/sealing.py` seals 32-byte seeds with `SecretBox` under `SIGHOP_SECRET_KEY`, with a version byte.
- No HTTP client in runtime dependencies; `httpx2` is dev-only. DESIGN.md §2 pins single asyncio
  process and a deliberately small dependency set (e.g. no `uvicorn[standard]`).
- Replay runs set persistence `writes_enabled=False` (design D13 of milestone 5).

## Goals / Non-Goals

**Goals:**
- A trigger model where adding `new_chatter` later is a new enum member, a new event source, and a
  payload renderer case — no schema migration, no dispatcher change.
- Zero effect on the reception path, bots, and radio when an endpoint is slow, down or hostile.
- One implementation of each rule shared by CLI and panel.

**Non-Goals:**
- Durable outbox / guaranteed delivery (operator chose best-effort).
- Request signing (HMAC headers). The URL's own token is the credential for Discord/n8n/Home
  Assistant; signing can be added later as a nullable sealed column without changing events.
- Arbitrary templating of payloads, per-webhook custom headers, or formats beyond `json`/`discord`.
- `new_chatter`, room-post, DM, or rename triggers.
- Firing for contacts first seen during a database outage that later turn out to be duplicates
  (see Risks).

## Decisions

### D1. Fan-out in the contact store, not a composed listener in the runtime
`ContactStore` keeps a tuple of listeners (`add_observation_listener`; `set_observation_listener`
kept as a replace-all shim or removed with its one caller updated). `_report` iterates, catching and
counting per listener. *Alternative:* a lambda in `runtime.py` calling bots then webhooks — rejected
because a raising bot host would silently starve webhooks, and the isolation rule belongs to the
store that already owns it. The bot host is registered first; order carries no meaning beyond
determinism.

### D2. `src/sighop/webhooks/` package: triggers, events, rendering, dispatcher
- `triggers.py`: `Trigger(StrEnum)` = `NEW_REPEATER`, `NEW_COMPANION`; `trigger_for(observation)`
  maps `created` + `NodeType.REPEATER`/`CHAT` to a trigger, else `None`. This is the one function
  `new_chatter` will not touch — it gets its own source (room/channel layer) calling
  `dispatcher.offer(event)`.
- `events.py`: frozen `WebhookEvent` (event_id uuid4, trigger, occurred_at, node fields, reception
  fields, `test: bool`). Built in the listener from `Contact` + `RxRecord`; position taken from the
  record's decoded advert appdata when present (the contact row does not carry it).
- `render.py`: `render_json(event) -> bytes`, `render_discord(event) -> bytes`. Pure functions, unit
  tested with golden bodies. Discord: `allowed_mentions: {"parse": []}`, markdown escaping of every
  advert-derived string, `username: "sighop"`, embed colour per trigger, `"[test]"` prefix for
  samples. JSON: `schema: 1`, `test` field.
- `dispatcher.py`: `WebhookDispatcher` with `offer(event)` (sync, never raises, bounded
  `collections.deque`/`asyncio.Queue` drop-oldest like `net/bus.py`), a single `run()` task, and
  counters `delivered/failed/dropped/config_read_failures`.
*Alternative:* put it under `bots/` as a driver — rejected: bots are bound to an identity, spend a
radio rate limit, and have observe/active modes; a webhook has none of that.

### D3. Delivery: stdlib `urllib.request` in `asyncio.to_thread`, per-webhook concurrent attempts
Each event fans out to its matching webhooks as separate `asyncio` tasks (bounded by a small
semaphore, e.g. 4) so a webhook in backoff does not delay another. Each attempt runs
`urllib.request.urlopen(Request(..., method="POST"), timeout=10)` in a thread with an opener that
has no `HTTPRedirectHandler` (redirects become failures). Retry policy: up to 4 attempts, delays
2 s, 10 s, 60 s; `429` waits `max(delay, Retry-After)` capped at 300 s; retry on `URLError`,
timeout, `5xx`, `429`; any other non-`2xx` is final. `User-Agent: sighop/<version>`.
*Alternatives:* add `httpx`/`aiohttp` as a runtime dependency — rejected for now: traffic is a handful
of POSTs per day, and a new dependency grows the image and the lock for no measured need; the
thread bridge is the same one `alembic` already uses. Revisit if a trigger with real volume arrives.

### D4. Configuration read at event time, with last-good fallback
The dispatcher holds a `WebhookRepository` and, per event, calls `list_enabled()` (one small indexed
query). On `Failed` it logs `webhook_config_read_failed` and uses the last good list. URLs are opened
(unsealed) at read time; a row that fails to open is skipped and logged once per read with the
sealing error, others proceed. *Alternative:* load once at startup plus a reload signal — rejected:
the CLI runs in another process and the project already solved cross-process freshness by reading
the row (`password_set_at` epoch in milestone 9). Events are rare enough that a query each is free.

### D5. Schema: `webhook` table, migration `0006`
Columns: `id uuid pk`, `name text unique`, `sealed_url bytea`, `url_host text` (scheme+host, stored
in the clear for display so listing needs no secret), `format text` (check `json|discord`),
`triggers text[]` (validated in the repository against `Trigger`, **not** a DB enum, so adding
`new_chatter` is code-only), `max_hops integer null` (check `>= 0`), `enabled bool`,
`created_at`, `last_delivered_at timestamptz null`, `last_failed_at timestamptz null`,
`last_failure text null`. Outcome columns are updated by `record_delivery`/`record_failure`, fire-and-forget
from the dispatcher; a failed update is logged and ignored.
Downgrade drops the table (deletes all webhooks), tested like `0005`.

### D6. Sealing: generalise `sealing.py` with `seal_value`/`open_value`
Same `SecretBox` construction, a distinct version byte (`2` = variable-length value) so a seed can
never be opened as a URL or vice versa, and the existing seed functions unchanged. *Alternative:*
store plain text and redact on display — rejected: DESIGN.md §6's stance is that a dump must not
hand out credentials, and a Discord URL is a posting credential.

### D7. Validation lives in the repository layer, shared by CLI and panel
`webhooks/config.py` (or repository helpers): `parse_url` (scheme `http|https`, host present, no
userinfo echoed), `parse_triggers` (non-empty, known, deduplicated, error lists known names),
`parse_format`, `parse_max_hops`. Refusals are `WebhookConfigError` subclasses with operator-facing
messages; `WebhookExistsError` for the name. Both surfaces call `WebhookRepository.create/update/…`
so parity tests compare stored rows.

### D8. `test` sends synchronously from the caller's process, once
`sighop webhook test NAME --trigger new_repeater` and `POST /admin/webhooks/{id}/test` build a
sample event (fixed fake key `00…`, name `dev-sample`, `test: true`), render it, and perform a single
attempt with the same transport function, returning status or reason. Works on disabled webhooks
and ignores `max_hops`. The panel's POST is CSRF-protected like other admin writes; it is not a
guarded (nonce) action because it neither transmits on air nor reveals a secret.

### D9. Runtime wiring
In `Runtime.__post_init__`, after the bot host listener: if persistence is configured **and** the
run is not a replay, construct `WebhookDispatcher(repository=…, secret=…, logger=…)`, register
`dispatcher.on_observation` with the store, and start `dispatcher.run()` with the other service
tasks; cancel on shutdown (pending events are dropped, counted in the final line). Startup line:
`webhooks: 2 enabled (new_repeater, new_companion)` / `webhooks: none — no database` /
`webhooks: none — replay`. Status line segment `wh ok=N fail=N drop=N` only when a dispatcher exists.
The web panel's state exposes the dispatcher counters for the webhooks page.

### D10. Log events
`webhook_event_raised` (trigger, node hash, event_id), `webhook_delivered` (webhook name, host,
status, attempts, duration_ms), `webhook_attempt_failed` (…, status or error, next delay),
`webhook_abandoned`, `webhook_dropped`, `webhook_config_read_failed`, `webhook_url_unsealable`,
`webhook_test_sent`. Only `url_host` ever appears; never the path or query.

## Risks / Trade-offs

- [First run with an empty contact table announces every node on the mesh within a day] → Documented
  in DESIGN.md and CLI `add` output; `max_hops` filter available; Discord's own rate limit is handled
  through `429`/`Retry-After`. A seeding step like the greeter's is unnecessary because `created`
  is already false for every restored contact.
- [Contact write lost during a DB outage, then restart → contact re-created → duplicate event] →
  Accepted; same window as milestone 5's backfill, rare, and a duplicate notification is harmless.
- [Blocking thread pool shared with other `to_thread` users] → semaphore caps concurrent attempts;
  10 s timeout bounds each thread.
- [SSRF: an operator-set URL can target internal services] → Only signed-in operators configure
  webhooks, who already control the host; redirects are not followed; scheme restricted. Documented,
  not otherwise mitigated.
- [Advert names are attacker-controlled] → Discord escaping + mentions disabled; JSON is data by
  construction.
- [Events lost on restart/outage] → Operator's explicit choice; counters and last-failure columns make
  loss visible.
- [`SIGHOP_SECRET_KEY` rotated or wrong] → affected webhooks logged unsealable and skipped; replace
  URL with `set-url`.

## Migration Plan

1. Deploy code with migration `0006`; operator runs `sighop db upgrade` (startup refuses a
   mismatched schema as today).
2. No existing data changes. Rollback: `sighop db downgrade 0005` deletes all webhooks; previous
   image then starts normally.
