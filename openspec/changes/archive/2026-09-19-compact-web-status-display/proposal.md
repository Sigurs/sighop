# Proposal

## Why

The panel's dense views spend most of their width on prose that repeats on every row:
`delivered — acknowledged after 1 attempt(s), 3010 ms`, `transmitted; no acknowledgement exists for
channel messages; repeat heard 2x — a repeater forwarded it`, full 64-hex public keys, and
microsecond ISO timestamps like `2026-09-14T17:35:56.857462+00:00`. The explanations are correct and
must stay available, but they belong on hover, not inline in every row, where they bury the message
text the operator is actually reading.

## What Changes

- **Status icons with hover explanation.** Every message/delivery state becomes a glyph plus its
  essential numbers, with the full explanatory sentence on hover (and to assistive technology):
  - DM delivered → `✓✓ 1 · 3.0 s` (icon, attempts, latency); unacknowledged → icon + attempts;
    awaiting, attempt-in-progress (icon + attempt number), dropped → icons.
  - Channel received → icon + hop count (`↓ 2`); transmitted → icon; repeats heard → repeat icon +
    count (`⟲ 2`); not transmitted → icon with the reason still visible; outcome unknown → icon.
  - The per-row "no acknowledgement exists for channel messages" is dropped from the row and kept
    in the transmitted icon's hover text and the channel page's standing note.
- **Verification marks as icons.** `identity` and claimed-sender markings drop the visible words
  "verified" / "unverified" / "key only" / "claimed, unverified"; the glyph (✓ ✗ ?, and a distinct
  claim glyph) remains, coloured, with its meaning on hover. The page legend still states what each
  glyph means. Glyph shape, not colour, remains the distinguishing signal.
- **Short public keys with copy.** Public keys render as their first 3 bytes (6 hex chars) with a
  copy control that puts the full key on the clipboard; the full key is on hover. Applies to every
  view that shows a public key, including identity detail pages. A private key revealed on purpose
  (`admin/revealed.html`) and key input fields are unchanged.
- **Human timestamps.** Timestamps render relative to now (`just now`, `3 min ago`, `2 h ago`,
  `4 d ago`) in the viewer's local clock, kept current by a small static script, including after
  HTMX partial refreshes. Hover shows the full local date-time and the exact UTC ISO value. Without
  JavaScript the server's compact UTC form is shown.
- **Compact routes and feed.** Contact routes `2 hop(s) via bed0` → hop icon + count + path; feed
  direction column as ↓/↑ arrows; feed time cell gets the full timestamp on hover.
- **Guessable-channel mark** becomes an icon with the guessable explanation on hover.
- **Durations and plurals.** One duration format (`3010 ms` → `3.0 s`, `90 s` → `1 m 30 s`) for ack
  latency and flood intervals; `(s)` pseudo-plurals in the web interface become real plurals.

No change to what is recorded, transmitted, or authenticated. Terminal (`monitor`) rendering is
unchanged.

## Capabilities

### New Capabilities
- `web-display`: shared display conventions for the web interface — status glyphs with hover and
  accessible explanations, abbreviated public keys with copy-to-clipboard, relative local
  timestamps with exact values on hover, durations and plurals.

### Modified Capabilities
- `web-chat`: DM send state is shown as glyph + attempts + latency with the full statement on
  hover; a transmitted channel post no longer states per row that no acknowledgement exists (the
  statement moves to hover and the channel page note); received channel message shows hop count
  with a glyph.
- `web-dashboard`: contact table shows the public key abbreviated with the full key copyable;
  verification marking may be glyph-only in dense views provided the glyph, not colour, carries the
  distinction and its meaning is available on hover and to assistive technology.

## Impact

- `src/sighop/web/routes/chat.py` — `_state_text` / `_channel_state_text` return structured state
  (glyph, numbers, explanation) instead of prose.
- `src/sighop/web/render.py` — `IdentityView.short_key` to 3 bytes, `RouteView.text`, new display
  helpers (duration, plural).
- Templates: `_identity.html`, `_claimed.html`, new `_display.html` macros (timestamp, key, status);
  roughly 20 templates switch from `.isoformat()` / `.hex()` to those macros.
- Jinja environment (`app.py`) gains filters/globals for the helpers.
- New static script `static/display.js` (relative time, local clock, copy); `static/feed.js`
  timestamps and direction; `static/panel.css` glyph and copy-control styles.
- Tests asserting old prose (`test_web_chat.py`, `test_web_dashboard.py`) updated; new tests for the
  helpers and asset presence.
- No dependency, schema, or API change. The pending `rename-to-siggynet` change moves these files
  to `src/siggynet/`; whichever lands second rebases paths only.
