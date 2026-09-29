# Design

## Context

`_path_lines` in `src/sighop/web/routes/chat.py` splits each recorded path
(`ChannelMessageRecord.paths`, `path_hash_size`) into hex hops joined by `->`; the same
lines feed both the copy control (`data-copy`, newline-joined) and the received-state hover
(comma-joined). The Discord webhook already resolves hops: `resolve_hop` in
`src/sighop/webhooks/events.py` looks a hop up with `HopLookup.by_prefix` (satisfied by
`ContactStore`), and `PathHop.label` picks name / `<unknown>` / `<ambiguous>` / key prefix.
`render.PATH_SEPARATOR` is ` → `. The chat routes reach the contact store as
`page.state.contacts`.

## Goals / Non-Goals

**Goals:**
- Copy lines in the Discord hop style, resolved from live contacts at render time.
- One shared resolution rule for webhooks and chat, so the two never disagree on a label.

**Non-Goals:**
- Storing resolved names on `channel_message` (names change; the store is the truth).
- Names in the hover — kept to hashes so the title stays short.
- Excluding the channel sender from candidates — its name is an unauthenticated claim with
  no key, so there is nothing to exclude.
- Changing Discord/JSON webhook output.

## Decisions

1. **Reuse `resolve_hop` / `PathHop`, make the advertiser optional.**
   `resolve_hop(hop, contacts, advertiser: bytes | None = None)`; `None` excludes nobody.
   Alternative — a chat-local resolver copying the label rules — rejected: two copies of the
   `<unknown>`/`<ambiguous>`/key-prefix rules would drift. Importing from `sighop.webhooks`
   into the web layer is acceptable (web already depends on runtime modules); if that
   dependency feels wrong at apply time, move `PathHop`/`resolve_hop`/the label constants to
   a neutral module (e.g. `sighop.net.paths`) and re-export from `webhooks.events`.

2. **Plain-text hop form `"{hash.hex()} {label}"`, joined by ` → `** (reuse
   `render.PATH_SEPARATOR` or an equal constant). No Discord backticks or markdown escaping:
   the text goes to the clipboard, and Jinja autoescaping already guards the `data-copy`
   attribute. Control characters in a name are flattened to a space (same rule as
   `escape_discord`'s flattening, without its markdown escape) so a name cannot break the
   one-path-per-line format.

3. **Split lines into two forms from one parse.** `_path_lines` returns hop tuples
   (or `None` for direct); the copy text maps hops to `hash label`, the hover maps them to
   `hash`, both joined by ` → `. The "hide control when all direct" rule keeps working on the
   parsed form, not on string comparison with `direct`.

4. **Resolve at render time, per request.** `_channel_message_view` already has `page`;
   pass `page.state.contacts` down. `by_prefix` is a dict lookup plus a filter over one
   node-hash bucket; ≤32 paths × ≤64 hops per message is negligible.

## Risks / Trade-offs

- [Copy format change breaks an operator's own paste-parsing] → Accepted; format was
  introduced today (channel-message-path-copy) and the user asked for this form.
- [A name that looks like `→` or contains a newline confuses a reader] → Controls flattened;
  arrows in names are left as-is (the hash before each label still delimits hops).
- [Web layer importing from webhooks] → Fallback in Decision 1.
