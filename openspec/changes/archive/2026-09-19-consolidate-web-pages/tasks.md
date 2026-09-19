# Tasks

## 1. System page (modem + radio + schema)

- [x] 1.1 Create `src/sighop/web/routes/system.py` with `GET /system`, moving the schema handler body and the radio readback call out of `admin.py` (D1); include it in `app.py`. Verify: `uv run pytest tests/test_web_admin.py -k schema` passes once its paths are updated in 1.4.
- [x] 1.2 Create `templates/system.html` from `admin/radio.html` + `modem.html` (readback once, readback-mismatch warning, not-persisted note) + `admin/schema.html` (revision table, migrations, accounts, sealing secret), plus a "station controls" section showing gate state and current ceiling fraction with links to `/admin/transmit` and `/admin/ceiling`. Verify: a new test in `tests/test_web_admin.py` asserts the page contains both links and one readback table, and that GET `/admin/transmit` from it performs nothing (gate unchanged).
- [x] 1.3 Remove `GET /modem` from `app.py`, `GET /admin/radio` and `GET /admin/schema` from `admin.py`, and delete `modem.html`, `admin/radio.html`, `admin/schema.html`. Verify: `rg -n '"/modem"|/admin/radio|/admin/schema' src/sighop` returns nothing.
- [x] 1.4 Repoint tests: `tests/test_web_dashboard.py` (`/modem` → `/system`), `test_web_admin.py` (radio, schema), `test_web_write_parity.py` (schema enumerations), `test_web_auth_routes.py` (schema reads; `next=/admin/radio` → `next=/system`). Verify: those files pass.

## 2. One rooms page

- [x] 2.1 Move `admin.rooms` context building into `routes/rooms.py` as a builder plus a `render_index(request, page, *, refusal=None, status_code=200)` entry point used by `rooms.index` (D2). Verify: `uv run pytest tests/test_web_rooms.py` passes.
- [x] 2.2 Merge `admin/rooms.html` into `rooms/index.html`: served/unserved with reason, counts, access, read-only fallback, retention, action links (history, members, post, access, retention, rename, delete, advert zero-hop/flood), `?deleted=` notice, create-a-room form and refusal banner. Delete `admin/rooms.html` and `GET /admin/rooms`. Verify: test asserts one room row carries both `/rooms/{id}` and `/admin/rooms/{id}/delete` links.
- [x] 2.3 Repoint room POST success redirects (`create`, `password`, `retention`, `rename`, `delete`) to `/rooms` and refusal re-renders to `rooms.render_index`; repoint cancel/back links in `admin/room_{password,retention,rename,delete}.html`. Verify: `rg -n '"/admin/rooms"|/admin/rooms\?|href="/admin/rooms"' src/sighop` returns nothing, and room redirect tests assert `location == "/rooms"`.
- [x] 2.4 Repoint tests in `test_web_admin.py`, `test_web_adverts.py`, `test_web_write_parity.py` from `/admin/rooms` to `/rooms`. Verify: those files pass.

## 3. Channels administered from chat

- [x] 3.1 Move `admin.channels` context building into `routes/chat.py` and add `render_index(request, page, *, added="", removed="", refusal=None, status_code=200)`; `chat.index` reads `added`/`removed` from the query (D3). Verify: `uv run pytest tests/test_web_chat.py` passes.
- [x] 3.2 Extend the channels section of `chat/index.html` with the stored-channel join (kind, hash, key marking, message count, loaded/not-loaded, rename/remove links), added/removed notices, refusal banner, add-hashtag, add-PSK (key field always blank), re-add-Public forms, no-database statement, and the terminal-only PSK note. Delete `admin/channels.html` and `GET /admin/channels`. Verify: test asserts no stored PSK appears in `/chat` source (hex and base64) and that the add forms are absent with no database.
- [x] 3.3 Repoint `_refuse_channel` / `_channel_added` and rename/remove redirects to `/chat` (`/chat?added=…`, `/chat?removed=…`) and to `chat.render_index`; repoint cancel links in `admin/channel_{rename,remove}.html`. Verify: `rg -n 'href="/admin/channels"|"/admin/channels\?' src/sighop` returns nothing.
- [x] 3.4 Repoint channel tests in `test_web_admin.py` from `/admin/channels` to `/chat`. Verify: file passes.

## 4. Bots on their identity's page

- [x] 4.1 In `routes/keys.py` `identity`, load the bot bound to the entity and, for a bot-type identity, add bot context (driver, enabled, mode, config, durable state, running, advert id, drivers) and accept `refusal`/`status_code`/`bot_deleted` (D4). Verify: test opens a bot identity's page and sees its durable state keys.
- [x] 4.2 Fold `admin/bots.html`'s per-bot section and create form into `admin/identity.html` (create form: hidden `entity_id`, driver select only; hidden for non-bot identities). Delete `admin/bots.html`, `GET /admin/bots` and `_bot_identities`. Verify: test asserts a non-bot identity page has no create-a-bot form and a free bot identity page has one.
- [x] 4.3 Repoint bot POST redirects (`create`, `enabled`, `mode`, `config`, `greeted/*`, `state/clear`, `delete`) to `/admin/identities/{entity_id}` (delete adds `?bot_deleted=`), refusals to the identity page re-render, and back/cancel links in `admin/{bot_mode,bot_state,bot_delete,greeted}.html` and the "shown in full on the bots page" text in `greeted.html`. Verify: `rg -n '/admin/bots"' src/sighop` returns nothing; invalid-config test asserts the identity page re-renders with the driver's reason and 400.
- [x] 4.4 Make the identities list's "serving" entries link to the identity page. Repoint bot tests in `test_web_admin.py`, `test_web_adverts.py`, `test_web_write_parity.py`. Verify: those files pass.

## 5. Overview dedupe and identity counters

- [x] 5.1 Move `entity_rows` from `app.py` to `render.py`; call it from the identities list handler and add transmitted/suppressed/addressed columns to "loaded by this run" joined by public key, with the node-hash caveat note (D5). Verify: test asserts a loaded identity row shows its transmitted count on `/admin/identities`.
- [x] 5.2 Remove the "persistence" and "identities" sections from `overview.html` and the `entities` argument from the `/` handler. Verify: `tests/test_web_dashboard.py` passes after moving its identity-counter assertions to `/admin/identities`, and a degraded-persistence test asserts the meter strip still shows discarded-write counts.

## 6. Contacts start conversations

- [x] 6.1 Pass loaded identities to `/contacts`; add a column with one `as <name>` link per identity to `/chat/{entity}/{peer}`, or a no-identity statement (D6). Verify: test with two identities and one contact asserts both links; test with none asserts no `/chat/` link and the statement.
- [x] 6.2 Remove the "start a conversation" matrix and `contact_views` from `chat/index.html` / `chat.index`; point empty-state text to `/contacts`. Verify: `uv run pytest tests/test_web_chat.py` passes and `/chat` contains no contacts × identities table.

## 7. Navigation, docs, gates

- [x] 7.1 Set `base.html` nav to overview, contacts, chat, rooms, identities, webhooks, system. Verify: test asserts exactly those seven hrefs in the nav of `/`.
- [x] 7.2 Add a test that GETs `/modem`, `/admin/radio`, `/admin/schema`, `/admin/rooms`, `/admin/bots`, `/admin/channels` as a signed-in operator and asserts 404, and that no template under `src/sighop/web/templates` contains those hrefs. Verify: test passes.
- [x] 7.3 Update `DESIGN.md` (chat client paragraph and source tree mention of `/admin/channels`). Verify: `rg -n '/admin/channels\b[^/]' DESIGN.md` returns nothing.
- [x] 7.4 Run `./build.sh` (format, lint, types, tests). Verify: all gates pass.
