# Proposal

## Why

The panel's navigation has grown to twelve links, several of which show the same thing twice or
split one concern across two pages: `/modem` and `/admin/radio` render the same `modem_readings`
table, `/rooms` and `/admin/rooms` list the same rooms with different columns and link to each
other, channel add/remove lives one page away from the chat page that lists channels, and bots are
configured on a page separate from the identity each bot *is*. Meanwhile the two station-wide
guarded actions — enabling transmission and raising the airtime ceiling — have confirmation views
(`/admin/transmit`, `/admin/ceiling`) that no page links to. Fewer, fuller pages make the panel
easier to find things in without removing any capability.

## What Changes

- **New `/system` page** replacing `/modem`, `/admin/radio` and `/admin/schema`: the board's
  readback shown once (with the not-persisted-by-the-board statement), the schema revision
  agreement, the terminal-only notes (migrations, accounts, sealing secret), and links to the
  enable-transmission and raise-ceiling confirmation views, which become reachable from navigation
  for the first time. **BREAKING**: `GET /modem`, `GET /admin/radio`, `GET /admin/schema` are
  removed (404).
- **One rooms page**: `/rooms` becomes the single rooms list, carrying served state and unserved
  reason, member and message counts, access, retention, and every per-room action (history,
  members, post, access, retention, rename, delete, advert), plus the create-a-room form.
  **BREAKING**: `GET /admin/rooms` is removed; room writes redirect to `/rooms`.
- **Channels administered from chat**: `/chat`'s channels section gains per-channel rename and
  remove links, stored-but-not-loaded channels, message counts, and the add-hashtag, add-PSK and
  re-add-Public forms. **BREAKING**: `GET /admin/channels` is removed; channel writes redirect to
  `/chat`.
- **Bots configured on their identity's page**: `/admin/identities/{id}` for a bot-type identity
  shows that bot's driver, enabled state, mode, configuration, durable state and actions, or the
  create-a-bot form when it carries none. The identities list says which identity carries a bot.
  **BREAKING**: `GET /admin/bots` is removed; bot writes redirect to the identity's page.
- **Overview de-duplicated**: the overview's "persistence" section (already in the meter strip on
  every page) and its "identities" table are dropped; the per-identity traffic counters
  (transmitted, suppressed, addressed) move onto the identities page's "loaded by this run" table.
- **Contacts link to conversations**: each contact row gains "message as <identity>" links, one per
  loaded identity; chat's contacts × identities "start a conversation" matrix is removed and
  replaced with a pointer to contacts.
- **Navigation** shrinks from twelve links to seven: overview, contacts, chat, rooms, identities,
  webhooks, system.
- POST action URLs (`/admin/rooms/...`, `/admin/bots/...`, `/admin/channels/...`,
  `/admin/transmit`, `/admin/ceiling`) and the per-item confirmation views under them are unchanged;
  only where their "back"/"cancel" links and success redirects point changes.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `web-dashboard`: modem readback moves off the dashboard to the system page; persistence state is
  presented by the meter strip rather than an overview section; adds a navigation requirement
  naming the seven pages and that each concern is on exactly one of them.
- `web-admin`: radio readback and schema revision are on the system page, which also links the
  transmit and ceiling confirmations; bots are configured on their identity's page; channels are
  administered from the chat page; loaded identities show traffic counters; deletion scenarios name
  the pages that now carry the delete links.
- `web-rooms`: the room list is the one rooms page and carries each room's configuration and
  administration actions alongside its served state.
- `web-chat`: the channels section of chat carries channel administration; conversations with a
  contact are started from the contacts page.

## Impact

- Code: `src/sighop/web/app.py` (`/`, `/modem`), `src/sighop/web/routes/admin.py` (rooms, bots,
  channels, schema, radio list handlers and every redirect/refusal re-render that targets them),
  `src/sighop/web/routes/rooms.py` (index), `src/sighop/web/routes/keys.py` (identity list and
  detail), `src/sighop/web/routes/chat.py` (index), new `src/sighop/web/routes/system.py`,
  `src/sighop/web/render.py` (receives `entity_rows` from `app.py`).
- Templates: `base.html`, `overview.html`, `contacts.html`, `modem.html` (removed),
  `admin/radio.html`, `admin/schema.html` (merged into new `system.html`), `admin/rooms.html`
  (merged into `rooms/index.html`), `admin/bots.html` (folded into `admin/identity.html`),
  `admin/channels.html` (folded into `chat/index.html`), `admin/identities.html`, and the
  back/cancel links in `admin/{bot_mode,bot_state,bot_delete,greeted,room_*,channel_*}.html`.
- Tests: `tests/test_web_dashboard.py`, `test_web_admin.py`, `test_web_adverts.py`,
  `test_web_write_parity.py`, `test_web_auth_routes.py`, `test_web_chat.py`, `test_web_rooms.py`
  request the removed paths or assert redirect locations.
- Docs: `DESIGN.md` mentions `/admin/channels` (feature list, source tree).
- No schema, CLI, protocol or dependency change. Bookmarks to the six removed pages break.
