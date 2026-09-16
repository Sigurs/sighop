## Context

Motivation: see proposal.md. Requirements: see `specs/webhooks/spec.md`.

Current state:
- `WebhookEvent` (`src/sighop/webhooks/events.py`) derives `node_hash` as `public_key[0]`; both
  renderers print it as two hex digits. The event carries `hop_count` but not the path or its
  hash size.
- `RxRecord` already exposes `hash_size` and `path`, and `Packet.hops` splits the path. MeshCore
  sets a flood packet's hash size from the sender's `path_hash_mode + 1`; each repeater appends its
  own key prefix at that size (`Mesh.cpp:349`). `sendZeroHop` writes `path_len = 0`, so a zero-hop
  advert declares 1 byte whatever the sender's setting.
- `WebhookDispatcher.on_observation` builds the event synchronously in the contact-store listener
  and has no access to the store. `ContactStore.by_node_hash(int)` indexes by first key byte only.
- The Discord `Public key` field shows `public_key.hex()[:16]` followed by `…` (inline field); JSON
  already carries the full key.
- `WebhookEvent.position` is set from the raising advert's appdata (`AdvertAppData.latitude_degrees`
  / `longitude_degrees`, each the wire integer over `GEO_SCALE = 1_000_000`, so 1e-6° of precision)
  and reaches JSON as `node.position` only; the Discord embed never shows it. The embed does show
  an `SNR` field, which no operator acts on from a chat message.
- DESIGN.md §Webhooks states "fields are added in a later schema version, never removed or
  repurposed within one", and `render.py`'s docstring repeats it. The operator chose additive fields
  within schema 1 for this change, which revises that stated convention.

## Goals / Non-Goals

**Goals:**
- Resolve the path once, when the event is raised, so every retry and every webhook sees the same
  names (same rule as `event_id`).
- Keep the listener synchronous, non-raising and I/O-free.
- No change to existing JSON field meanings.

**Non-Goals:**
- Inferring a node's real hash size for zero-hop adverts (from stored history, later floods, etc.).
- A map provider the operator can choose, or a rendered map image.
- Removing SNR from the `json` format, or from anything outside webhooks (monitor, run output).
- Contact share links (`meshcore://contact/add?…`) or QR images — dropped by the operator.
- Matching hops against sighop's own local identities.
- Changing `as_log_fields` (logs keep the 1-byte hash).
- Web panel or CLI changes.

## Decisions

### D1. Event carries resolved data, not a store reference
`WebhookEvent` gains `hash_size: int` and `path: tuple[PathHop, ...]`, where
`PathHop(hash: bytes, name: str | None, matches: int, key_prefix: str | None)` is frozen.
`key_prefix` is set only for a single unnamed match, so the renderer can label it without the
store. A `sized_hash` property (`public_key[:hash_size].hex()`) and a `label` on
`PathHop` (`name` / key prefix / `<unknown>` / `<ambiguous>`) keep rendering pure.
*Alternative:* pass the store into renderers — rejected: renderers are pure, golden-tested, and a
retry minutes later could name different contacts.

### D2. The dispatcher takes an optional hop resolver
`WebhookDispatcher(..., contacts: HopLookup | None = None)` where `HopLookup` is a `Protocol` with
`by_prefix(prefix: bytes) -> frozenset[Contact]`. `Runtime._build_webhooks` passes `self.contacts`.
`event_from_observation(observation, record, trigger, now, contacts=None)` resolves each hop,
excluding the advertising contact itself (a node never repeats its own advert, and counting it
would turn a 1-byte collision with the advertiser into a false `<ambiguous>`). With no lookup every
hop is `<unknown>` — existing tests keep working unchanged.
*Alternative:* resolve inside `ContactStore._report` and put the result on `ContactObservation` —
rejected: widens a shared type for one listener.

### D3. `ContactStore.by_prefix`
`by_prefix(prefix)` returns `frozenset(c for c in by_node_hash(prefix[0]) if c.public_key.startswith(prefix))`;
empty prefix returns empty. Uses the existing first-byte index, so cost is one small set scan per
hop (≤ 64 hops). Keeps the "always a set" contract of `by_node_hash`.

### D4. Hash size source
`hash_size = record.hash_size or 1`. No special case for zero-hop: the decoder already yields 1 for a
zero path length byte, and a flood advert heard with 0 hops keeps its declared size.

### D5. Discord rendering
- `Node hash` field value: `sized_hash`.
- `Public key` field: full `public_key.hex()` in inline code, changed to not inline — 64 characters
  wrapped into a third-width inline column would break mid-key. Hex contains no backtick, so inline
  code cannot be broken out of. `KEY_PREFIX_LENGTH` goes away.
- Unnamed-node description: identified by `sized_hash` and the full key (no ellipsis).
- New `Path` field (not inline), value like ``` `c3d4` Hilltop → `e5f6` <unknown> ```; hop hashes in
  inline code, names through `escape_discord`; our fixed labels (`<unknown>`, `<ambiguous>`) are not
  escaped — Discord treats `<…>` specially only for mention/channel/emoji/timestamp/URL forms, and
  mentions are disabled regardless. Empty path → `heard directly`. Built hop by hop and cut at
  1024 characters (Discord's field limit) ending with `…`, never mid-hop.
- New `Location` field (inline), value a masked link
  `[59.329460, 18.068580](https://www.google.com/maps/search/?api=1&query=59.329460,18.068580)`, or
  `not advertised` when the event has no position. The label is ~21 characters, so it fits a
  third-width column without wrapping mid-value; only the label is displayed, so the URL's length
  does not matter. The field is always present, like the `SNR` field it replaces in the grid: a
  fixed field set keeps the embed's shape the same for every sighting.
- The `SNR` field goes away (D8).
- Field order: `Name`, `Type`, `Node hash`, `Hops`, `Location` (inline), then `Public key`, then
  `Path`.

### D6. JSON additive fields
`node.hash` (sized hex), `node.hash_size`; `reception.path` as
`[{"hash": "c3d4", "name": "Hilltop" | null, "matches": 1}]`; `node.position.map_url` alongside the
existing `latitude` and `longitude`, absent as a whole when `position` is `null`. `node_hash` and
`reception.snr_db` unchanged — schema 1 forbids removing a field, and a machine consumer is exactly
who SNR is for. Update
`render.py` docstring and DESIGN.md §Webhooks to "fields may be added within a schema version;
never removed or repurposed".

### D7. Sample event
`sample_event` uses `hash_size=2`, a path of `c3d4` named `dev-hop` (1 match) and `e5f6`
(0 matches), so a test message exercises both labels. `hop_count` becomes 2 to match the path. It
also carries a sample position, so `sighop webhook test` exercises the maps link — the one field an
operator has to click to check.

### D8. The maps link and the SNR field
One helper in `render.py`, `maps_url(position) -> str`, formats
`https://www.google.com/maps/search/?api=1&query={lat},{lon}` — Google's documented URL scheme,
which opens the app on a phone and the web map elsewhere, and needs no key or account. Both formats
call it, so a URL never differs between them. Coordinates are formatted to 6 decimals
(`f"{value:.6f}"`), the wire precision: fixed-point avoids a float repr like `5.9e-05` in a URL, and
the trailing zeros of a round value cost nothing. Coordinates are numbers sighop formats, not advert
text, so they are not passed through `escape_discord`; the masked-link syntax around them is ours.
*Alternative:* OpenStreetMap or a geo: URI — rejected: the operator asked for Google Maps, and a
`geo:` URI is not clickable in Discord on desktop.

SNR leaves the Discord embed in the same change because the field it frees is the one `Location`
takes: the embed keeps five inline fields and its current shape. SNR stays in `json` (D6) and
everywhere outside webhooks.

## Risks / Trade-offs

- [Zero-hop adverts still show a 1-byte hash, the case most likely for nearby nodes] → Documented
  in spec; the packet carries nothing better. A later change could fall back to a hash size learned
  from the same node's floods.
- [A hop resolves to a name that is stale or a coincidental prefix match with a 1-byte hop] →
  `matches` count exposed in JSON; ambiguity shown explicitly; single 1-byte matches remain
  best-effort, same as elsewhere in sighop.
- [Discord receivers that parsed the old `ab01…` key-prefix field value] → accepted; Discord embeds
  are for people, and JSON is the machine format.
- [Long paths with long names exceed field limits] → truncated at 1024 chars; embed total stays
  well under Discord's 6000.
- [JSON consumers with strict schemas reject unknown fields] → accepted by operator; noted in
  DESIGN.md convention change.
- [A Discord reader who used the SNR field loses it] → accepted; the link quality of a first
  sighting is a machine concern and stays in `json`.
- [An advert's coordinates are self-declared and may be wrong, zeroed or spoofed] → shown as heard,
  labelled with the coordinates so an operator sees `0.000000, 0.000000` for what it is; sighop
  neither validates nor hides them, the same as the advertised name.
- [A maps link sends the coordinates to Google when an operator clicks it] → the operator chose the
  provider; nothing is fetched by sighop, and the link is only rendered.
