# Design

## Context

See `proposal.md` — Why. The constraints that shape every decision below:

- **No build step, no runtime fetch** (`web-server`: "The interface is served without a build step
  or third-party runtime fetches"). Everything is vanilla CSS and vanilla JS under
  `src/sighop/web/static/`, and `tests/test_web_assets.py` already asserts that every `src`/`href`
  a template names resolves to a file there. A new library is allowed only vendored, with its
  version, source, SHA-256 and licence recorded in `static/VENDORED.md` the way `htmx.min.js` is.
- **The page must work without script.** The panel already renders correct markup server-side and
  `display.js` only improves it (compact UTC becomes relative time, a copy button becomes usable).
  Everything added here keeps that shape: the byte counter, the keyboard send and the copy control
  are improvements over markup that already works.
- **Shared context has one seam.** `Panel.context` in [deps.py:85-94](src/sighop/web/deps.py#L85-L94)
  builds `token` and `signed_in_as` for every page from the `Request`. Anything else every page
  needs belongs there, not in thirty route handlers.
- **All four standalone pages link `panel.css`** (`login.html`, `setup.html`, `error.html`), so
  palette and layout work reaches them without touching them.
- **The two message limits are not the same shape.** `MAX_TEXT_LEN = 160`
  ([net/dm.py:94](src/sighop/net/dm.py#L94)) counts the message text alone; `MAX_CHANNEL_TEXT_LEN =
  160` ([net/channels.py:82](src/sighop/net/channels.py#L82)) counts `name: text` — the sending
  identity's name and `": "` ride inside the same 160 bytes.

## Goals / Non-Goals

**Goals:**

- The copy control's behaviour stops depending on whether the panel happens to be served over TLS.
- One palette definition, rendered light or dark from the viewer's own preference.
- Layout work done once in CSS and in a wrapper element, not per page and not in script.
- The conversation poll stops being something the operator has to work around.

**Non-Goals:**

- No operator-facing theme switch, and nothing stored per operator: a preference the browser
  already reports needs no setting, no cookie and no column.
- No redesign of the information architecture — `consolidate-web-pages` settled which page holds
  what, and this change moves nothing between pages.
- No change to what is transmitted, queued, refused or recorded. Every guard stays where it is.
- No responsive rework of the live packet feed's own scroll box; it already scrolls itself.

## Decisions

### D1 — The copy control never writes into the key it sits beside

Three tiers, tried in order, in `display.js`:

1. `navigator.clipboard.writeText` where `window.isSecureContext` and the API are both present.
2. Otherwise a `<textarea>` appended to `<body>`, positioned fixed and off-screen, holding the full
   key, selected, `document.execCommand("copy")`, then removed. This is what makes plain HTTP work,
   which is the deployment the panel is actually served in.
3. Only if both fail: a small popover anchored to the key, out of normal flow
   (`position: absolute`), holding the full key selected for manual copying, dismissed on Escape,
   on blur or on a click elsewhere.

The `<code>` element is never written to in any tier, which is what makes "no column changes width"
(`web-display`) hold by construction rather than by care. The existing `confirm()` — swap the glyph
for `✓` for 1.5 s — runs on tiers 1 and 2.

*Alternatives:* keep tier 3 as the only fallback but restore the abbreviation on a timer (rejected:
the row still reflows, and a timer racing the operator's selection is worse than no fallback);
always use `execCommand` (rejected: deprecated, and the Clipboard API is the one that survives).

### D2 — The current page is derived centrally from the request path

`Panel.context` gains `current_nav`, computed from `request.url.path` against the seven navigation
entries by longest-prefix match, so `/chat/<entity>/<peer>` marks `chat`. `base.html` renders
`aria-current="page"` and a `.current` class on the matching link. The marking is a weight and a
rule under the link, not a colour, so it survives the monochrome rule `web-display` already carries
for statuses.

*Alternative:* each route passes its own nav key (rejected: thirty-odd call sites, and every new
route is one forgotten flag away from an unmarked page).

### D3 — One palette, audited, not a second one added

This started as "the panel has no dark mode". It has no *light* mode: `panel.css` is dark by
design (its own header, and `DESIGN.md` §8: "Dark first, because this sits beside other radio
tooling") and already defines the whole palette as custom properties on `:root`, referred to by 75
`var(--…)` uses. A second scheme was considered and dropped — the panel sits beside other radio
tooling, and that was a decision, not an omission.

What is left is the audit, and making its rule checkable:

- **Two colours are still stated outside the palette** ([panel.css:132](src/sighop/web/static/panel.css#L132)
  and [panel.css:256](src/sighop/web/static/panel.css#L256)). They become named values like the
  rest, so the palette is genuinely the one place a colour is decided.
- **Contrast is measured, not assumed.** Every text colour against every background it is placed
  on, at the 4.5:1 ratio WCAG asks of body text. Measured before writing this: the palette passes,
  the narrowest pair being `--alarm` on `--bg-raised` at 5.17:1. The point of the test is the day
  someone dims a token.
- **Nothing rests on colour alone** — already true, already required of statuses by `web-display`
  and of markings by `web-dashboard`, and extended here to budget state and direction.

A pytest computes the WCAG ratio from the stylesheet's own values rather than from a table copied
into the test, so the check follows the palette when it changes.

*Alternative:* a `prefers-color-scheme: light` block giving the panel a light rendering (rejected
by the operator: dark beside other radio tooling is the point); a `data-theme` toggle with a stored
preference (rejected for the same reason, plus a setting to explain and a place to keep it).

### D4 — Wide tables scroll in a box; the breakpoint is one value

Every `<table>` in `templates/` is wrapped in the existing `.table-wrap` (currently used on exactly
one page of nineteen). One `@media (max-width: 40rem)` block lets `.panel-head` and `.nav` wrap,
drops the panel's side padding and lets `.composer` controls stack. Nothing is hidden at a narrow
width — the spec forbids it, and a status the operator cannot see is the failure mode this whole
interface is built against.

`tests/test_web_assets.py` gains an assertion that every `<table>` in `templates/` has a
`.table-wrap` ancestor, so the next table added does not quietly reintroduce the page-wide scroll.

### D5 — The conversation is morphed, not replaced

`chat/_messages.html` and `chat/_channel_messages.html` poll every 3 s with
`hx-swap="outerHTML"`, which discards and rebuilds every row — losing selection and scroll. They
move to `hx-ext="morph"` with `hx-swap="morph:outerHTML"`, backed by **idiomorph vendored into
`static/`** alongside htmx (same author, same 0BSD/BSD terms, recorded in `VENDORED.md` with its
version, source URL and SHA-256). Morphing touches only nodes whose content actually differs, so a
message row whose text and state are unchanged is left alone entirely, and the only thing that
changes on a quiet tick is the text node inside each `<time class="when">`.

`display.js` already re-renders timestamps on `htmx:afterSwap`; that stays, and runs after the
morph settles.

*Alternatives:* pause the poll while a selection exists inside the conversation (rejected: cheap,
but it leaves the 3-second flicker and the needless state churn, and it fails the "not redrawn
unless what it states has changed" requirement outright); a bespoke keyed reconcile in `display.js`
(rejected: it has to special-case the `<time>` elements `display.js` itself rewrote, which is
exactly the subtlety a library has already solved).

### D6 — The counter counts the bytes the refusal counts

The composer carries the limit and, for a channel post, the prefix that rides inside it, as data
attributes written by the route that already knows them:

- Direct message: `data-limit="160"`, no prefix. Counted as `TextEncoder().encode(text).length`.
- Channel post: `data-limit="160"` and `data-prefix="<identity name>: "`, counted as the encoded
  length of prefix plus text — the same bytes `send_channel_post` measures.

Because the prefix depends on the chosen identity, the counter recomputes when the identity select
changes. Over the limit, the counter states by how much, matching the refusal's wording, and the
send control is *not* disabled: the refusal at submission is the authority, and a disabled button
that a script mis-computed is a panel that cannot send.

### D7 — Ctrl+Enter submits the form the button submits

A delegated `keydown` inside `.composer textarea`: Ctrl+Enter or Meta+Enter (macOS) calls
`form.requestSubmit()` — not `form.submit()`, which would skip validation and the submit event.
Plain Enter is untouched, so a multi-line message is still writable.

### D8 — Channel administration goes behind `<details>`, not behind script

The three add-a-channel forms on `chat/index.html` move inside a single `<details>` titled for what
it holds. It is rendered with `open` whenever `refusal`, `just_added` or `removed` is in the
context, which keeps `web-admin`'s "the chat page is re-shown with the reason the command line
gives" visible without interaction, and keeps it working with no script at all. The standing prose
notes that duplicate an explanation already reachable on hover move to `title`; every statement a
spec requires to be *stated* stays visible text.

## Risks / Trade-offs

- **A second vendored library (idiomorph) to keep pinned and reviewed** → recorded in `VENDORED.md`
  with version, source, SHA-256 and licence exactly as htmx is, and covered by the existing
  no-external-origin assertion in `tests/test_web_assets.py`.
- **The colour refactor could shift the light rendering** → the hoisted values are the current hex
  values verbatim; the light rendering is compared before and after on the overview, contacts, chat
  and system pages.
- **`execCommand("copy")` is deprecated** → it is reached only where the Clipboard API is
  unavailable, and tier 3 covers the day it is removed.
- **Morphing could interact badly with `display.js`'s timestamp rewriting** → the rewrite already
  runs on `htmx:afterSwap` over the whole document; the case to check in test is a tick that
  changes nothing, where the relative time must still advance.
- **The counter could drift from the server's rule** → the limit and the prefix come from the same
  route that constructs the refusal, as data attributes, rather than being restated in the script.
- **Wrapping every table changes DOM depth** → existing web tests assert on text and on links, not
  on element nesting, so the wrapper is invisible to them; the new assertion in
  `tests/test_web_assets.py` is what keeps it in place.

## Migration Plan

None. No schema, no stored state, no configuration. The change is static assets, templates and one
computed context key; rollback is a revert, and a browser that has cached `panel.css` or
`display.js` picks the change up on its next load.
