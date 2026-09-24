# Proposal

## Why

Operators can see which repeaters sighop hears, but not how those repeaters are doing: battery,
noise floor, airtime, queue depth, error counters, and which other repeaters each one hears
zero-hop. Stock MeshCore repeaters answer `GET_STATUS` and `GET_NEIGHBOURS` to any client logged
in as a guest, and a repeater whose guest password is unset (the firmware default) admits a
blank-password login. Collecting this periodically turns sighop into a low-effort health monitor
for the repeaters around it, without the operator holding any repeater's admin password.

sighop today is only ever a *server* (room server, direct messages): it has never logged in to
another node, sent a `REQ`, or read a `RESPONSE`. This change adds that client side, scoped to
exactly what collection needs.

## What Changes

- **Repeater collection, configured from the system page.** A new section holds: enabled (off by
  default), the local identity that logs in (must be chosen before enabling), the interval
  (default 60 minutes), the recency window (only repeaters whose advert was last heard within
  *X* days, default 3), and how long samples are kept (default 30 days).
- **Per-repeater opt-in on the contacts page.** Repeater contacts get a "Collect metrics"
  checkbox. Nothing is polled that was not ticked, and non-repeater contacts have no checkbox.
- **A client login with a blank guest password.** Each cycle, each selected repeater that passes
  the recency filter is sent an `ANON_REQ` login with an empty password, then a `GET_STATUS`
  request, then `GET_NEIGHBOURS` requests paged until the list is complete. Repeaters are polled
  one at a time, at the lowest transmit priority, only while transmission is enabled. There is no
  password configuration: a repeater with a guest password set refuses (silently, as firmware
  does) and is recorded as such.
- **Durable history.** Every poll is recorded with its outcome (ok, login unanswered, status
  unanswered, …), the repeater's status fields, and its neighbour list. Samples older than the
  retention window are pruned.
- **Where it is shown.** The contacts page shows, per collected repeater, when it was last polled
  and whether that succeeded, linking to a per-repeater metrics page with the latest status, its
  neighbours (resolved to known contacts by key prefix where possible), and the poll history.
- **New codecs** for the repeater's 56-byte `RepeaterStats` (distinct from the room server's
  52-byte `ServerStats` sighop already builds), the `GET_NEIGHBOURS` request and response, and the
  repeater login body (no sync timestamp, unlike a room login). The room server's login response
  layout is reused to read the repeater's answer.
- **Migration `0011`**: collection settings, selected repeaters, poll records and neighbour rows.

## Capabilities

### New Capabilities
- `repeater-metrics`: periodic guest-login collection of status and neighbour data from selected
  repeaters — selection, recency filter, schedule, the login/request exchange, how responses are
  matched, what is stored, retention, and how failures are recorded.

### Modified Capabilities
- `web-admin`: the system page gains the repeater collection settings form (it is no longer
  read-only), with the same validation-before-write rule other configuration writes follow.
- `web-dashboard`: the contact table gains the "Collect metrics" checkbox and last-poll status for
  repeater contacts, and a per-repeater metrics page is added.
- `payload-codec`: the repeater status body, the `GET_NEIGHBOURS` request and response, and the
  repeater login body and login answer as a client reads them.

## Impact

- **Code**: new `src/sighop/net/collect.py` (collector: scheduler loop, login/request exchange,
  response matching); `protocol/payloads.py` (new codecs); `net/pathbodies.py` (hand bundled
  `RESPONSE` payloads to the collector instead of discarding them); `runtime.py` (wire the
  collector as a bus subscriber and task); `db/models.py`, `db/repositories.py`, new
  `alembic/versions/0011_repeater_metrics.py`; `web/app.py` (contacts page), `web/routes/system.py`
  (settings form), new metrics route and templates.
- **Airtime**: bounded — per cycle, per repeater, one login plus one status plus ⌈neighbours/11⌉
  requests and their replies, at the `ADVERT` (lowest) priority class so it always yields.
- **Repeaters**: each successful login occupies one guest slot in the repeater's in-memory client
  table (32 by default); firmware does not persist guest entries.
- **Requires the database** (settings and selection live there), as the web interface already does.
- No new dependencies.
