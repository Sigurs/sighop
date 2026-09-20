# Tasks

## 1. The copy control (D1)

- [x] 1.1 Rewrite the copy path in `static/display.js` as the three tiers of design D1 — Clipboard
      API, off-screen `<textarea>` plus `execCommand`, then an out-of-flow popover — with the
      `<code>` element never written to; verify by reading the file that no branch assigns to
      `code.textContent`
- [x] 1.2 Add the popover markup the third tier needs to the `key()` macro in
      `templates/_display.html` and style it in `panel.css` as `position: absolute` so it takes no
      space in the row; verify the contacts page's column widths are identical before and after a
      copy click
- [x] 1.3 Extend `tests/test_web_display.py`: copying in a secure context copies the full key and
      confirms; copying without a secure context copies the full key and leaves the abbreviation at
      three bytes; the abbreviation returns after the manual-copy popover is dismissed — verify
      `uv run pytest tests/test_web_display.py` passes

## 2. Navigation marks the current page (D2)

- [x] 2.1 Compute `current_nav` in `Panel.context` (`web/deps.py`) from `request.url.path` by
      longest-prefix match over the seven navigation entries; verify a unit test maps `/`,
      `/contacts`, `/chat`, `/chat/<entity>/<peer>` and `/admin/identities/<id>` to the expected entry
- [x] 2.2 Render `aria-current="page"` and a `.current` class on the matching link in
      `templates/base.html`, styled by weight and rule rather than colour in `panel.css`; verify
      `tests/test_web_dashboard.py` asserts exactly one marked link per page and the right one on a
      page beneath chat

## 3. The palette, audited (D3)

- [x] 3.1 Fold the two colours still stated outside the palette (`panel.css` lines 132 and 256) into
      named values on `:root`; verify no colour literal remains outside the `:root` block
- [x] 3.2 Add a test that reads the palette from `panel.css` and computes the WCAG contrast ratio of
      every text colour against every background it is placed on; verify each pair reaches 4.5:1 and
      that the test fails if a token is dimmed
- [x] 3.3 Add a test that no rule outside `:root` states a colour literally, so the palette stays the
      one place a colour is decided; verify `uv run pytest tests/test_web_display.py` passes
- [x] 3.4 Confirm nothing is distinguishable by colour alone — each status, identity marking, budget
      state and TX/RX direction also carries a glyph, a word or a shape; verify by reading the
      markup the macros produce for each

## 4. Phone-width layout (D4)

- [x] 4.1 Wrap every `<table>` in `templates/` in `.table-wrap`; verify a new assertion in
      `tests/test_web_assets.py` that each `<table>` has a `.table-wrap` ancestor passes
- [x] 4.2 Add the `@media (max-width: 40rem)` block — wrapping `.panel-head` and `.nav`, reduced
      panel padding, stacked `.composer` controls; verify at a 360-pixel viewport that no page
      scrolls sideways as a whole and every navigation link is reachable
- [x] 4.3 Check at 360 pixels that nothing present at desktop width is hidden — statuses, notes,
      legends and controls; verify by comparing the contacts, chat and system pages at both widths

## 5. Refresh that keeps the reader's place (D5)

- [x] 5.1 Vendor idiomorph into `static/` and record its version, source URL, SHA-256 and licence in
      `static/VENDORED.md` beside htmx's entry; verify `tests/test_web_assets.py` still passes,
      including its no-external-origin assertion
- [x] 5.2 Load the extension in `base.html` and move `chat/_messages.html` and
      `chat/_channel_messages.html` to `hx-ext="morph"` with `hx-swap="morph:outerHTML"`; verify a
      selection inside a message survives a refresh tick and the view does not jump
- [x] 5.3 Confirm a tick that changes nothing still advances relative timestamps (`display.js` runs
      on `htmx:afterSwap` after the morph settles), and that a delivery state changing from awaiting
      to acknowledged is redrawn; verify with the scenarios added to `tests/test_web_chat.py`

## 6. Composer counter and keyboard send (D6, D7)

- [x] 6.1 Pass the limit — and, for a channel post, the `"<identity name>: "` prefix that rides
      inside it — as data attributes from `routes/chat.py` into the composer; verify the rendered
      attributes match `MAX_TEXT_LEN` and `MAX_CHANNEL_TEXT_LEN` in `tests/test_web_chat.py`
- [x] 6.2 Implement the counter in `display.js` using `TextEncoder`, recomputing when the identity
      select changes, stating by how much the text is over and never disabling the send control;
      verify multi-byte text counts bytes rather than characters
- [x] 6.3 Add the delegated Ctrl+Enter / Meta+Enter `keydown` handler calling `form.requestSubmit()`,
      leaving plain Enter to insert a newline; verify a keyboard send carries the chosen identity and
      the flood checkbox exactly as the send control does
- [x] 6.4 Confirm the composer still submits and a message over the limit is still refused with its
      reason when the script does not run; verify with a script-free request in `tests/test_web_chat.py`

## 7. Chat page decongestion (D8)

- [x] 7.1 Move the three add-a-channel forms in `templates/chat/index.html` inside one `<details>`,
      rendered `open` when `refusal`, `just_added` or `removed` is in the context; verify a refused
      pre-shared key re-shows the page with the reason and the form both visible without interaction
- [x] 7.2 Move the standing notes that duplicate an explanation already available on hover into
      `title`, leaving every statement a spec requires stated as visible text; verify the existing
      `tests/test_web_chat.py` assertions on the guessable statement and the authentication note pass
- [x] 7.3 Style the disclosure in `panel.css` so its summary reads as a control in both palettes and
      at 360 pixels; verify by opening the chat page dark and narrow

## 8. Verification

- [x] 8.1 Run `uv run pytest` and confirm the whole suite passes, including
      `tests/test_web_display.py`, `tests/test_web_chat.py`, `tests/test_web_dashboard.py` and
      `tests/test_web_assets.py`
- [x] 8.2 Run `openspec validate "improve-panel-usability" --strict` and confirm it reports valid
- [x] 8.3 Drive the running panel over plain HTTP (the deployment the copy bug appears in) and walk
      the four scopes: copy a key from contacts, read a conversation while it refreshes, compose past
      the limit and send with Ctrl+Enter, and open every page dark at 360 pixels
