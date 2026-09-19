# Design

## Context

See proposal.md for motivation. Current shape that constrains the approach:

- Handlers are split across `app.py` (`/`, `/contacts`, `/modem`), `routes/rooms.py` (`/rooms`),
  `routes/chat.py` (`/chat`), `routes/keys.py` (`/admin/identities`) and `routes/admin.py`
  (everything else under `/admin`, ~2100 lines). The panel has no path-based privilege split: every
  page needs a signed-in operator, so moving admin content onto `/rooms` or `/chat` changes no
  access rule.
- List handlers in `admin.py` double as refusal re-renderers: `rooms(...)`, `bots(...)` and
  `channels(...)` take `refusal=`/`status_code=` and are called by the POST handlers when a write is
  refused (e.g. `_refuse_channel`). Success paths redirect with `303` to the list page, sometimes
  with a query flag (`/admin/rooms?deleted=…`, `/admin/channels?added=…`).
- `/modem` and `/admin/radio` both call `render.modem_readings(state.probe_result, state.radio)`.
- `/admin/transmit` and `/admin/ceiling` GET confirmation views exist and are guarded by nonce and
  password on POST; nothing links to them.
- The identities page's stored table already has a "serving" column built by `keys._bindings`
  (`"bot 'greeter'"`, `"room 'x'"`), which satisfies "the identities list indicates which identities
  carry a bot" without new data.
- Change `rename-to-siggynet` is planned (no tasks yet) and touches the same templates' wording.

## Goals / Non-Goals

**Goals:**
- Each surviving page's handler builds all of its context in one place, reused by the refusal
  re-render path so a refused write shows the same page it came from.
- No POST URL, nonce scope, guarded-action rule or structured event changes.

**Non-Goals:**
- Visual redesign, CSS changes beyond what new sections need, or JS.
- Redirects from removed URLs (user chose removal: 404).
- Moving `/admin/identities` or `/admin/webhooks` to top-level paths.
- Grouped/two-level navigation.

## Decisions

**D1 — `/system` is a top-level path served from a new `routes/system.py`.**
It holds the schema and radio handlers' logic moved out of `admin.py` (the `migrations` import, the
terminal-only notes constants stay where they are and are imported). Top-level because it sits in
the flat nav beside `/rooms` and `/chat`; a new module because `admin.py` is already the largest
route file and this page is read-only. Alternative: `/admin/system` in `admin.py` — rejected, keeps
growing the file for no access benefit. The page links to `/admin/transmit` and `/admin/ceiling` as
plain links to the existing confirmation views, which is what the guarded-action requirement
already permits (the GET mints a nonce and performs nothing). It shows current gate state and
ceiling fraction beside the links so the operator sees what they would change.

**D2 — `/rooms` absorbs `/admin/rooms` via a shared context builder.**
`admin.rooms` becomes `rooms_context(page) -> dict` (counts, served, advert ids, hosts) living in
`routes/rooms.py`; `rooms.index` merges it with the unserved reasons and renders `rooms/index.html`,
which takes over `admin/rooms.html`'s columns, actions and create form. Room POST handlers in
`admin.py` redirect to `/rooms` (`/rooms?deleted=…` after delete) and re-render refusals through a
`rooms.render_index(request, page, refusal=…, status_code=…)` entry point. Direction of import is
`admin → rooms`, matching today's `rooms.py` which does not import `admin.py`. The no-database /
degraded branches keep their current `collection.state` rendering and simply hide the admin columns.

**D3 — `/chat` absorbs `/admin/channels` the same way.**
`admin.channels` becomes `channels_admin_context(page, added, removed)` in `routes/chat.py`;
`chat.index` gains `added`, `removed`, `refusal`, `status_code` and merges it. The chat channels
table joins the loaded list (unread markers, open link) with the stored list (kind, count,
rename/remove, stored-but-not-loaded rows) by channel id; the add forms render below it only when a
database is configured. `_refuse_channel` / `_channel_added` in `admin.py` call
`chat.render_index(...)` and redirect to `/chat?added=…`. The PSK field stays blank on re-render —
same template code, moved.

**D4 — A bot is rendered on `/admin/identities/{entity_id}`.**
`keys.identity` looks up the bot bound to the entity (one identity plays one role, so at most one)
and, when the record's type is the bot entity type, adds the bot section (driver, enabled toggle,
mode link, config table, durable state, greeted/clear/delete links, advert links) or the create
form with `entity_id` as a hidden field and only the driver select. Bot POST handlers resolve
`bot_id → entity_id` (they already load the `BotRecord`) and redirect to that identity page; refused
create/config re-render the identity page with the refusal. A deleted bot redirects to the identity
page with `?bot_deleted=…` since the identity survives. `_bot_identities` (the "which identities can
carry a bot" filter) is no longer needed for a select and is dropped. Back/cancel links in
`bot_mode`, `bot_state`, `bot_delete`, `greeted` point at the identity page. Alternative considered:
keep a bots list and only link from identities — rejected by the user's scope choice.

**D5 — Identity traffic counters move by public-key join.**
`app.entity_rows(state, feed)` already computes transmitted/suppressed/addressed per loaded
identity; the identities handler calls it and joins to `adverts.stubs` by public key (the same key
the advert links use, per `admin._advert_id`) to add three columns and the node-hash caveat. It moves from `app.py` to `render.py` so `routes/keys.py` can import it without importing the app
factory. The overview drops its identities and persistence sections and stops calling it.

**D6 — Contacts carry conversation links; chat points to contacts.**
The `/contacts` handler passes `state.adverts.stubs`; each row gets one `/chat/{entity}/{peer}` link
per loaded identity (same URL the matrix built), rendered compactly (`as <name>`). With no loaded
identity the column is omitted and the page says a conversation needs an identity. `chat/index.html`
drops the matrix and `contact_views`; its empty-state text points to `/contacts`.

**D7 — Removed GETs are deleted, not stubbed.**
`GET /modem`, `/admin/radio`, `/admin/schema`, `/admin/rooms`, `/admin/bots`, `/admin/channels` are
removed along with `modem.html`, `admin/radio.html`, `admin/schema.html`, `admin/rooms.html`,
`admin/bots.html`, `admin/channels.html`. FastAPI's default 404 answers them, exactly as any other
unknown path does today (`_error_page` handles only unhandled exceptions, as 500). A test asserts each returns 404 and that no served template contains
those hrefs.

## Risks / Trade-offs

- [Chat and rooms pages get long] → Admin sections go below the reading content (conversations and
  channel list first; room list columns grouped read-then-configure), and the forms sit under their
  own `h2` so they are skippable.
- [Refusal re-render renders a different page than before] → Covered by the existing refusal tests
  after their paths are updated; each re-render keeps its 400/409 status.
- [`web-write-parity` tests enumerate admin pages for write parity/guard checks] → Update the
  enumerations to the surviving pages; the POST endpoints under test are unchanged.
- [Bookmarks to removed pages 404] → Accepted per user decision; single-operator panel.
- [Conflicts with `rename-to-siggynet`] → Both edit template wording; whichever lands second
  rebases. No structural overlap (rename touches names, not routes).

## Migration Plan

Single deploy; no data or config change. Rollback is reverting the commit.
