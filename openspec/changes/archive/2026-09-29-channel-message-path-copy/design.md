# Design

## Context

- An inbound channel message is recorded from `RxRecord` in `net/channels.py`
  (`_handle_group_text` → `ChannelMessageRecord(...)`, ~line 720). The reception's `packet.path` (raw
  bytes) and `packet.hash_size` are in hand there; only `hop_count` is copied onto the record.
- Duplicate copies never reach `_handle_group_text` (dedup drops them), but every GRP_TXT reception,
  duplicate or not, reaches the reception observer `ChannelMessenger.observe(record, duplicate)`.
  Today `_observe` uses it only for our own posts: it looks up `content_key(GRP_TXT, payload)` in the
  `RepeatRegistry` (256 entries, 1 h TTL), bumps `repeats_heard` and re-records the post.
- `channel_message` (`db/models.py`) has `hop_count` but no path. Writes are upserts
  (`ON CONFLICT (channel_id, ref) DO UPDATE`) from `_channel_message_values(record)`, so re-recording
  an updated record updates the row in place. `packet_log.path_bytes` exists but is a best-effort
  ring buffer that nothing may depend on — not a source for this.
- The chat page merges stored history with the in-memory `page.channel_log` (`routes/chat.py`), so
  both carry `ChannelMessageRecord`; a new field on the record reaches the view from either.
- The received state is `Status("received", "↓", (hops,), explanation)`; `explanation` is the hover
  and screen-reader text.
- A copy control already exists for keys: `display.key()` emits `button.copy[data-copy]` plus an
  empty `.key-full` holder; `display.js` delegates clicks from the document (so HTMX morph refreshes
  need no rebinding) and falls back clipboard → off-screen textarea → select-by-hand in `.key-full`.

## Goals / Non-Goals

**Goals:**
- Persist every heard copy's path with a received channel message and copy them as
  `hh->hh->…`, one path per line.

**Non-Goals:**
- Resolving hashes to repeater names (hashes collide at 1 byte; that is `webhooks`' `PathHop`
  territory and a separate change).
- Paths of repeats heard for our own posts (they could use the same mechanism later).
- Per-copy SNR/RSSI/time, backfilling existing rows, paths for direct messages / rooms.
- Attaching copies heard after a restart (the in-memory registry is gone).

## Decisions

**D1. Store `paths bytea[]` plus one `path_hash_size smallint`, both nullable.** The hash size is set
by the originator in the path-length byte and repeaters keep it, so every copy of one packet shares
it; one column is enough. Each array element is one copy's raw path (`b""` for zero-hop), in arrival
order. `NULL` means "not recorded" (pre-migration rows, or a reception with no decoded packet).
`ARRAY` already has precedent in `models.py`. Alternatives: a child table
`channel_message_path` — rejected, a join and a second write path for data only ever read with its
message; storing formatted text — rejected, bakes display into the database.

**D2. Remember received messages in a second bounded registry.** A `HeardRegistry` (same shape as
`RepeatRegistry`: `OrderedDict` by `content_key(GRP_TXT, payload)`, capacity 256, TTL 1 h) holds, per
group text, the paths heard so far and — once decrypted — its `ChannelMessageRecord`. The entry is
made by whichever sees the first copy first: the observer's `duplicate=False` call (synchronous, in
`IngressPipeline.ingest`) or `_received` (the bus handler, which runs later). That ordering matters:
a duplicate can be observed before the handler has decrypted the first copy, and its path must wait
in the entry rather than be lost. On `duplicate=True`, if the entry exists and holds fewer than
`MAX_PATHS = 32` paths, append `packet.path`; if the record exists, replace it and call
`self._record(...)` (the existing upsert). `_received` builds the record from the entry's paths so far.
Hop count, SNR and RSSI stay the first copy's. Own posts never enter it — the `RepeatRegistry` branch
returns first. The upsert keeps the longer `paths` array, so a stale offer never takes a copy back.
Alternative — joining duplicates via `packet_log` at render time — rejected: `packet_log` may drop
rows.

**D3. Format in the route, as a pure helper.** `_path_lines(paths, hash_size) -> tuple[str, ...]` next
to `_channel_state` in `routes/chat.py`: each path split into `hash_size`-byte hops,
`"->".join(hop.hex() ...)`, or `"direct"` for `b""`; an element whose length isn't a multiple of
`hash_size` is skipped rather than guessed at. The view's `path_copy` is `"\n".join(lines)` when any
line is not `direct`, else `None` (also `None` for outbound and `NULL` paths). Lowercase hex because
`bytes.hex()` is and keys are shown that way elsewhere. Unit-testable without a page.

**D4. Hover text carries the paths.** `_channel_state` gets the lines and, when a copy control would
be shown, appends to the received explanation: `received over 3 hops; heard 2 times via
a3f1->28c0->9e4b, a3f1->5d02` (comma-separated there, since `title` newlines render inconsistently).
Figures stay the hop count so the column does not widen.

**D5. Reuse the copy mechanism via a new `display.copy_path(text)` macro.** Emits
`<span class="key path-copy"><button type="button" class="copy" data-copy="…" title="copy paths"
aria-label="copy paths">⧉</button><span class="key-full mono" hidden></span></span>` — same classes
`display.js` already looks for. `data-copy` carries the `\n`-joined text; Jinja escapes it into the
attribute and `getAttribute` returns the newlines intact; the off-screen fallback is a `<textarea>`,
which keeps them. The copy-by-hand holder needs `white-space: pre` for the lines to show — the one CSS
addition. JS changes only in its header comment (job 2 now covers paths).

**D6. Migration `0015_channel_message_paths`**: add both columns nullable; downgrade drops them. No
constraints (D3 tolerates bad elements).

## Risks / Trade-offs

- [One DB write per duplicate copy] → bounded by 32 paths per message and the registry's size/TTL;
  channel traffic is already one write per message and per repeat of a post.
- [Hash size assumed constant across copies] → it lives in the originator-set path-length byte; if a
  copy ever disagreed, D3 skips the element rather than mis-splitting it.
- [1-byte hashes are ambiguous] → we copy hashes, never names; resolving is a non-goal.
- [Copies heard after restart or after the 1 h TTL are lost] → stated in the spec; flooding copies
  arrive within seconds.

## Migration Plan

Deploy runs `alembic upgrade head` as usual; existing rows stay `NULL` and show no control. Rollback:
`alembic downgrade -1` drops the columns; older code never reads them.
