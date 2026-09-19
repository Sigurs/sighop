# Tasks

## 1. Formatting helpers and Status value

- [x] 1.1 Add `duration()` and `plural()` to `src/sighop/web/render.py` (3010 ms → "3.0 s", 90 s → "1 m 30 s", 1 → "1 attempt", 2 → "2 attempts"); verify with new unit tests in `tests/test_render.py` or a new `tests/test_web_display.py`
- [x] 1.2 Add the `Status` dataclass (design D1) to `render.py`; verify it imports cleanly and `./build.sh` types gate passes
- [x] 1.3 Change `IdentityView.short_key` to 3 bytes (6 hex) and `RouteView` to expose a route `Status` (⇢, hops · path, "direct, zero hops" / "n hops via path" explanation, ambiguity caveat kept); verify with unit tests covering zero-hop and hash-matched routes

## 2. Template macros and Jinja wiring

- [x] 2.1 Create `templates/_display.html` with `status(s)`, `key(hex)` and `when(dt, empty='—')` macros per D1/D4/D5 (title + visually-hidden explanation, `type="button"` copy control, `<time class="when" datetime=…>` with compact UTC fallback); verify by rendering each macro in a test and asserting markup
- [x] 2.2 Add `install_display(env)` registering helpers, call it wherever `Jinja2Templates` is built in `app.py`; verify an app test page renders using a helper
- [x] 2.3 Add `.visually-hidden`, `.status-*`, `.key`, `.copy`, `.when` styles and `font-variant-emoji: text` to `static/panel.css`; verify in the running panel (`/run` skill) glyphs are monochrome-capable and coloured per class

## 3. Client script

- [x] 3.1 Create `static/display.js`: relative time rendering (past and future, thresholds in D5), local+UTC title, 30 s tick, `htmx:afterSwap` hook, `window.sighopDisplay`; delegated `.copy` click with clipboard/selection fallback and ✓ confirmation; verify manually in browser and that `tests/test_web_assets.py` passes
- [x] 3.2 Load `display.js` from `templates/base.html` with `defer`; verify every page includes it and asset tests pass

## 4. Chat states

- [x] 4.1 Replace `_state_text` in `routes/chat.py` with a function returning `Status` values per the D2 table (awaiting, attempt n, delivered with attempts · latency, unacknowledged with attempts, dropped, received); update `_message_view`; verify `tests/test_web_chat.py` delivered/unacknowledged cases assert glyph, figures and hover sentence
- [x] 4.2 Replace `_channel_state_text` with `Status` values (received + hops, transmitted, repeats count, not transmitted with visible reason, unknown); drop per-row no-ack prose; verify tests for "transmitted, 3 repeats", "transmitted, 0 repeats", "received, 2 hops" and not-transmitted reason visibility
- [x] 4.3 Render states via `status()` in `chat/_messages.html` and `chat/_channel_messages.html`; confirm `POST_NOTE` still shows on the channel page; verify channel page test asserts the no-ack statement appears once on the page and in the transmitted hover
- [x] 4.4 Guessable marks in `chat/channel.html` and `chat/index.html` via a guessable `Status` (◌ + GUESSABLE_NOTE hover); verify listing test still finds the guessable marking

## 5. Identity and claim marks

- [x] 5.1 `_identity.html`: move status word into visually-hidden, keep glyph + colour + `title`; `identity_full` uses `key()`; update `verification_legend()` to include the claim glyph; verify identity tests and `test_web_dashboard.py` verification scenarios
- [x] 5.2 `_claimed.html`: claim glyph “ (distinct from ✗), words to visually-hidden, hover explanation kept; verify `test_web_chat.py` claimed-name test asserts the distinct glyph and no verified class

## 6. Keys and timestamps across templates

- [x] 6.1 Replace displayed full keys with `key()` in `contacts.html`, `rooms/index.html`, `rooms/members.html`, `rooms/revoke.html`, `admin/identities.html`, `admin/identity.html`, `admin/identity_remove.html`, `admin/identity_renamed.html`, `admin/advert.html`, `admin/greeted.html`, `admin/revealed.html` (public key only); leave hrefs, form values, inputs and the revealed private key untouched; verify `grep -rn '\.hex()\|key_hex' src/sighop/web/templates` shows only link/form/value uses and tests pass
- [x] 6.2 Replace every displayed `.isoformat()` in templates with `when()` (chat, channel incl. sender's-clock, rooms history/members, contacts, identities, identity, identity_rename, advert, webhooks); convert `render.py`/`routes/keys.py`/`routes/admin.py` values that pre-format ISO for display to pass datetimes; verify `grep -rn isoformat src/sighop/web/templates` is empty and page tests pass
- [x] 6.3 Route cells in `contacts.html` via route `Status`; update `tests/test_web_dashboard.py` "2 hop(s) via bed0" assertion; verify zero-hop and ambiguous scenarios still pass

## 7. Feed

- [x] 7.1 `static/feed.js`: time cell as `<time class="when">` with local time-of-day incl. ms and full local/UTC hover (feed exception, D5); direction as ↓/↑ with `title`; verify in running panel and `tests/test_web_feed.py` passes

## 8. Plurals and durations sweep

- [x] 8.1 Replace `(s)` pseudo-plurals in web templates/routes (e.g. `admin/greeted.html` attempts, feed boundary "record(s)", status lines) with `plural()`; format ack latency and flood intervals with `duration()`; verify `grep -rn '(s)' src/sighop/web --include=*.html --include=*.py --include=*.js` returns only non-user-facing matches

## 9. Integration

- [ ] 9.1 Run `./build.sh` (format, lint, types, test) and fix failures; verify it exits 0
- [ ] 9.2 Launch the panel and check a DM conversation, a channel, contacts, identities and the feed in a browser: glyphs, hover text, copy button (on localhost), relative times advancing, HTMX refresh without stale fallback text; verify with screenshots
