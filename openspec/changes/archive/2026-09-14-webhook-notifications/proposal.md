## Why

A new repeater or companion appearing on the mesh is something operators want to hear about when
they are not watching the panel — a new repeater changes coverage, a new companion is a new person
nearby. sighop already knows the moment it happens (the contact store reports a created contact,
the greeter bot relies on exactly that), but the only way to learn of it today is to be looking at
the run output or the dashboard. Webhooks send that fact to whatever the operator already reads:
Discord, n8n, Home Assistant.

## What Changes

- **Webhooks as stored configuration.** New `webhook` table (migration `0006`): a name, the target
  URL (sealed at rest under `SIGHOP_SECRET_KEY`, because a Discord or n8n URL is itself the
  credential), a payload format (`json` or `discord`), the triggers it subscribes to, an optional
  `max_hops` filter, an enabled flag, and the outcome of its last delivery.
- **Two triggers now: `new_repeater` and `new_companion`.** Each fires once, when a verified advert
  *creates* a contact of node type `REPEATER` or `CHAT` respectively. A key already known (restored
  from the database, or pasted by an operator) does not fire. Room servers and sensors fire nothing.
  The trigger set is an enumeration built so `new_chatter` can be added later without a schema
  change; it is **not** part of this change.
- **Two payload formats.** `json` is a versioned, documented event (`schema: 1`, event name, event
  id, node key/hash/name/type/position, reception hop count/SNR/RSSI/time). `discord` is a Discord
  webhook message with one embed, advert content escaped and mentions disabled — advert names are
  written by strangers.
- **Best-effort delivery, off the reception path.** The contact-store listener only *offers* an
  event to a bounded queue; one delivery task POSTs it with a timeout, retries on network errors,
  `5xx` and `429` (honouring `Retry-After`) with backoff up to a small attempt limit, drops the
  oldest event on overflow, and counts every outcome. Events are not persisted: a restart or long
  outage loses pending ones.
- **Changes apply to a running process.** The delivery task reads the enabled webhooks from the
  database when an event arrives (events are rare), keeping the last good list if that read fails —
  so `sighop webhook …` in another process and the panel both take effect without a restart.
- **Contact store fans out to several listeners.** Today it has one slot, held by the bot host.
  It gains a list of listeners, each isolated from the others' failures.
- **`sighop webhook` command surface:** `add`, `list`, `show`, `enable`, `disable`, `set`
  (triggers, format, max hops), `set-url` (read from stdin, never argv), `remove`, and `test`
  (sends a sample event of a chosen trigger and reports the HTTP outcome).
- **Admin panel page `/admin/webhooks`** with the same operations through the same repository calls,
  the URL shown redacted to scheme and host, and last delivery outcome visible.
- **Run output:** startup reports how many webhooks are enabled (or that none run without a
  database, or on a replay); each delivery outcome is a log event; the periodic status line carries
  sent / failed / dropped counters.
- **Replay runs never send webhooks.** A replayed reception carries an earlier session's timestamps;
  announcing a week-old repeater as new would be false. Receive-only mode does not affect webhooks:
  they are not radio transmissions.
- `DESIGN.md` gains a webhooks section under §7 and the table count in §6 moves to twelve.

Not changing: what makes a contact (only verified adverts), bot behaviour, the greeter's first-sighting
logic, and the radio path.

## Capabilities

### New Capabilities

- `webhooks`: triggers, filters, payload formats, delivery and retry semantics, storage and sealing
  of webhook URLs, live pickup of configuration changes, and the replay/no-database rules.

### Modified Capabilities

- `contacts`: the observation listener becomes a set of listeners; a failing listener costs neither
  the contact nor any other listener's report.
- `runtime-cli`: new `sighop webhook` command surface; the run reports webhooks at startup and in
  the periodic status line.
- `web-admin`: new requirement that webhooks are configured, tested and inspected through the
  interface, with the URL never shown in full after it is stored.

## Impact

- New package `src/sighop/webhooks/` (events, payload rendering, dispatcher/delivery task).
- `src/sighop/net/contacts.py`: multiple observation listeners.
- `src/sighop/runtime.py`: dispatcher construction, listener wiring, startup and status lines,
  replay exclusion.
- `src/sighop/db/models.py`, `repositories.py`, `persistence.py`, `alembic/versions/0006_webhooks.py`.
- `src/sighop/db/sealing.py`: sealing for a variable-length value alongside the seed functions.
- `src/sighop/cli.py`: `webhook` subcommands.
- `src/sighop/web/routes/admin.py`, `templates/admin/webhooks.html`, panel navigation.
- No new runtime dependency: HTTP delivery uses the standard library in a worker thread.
- Outbound HTTPS from the container to operator-chosen hosts; nothing inbound.
- Tests: new `tests/test_webhooks*.py`, extended contact-store, CLI and web parity tests.
