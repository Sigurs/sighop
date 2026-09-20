# Proposal

## Why

The panel's compaction work (`compact-web-status-display`, `consolidate-web-pages`) made the
interface dense but left four rough edges an operator hits on the first session: the copy control
destroys the layout it was built to protect, the panel has no phone layout and no dark rendering at
all, the chat page is a 196-line wall, and a conversation that refreshes every three seconds cannot
be read while it does. None of these are new features — each is a place where the existing
behaviour works against the operator.

The copy control is the sharpest. `web-display` requires that where the clipboard is unavailable
the control "instead select the full key as text", and `static/display.js` implements that by
overwriting the abbreviation with the full 64-character key. `window.isSecureContext` is false on
plain HTTP, which is how the panel is served on a LAN — so the fallback is the *normal* path, and
every copy click permanently widens the column. The requirement is right that a keyless clipboard
still has to yield the key; it is wrong that the row must be sacrificed to do it.

## What Changes

- **Copy never changes what the row shows.** The control copies the full key whether or not the
  page is in a secure context, and the abbreviation beside it is unchanged afterwards. Where no
  copy is possible at all, the key is offered for manual selection somewhere that does not reflow
  the table, and the abbreviation is restored when the operator moves on.
- **The panel lays out at phone width.** Wide tables scroll inside their own box rather than
  widening the page (the `.table-wrap` rule that exists but is used on one page of nineteen), the
  header and navigation wrap, and no page scrolls horizontally as a whole.
- **The one palette is audited rather than doubled.** The panel is already dark by design
  (`DESIGN.md` §8: "Dark first, because this sits beside other radio tooling") and already defines
  its colours as named values. No second scheme is added; instead the last colours stated outside
  the palette are folded into it, and the rule the interface rests on — nothing distinguishable by
  colour alone, every text colour legible against every background it lands on — is made checkable
  rather than a matter of care.
- **Navigation marks the page being viewed**, for sighted operators and for assistive technology.
- **The chat page is decongested.** Channel administration (add hashtag, add pre-shared-key, add
  Public back) moves behind a disclosure that is closed by default; the standing explanatory notes
  move to hover where `web-display` already puts explanations, keeping what a spec requires stated.
- **The composer counts bytes as it is typed** against the single-message limit, so the refusal
  `web-chat` already specifies is predicted rather than discovered on submit, and a send can be
  submitted from the keyboard.
- **A conversation refreshing in place keeps the reader's place**: new messages are added without
  replacing rows already on screen, so a selection or a scroll position survives the 3-second tick.

No behaviour on the air changes. Nothing is transmitted that was not transmitted before, and every
guard (`_token`, the refusal rules, the verification markings) stays where it is.

## Capabilities

### New Capabilities

None. Every change lands on an existing capability.

### Modified Capabilities

- `web-display`: the copy control's fallback must not alter the abbreviation it sits beside
  (existing requirement modified); two requirements added — phone-width layout, and one palette
  defined once and legible throughout — since both are shared conventions rather than one page's
  concern.
- `web-chat`: composed length is counted against the limit as it is typed and a send is submittable
  from the keyboard; a conversation refreshed in place preserves what the operator has selected or
  scrolled to; channel administration is present but not expanded by default.
- `web-dashboard`: navigation marks which of its pages is being viewed (its "Navigation names each
  concern once" requirement).

## Impact

- `src/sighop/web/static/display.js` — copy path rewritten; fallback no longer mutates the `<code>`.
- `src/sighop/web/static/panel.css` — `@media` breakpoints, `prefers-color-scheme` dark palette,
  current-page navigation state, disclosure styling.
- `src/sighop/web/templates/_display.html` — the `key()` macro gains whatever the new fallback needs.
- `src/sighop/web/templates/base.html` — navigation marks the current page.
- `src/sighop/web/templates/chat/index.html` — channel administration behind a disclosure.
- `src/sighop/web/templates/chat/conversation.html`, `chat/_messages.html`,
  `chat/_channel_messages.html` — byte counter, keyboard send, refresh that preserves the reader's place.
- Every template holding a `<table>` — wrapped for horizontal scroll.
- `tests/test_web_display.py`, `tests/test_web_chat.py`, `tests/test_web_dashboard.py`.
- Constraint from `web-server`: no build step and no third-party runtime fetches, so all of this is
  vanilla CSS and the existing `display.js`, with no framework added.
