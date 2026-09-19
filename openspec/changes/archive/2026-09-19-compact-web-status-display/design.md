# Design

## Context

State prose is built in Python (`routes/chat.py` `_state_text`, `_channel_state_text`;
`render.py` `RouteView.text`, `IdentityView.marking_text`) and dropped into templates as one string.
Timestamps are `.isoformat()` in ~20 templates plus `feed.js` (`record.at.slice(11, 23)`). Keys are
`.hex()` / `key_hex` in ~10 templates; `IdentityView.short_key` is 8 bytes. Identity marks already
come through two macros (`_identity.html`, `_claimed.html`), which already use a glyph *and* a word.

Constraints: no build step, no external origin (`web-server`; enforced by `tests/test_web_assets.py`),
HTMX partials refresh every 3 s (`chat/_messages.html`, `chat/_channel_messages.html`), and the
"never unverified as verified / not colour alone" rule (`web-dashboard`).

## Goals / Non-Goals

**Goals:**
- One place each for: status drawing, key drawing, timestamp drawing, duration/plural formatting.
- Explanations stay in the DOM (hover + assistive technology), so nothing the specs require to be
  *stated* is lost, only moved off the visible row.

**Non-Goals:**
- Terminal / `monitor` rendering (`render_*` in the CLI path, `tests/test_render.py`) — unchanged.
- Webhook payloads, events, JSON serialisation (`serialize.py`) — unchanged, still ISO.
- Touch-friendly tooltips beyond native `title` (see Risks).
- Layout redesign or new colours beyond glyph classes.

## Decisions

### D1 Status is a value, drawn by one macro
Add to `render.py`:

```python
@dataclass(frozen=True, slots=True)
class Status:
    kind: str  # css suffix, e.g. "delivered", "rx", "repeats"
    glyph: str
    figures: tuple[str, ...] = ()  # visible numbers: ("1", "3.0 s")
    explanation: str = ""  # full sentence: hover + screen reader
    reason: str = ""  # occurrence-specific, stays visible
```

`_state_text` → `_state(record) -> tuple[Status, ...]` (a channel post can carry two: transmitted +
repeats). New `_display.html` macro `status(s)` emits

```html
<span class="status status-{kind}" title="{explanation}">
  <span class="status-glyph" aria-hidden="true">{glyph}</span>
  {figures joined by " · "}{reason}
  <span class="visually-hidden">{explanation}</span>
</span>
```

`title` gives the hover; the visually-hidden span gives screen readers the sentence (a bare `title`
or `aria-label` on a role-less span is not reliably announced). Existing test assertions on prose
keep working against the hidden text, which keeps churn low.

*Alternative:* CSS-only tooltip (`[data-tip]:hover::after`). Rejected for now — `title` is free,
consistent, and the hidden span covers accessibility; can be layered later without changing markup.

### D2 Glyphs are Unicode text, not an icon font or SVG
No asset pipeline, no external fetch, renders in monochrome. Glyphs that have emoji presentation get
U+FE0E (text variation selector) so they stay monochrome and take CSS colour. Table:

| status | glyph | figures | hover (explanation) |
|---|---|---|---|
| DM awaiting | ◷ | — | awaiting transmission |
| DM attempt in progress | ↻ | attempt n | attempt n in progress |
| DM delivered | ✓✓ | attempts · latency | delivered — acknowledged after 1 attempt, 3.0 s |
| DM unacknowledged | ⚠︎ | attempts | unacknowledged after 4 attempts — the platform cannot tell whether it arrived |
| DM dropped | ⊘ | — | never reached the air — the scheduler dropped it |
| DM received | ↓ | — | received |
| channel received | ↓ | hops (`?` if unknown) | received over 2 hops |
| channel transmitted | ↑ | — | transmitted; no acknowledgement exists for channel messages |
| channel repeats (>0) | ⟲ | count | repeat heard 2 times — a repeater forwarded it |
| channel repeats (0) | (no repeat glyph; text in transmitted hover) | — | no repeat heard — which does not mean it was not received |
| channel not transmitted | ✕ | reason visible | not transmitted; not retried |
| outcome unknown | ⁇ | — | outcome unknown — the run stopped before it resolved |
| identity verified / unverified / key only | ✓ / ✗ / ? (existing) | — | existing `marking_text` |
| claimed channel sender | “ | — | a name the message claims; channel sender names are not authenticated |
| guessable channel | ◌ | — | GUESSABLE_NOTE |
| route | ⇢ | hops · path (`0 · direct`) | 2 hops via bed0 / direct, zero hops |
| feed RX / TX | ↓ / ↑ | — | received / transmitted |

The claimed-sender glyph changes from ✗ to “ so a claim is no longer drawn with the same mark as an
unverified contact — the modified `web-dashboard` requirement asks for distinct glyphs.
Exact glyphs may be tuned during implementation if one renders poorly; distinctness is the rule.

### D3 Visible verification words go, legend stays
`_identity.html` drops `identity-status` visible text into `visually-hidden`; `_claimed.html` same
for "claimed, unverified" / "no sender named". `verification_legend()` is kept and gains the claim
glyph. Colour classes unchanged.

### D4 Keys: `key()` macro, 3 bytes, copy via delegated click
`IdentityView.short_key` → `public_key.hex()[:6]`. Macro `key(hex)`:

```html
<span class="key"><code class="mono" title="{full}">{full[:6]}…</code>
  <button type="button" class="copy" data-copy="{full}" title="copy full key">⧉</button></span>
```

`type="button"` so it never submits an enclosing form. `display.js` handles clicks on `.copy` by
delegation (works for HTMX-swapped content with no rebinding): `navigator.clipboard.writeText` when
`window.isSecureContext`, else replace the `<code>` text with the full key and select it. Button
shows ✓ for ~1.5 s on success. Links and form values keep the full key — only display text shortens.
Excluded: `admin/revealed.html` private key, `<input>` values.

*Alternative:* keep 8 bytes. Rejected — user asked for 3; the full key is one click/hover away and
3 bytes still exceeds the 1-byte node hash operators already reason with.

### D5 Timestamps: server fallback + `display.js` relative rewrite
Jinja macro `when(dt)` emits
`<time class="when" datetime="{iso utc}" title="{iso utc}">{YYYY-MM-DD HH:MM UTC}</time>`
(`—`/caller text when `None`). `display.js`:
- `render(root)` finds `time.when`, sets text to relative ("just now" <45 s, "n min ago", "n h
  ago", "n d ago" <30 d, else local short date; future "in n min"), `title` to
  `local full date-time\nISO UTC`.
- Runs on `DOMContentLoaded`, on `htmx:afterSwap` for the swapped element, and every 30 s over the
  document.
- Exposes `window.sighopDisplay = {when, relative}` for `feed.js`.

**Feed exception:** the live feed keeps an absolute local time-of-day with milliseconds
(`17:35:56.857`) rather than relative — packets arrive seconds apart and "just now" on every row
would erase the ordering information the feed exists for. Hover gives full date and UTC ISO.
Wire-time ("sender's clock") uses the same `when` macro.

*Alternative:* server-rendered relative text. Rejected — goes stale on non-refreshing pages and
cannot know the viewer's zone.

### D6 Formatting helpers in `render.py`, registered once
`duration(seconds|ms)` and `plural(n, word, plural=None)` in `render.py`; one
`install_display(env)` registers `when`/`key`/`duration`/`plural` as Jinja globals/filters, called
wherever `Jinja2Templates` is built in `app.py` (panel and error page). Macros live in
`templates/_display.html`, imported like `_identity.html`.

### D7 `display.js` is a static file loaded from `base.html`
`<script src="/static/display.js" defer>` beside htmx. Existing `test_web_assets.py` checks it
resolves under `static/`; no vendoring note needed (first-party).

## Risks / Trade-offs

- [Native `title` tooltips do not appear on touch devices] → explanation also in legend/page notes
  and in visually-hidden text; a CSS tooltip can be added later without markup change.
- [HTMX 3 s refresh re-inserts server fallback text, then JS rewrites → brief flicker] → rewrite on
  `htmx:afterSwap` (before settle/paint in practice); if visible, move to `htmx:beforeSwap` on the
  fragment.
- [Glyph renders as colour emoji on some platforms] → U+FE0E on ⚠ and similar; `font-variant-emoji:
  text` in CSS.
- [3-byte keys can collide visually] → display only; every link, form and copy uses the full key;
  full key on hover.
- [Clipboard API blocked on plain-HTTP wider bind] → selection fallback (spec'd).
- [Tests pinned to old prose] → hidden explanation text keeps most substring asserts valid; the
  rest (`"received, 2 hop(s)"`, `"2 attempt(s)"`, `"claimed, unverified"` visible) updated.
- [Overlap with `rename-to-siggynet`] → purely path-level; `window.sighopDisplay` name follows the
  rename if that lands first.

## Migration Plan

Pure presentation; deploy by restart. Rollback = revert the commit. No data, config or URL change.
