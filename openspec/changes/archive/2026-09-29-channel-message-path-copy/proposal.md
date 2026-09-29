# Proposal

## Why

A received channel message's state in `/chat/channel/<id>` shows how many hops it travelled (`↓ 3`),
but not which repeaters it came through, nor that the same message often reaches the station more
than once by different routes. To work out who relayed a message, an operator has to find the
receptions in the packet feed, which is best-effort and may already have dropped them. Every copy's
path is known when that copy is received; none of them is kept with the message.

## What Changes

- A received channel message's record keeps the path of every copy the station hears, in the order
  they arrived: the first copy (the one the message is recorded from) and each duplicate copy heard
  afterwards, up to a bound.
- The channel conversation shows a copy control beside a received message's hop count. Copying gives
  all recorded paths, one per line, each written as its hop hashes in lowercase hex joined by `->` —
  e.g.

  ```
  a3f1->28c0->9e4b
  a3f1->5d02
  ```

  A copy that reached the station directly (zero hops) is written as `direct`.
- Hovering the received state also shows the paths, and how many copies were heard.
- Messages with nothing to copy show no control: posts from this station, messages whose every
  recorded copy was zero-hop, and messages recorded before this change (no paths stored).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `channel-history`: a received message's record holds the paths of every copy heard, and is
  updated in place as duplicates arrive.
- `web-chat`: a received channel message's paths are shown on hover and can be copied, one per line,
  hops joined by `->`.

## Impact

- `channel_message` table: new nullable `paths` (`bytea[]`) and `path_hash_size` columns; Alembic
  migration `0015`. Existing rows keep `NULL` (no backfill — `packet_log` is not an audit trail and
  cannot be relied on for it).
- `src/sighop/net/channels.py`: `ChannelMessageRecord` gains the paths; received messages are
  remembered for a bounded time so duplicate copies can be attached to them, as posts already are for
  repeat counting.
- `src/sighop/db/models.py`, `src/sighop/db/repositories.py`: store and read the columns.
- `src/sighop/web/routes/chat.py`, `src/sighop/web/templates/chat/_channel_messages.html`,
  `src/sighop/web/templates/_display.html`, `src/sighop/web/static/display.js`: format the paths and
  reuse the existing copy control (clipboard, off-screen fallback, copy-by-hand fallback).
- No change to what is transmitted, to direct messages, rooms, webhooks or the monitor.
