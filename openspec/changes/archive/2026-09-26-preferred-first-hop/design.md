# Design

## Context

Routes are learned in `net/paths.py` by reversing what we receive, and resolved
most-recently-confirmed-wins. Four places turn a learned route into a DIRECT send, each with its own
copy of "public key first, node hash second, ambiguous":

- `net/dm.py` `choose_route` — direct messages and their acknowledgements;
- `net/collect.py` `_route` — repeater polls, via `choose_route`;
- `net/room.py` `_known_route_to` — room-server sends to a member;
- `web/render.py` `_route_for` — the contact table, which deliberately mirrors `choose_route`.

Learning is receive-side only. On this node the receiver is fine and the transmitter is not, so a
route that proves a peer reached us says nothing about us reaching it. The operator has a nearby
repeater with a strong antenna and wants every DIRECT send to leave through it. Chosen semantics
(from the operator): prefer candidates already through it, else **prepend it always**. Configured
on the system page, DB-backed, live.

Station-wide settings already follow a pattern: `repeater_collection` is a one-row table
(`id = 1`, absence = defaults) with a repository in `db/repositories.py`, a form on
`web/routes/system.py`, and refusal-with-reason. Migrations `0012` and `0013` belong to in-flight
changes; this one takes `0014`.

## Goals / Non-Goals

**Goals:**
- One preference, applied identically by every sender and by the contact table.
- Byte-identical behaviour with no preference set.
- Lookup stays in memory; the database is never on the send path.

**Non-Goals:**
- Changing what is learned or stored. Candidates, persistence and eviction are untouched.
- Floods. The preferred repeater already repeats our floods when it hears them; a flood has no chosen
  path to prefix. (`TRANSPORT_FLOOD` scoping is a different feature.)
- Packets whose path comes from a received frame (room `_route_for_record`, the echoed path in a
  `PATH` return body). Those describe someone else's route.
- Several preferred repeaters, per-identity preference, or scoring by SNR.
- Verifying that the preferred repeater hears the other first hops. Prepending assumes it; that is
  the operator's call (see Risks).

## Decisions

### D1 — Preference lives on the shared `PathStore`, resolution is one function

`PathStore` gains `preferred_first_hop: bytes | None` (a public key) with a setter, plus a
`route_counters` field for fallbacks. A new `PathStore.resolve(key) -> ResolvedPath | None` does
the selection and rewrite and returns the learned path with `path`, `hash_size`, `hop_count` and a
`rewrite: RouteRewrite` (`NONE`, `PREFERRED` — candidate already started with it, `PREPENDED`,
`SHORTENED`). `lookup()` is left as-is — it still means "most recently confirmed", which
`path-learning` and existing tests rely on.

A small helper `resolve_for_contact(paths, public_key, node_hash) -> (ResolvedPath, ambiguous) | None`
replaces the four duplicated public-key-then-node-hash blocks. `choose_route`, `_known_route_to`
and `_route_for` all call it, so the table cannot drift from what is sent.

*Alternative:* pass the preference into each caller. Rejected — four call sites, all wired through
runtime, and the store is already the one shared object every sender holds. Setting an attribute on
it is also the whole of "apply without restart".

### D2 — Resolution rules, in order

Given candidates `C` for the destination and preferred key `P`:

1. `P` unset → most recently confirmed, `NONE`.
2. Destination's public key is `P` (or, for a hash-keyed lookup, its node hash equals `P[0]` and
   the key is ambiguous) → most recently confirmed, `NONE`. Never route to R through R.
3. Any `c ∈ C` whose first hop equals `P[:c.hash_size]` → newest such, `PREFERRED`.
4. Otherwise take newest `c`. If `P[:w]` occurs at hop index `k > 0` → path from hop `k`,
   hop count reduced by `k`, `SHORTENED`.
5. Else prepend `P[:w]`, hop count + 1, `PREPENDED` — unless the result exceeds `MAX_PATH_SIZE`
   (64 bytes) or 63 hops, in which case the unrewritten `c`, `NONE`, and `prepend_overflow += 1`.

Hop comparison is per hop at the candidate's own width (`reverse_path` already treats hops this
way); a byte search would match across hop boundaries.

### D3 — `Route` carries the rewrite for reporting

`net/dm.py` `Route` gains `rewrite: RouteRewrite = NONE`. `label` appends `" via-pref"` for
`PREPENDED`/`SHORTENED` (e.g. `DIRECT h1 via-pref`). `ack_timeout_ms` needs no change — it already
uses `route.hop_count`, which is now the sent count. Structured logs gain a `route_rewrite` field.
`RouteView` gains the same flag for the contact table, rendered with the existing hop glyph plus a
marker styled like other annotations (`web-display` conventions).

### D4 — Storage: one-row `route_preference` table

```
route_preference(id smallint pk default 1 check id = 1,
                 preferred_first_hop bytea null check octet_length = 32,
                 updated_at timestamptz not null)
```

No foreign key to `contact`: like `repeater_target`, an orphan is harmless and an advert upsert must
never clear it. Absence of the row = none set. `RoutePreferenceRepository` with `get()` and
`save(public_key | None)` returning `Outcome`, in `db/repositories.py`, hung on `Persistence`.
Migration `0014_route_preference.py`.

### D5 — Load and apply

- Startup: runtime reads the row after paths are restored and before the scheduler accepts traffic,
  and sets `pipeline.paths.preferred_first_hop`. A read failure is fatal like any other startup
  database failure (the DB is required).
- Save: `POST /system/route-preference` validates the key is a known contact with node type
  repeater, writes the row, and **only on success** sets the attribute on the live store. A failed
  write leaves memory unchanged so memory and DB never disagree.
- Replay: no DB, no preference; replay does not transmit.

### D6 — System page section

A select of repeater contacts (name + 3-byte key), "none" first. Below the form: the repeater in
force, and the result of `paths.lookup_public_key(P)` — if its newest route is zero-hop, show
"heard directly, confirmed <time>, SNR x"; otherwise the warning. The counters (`prepend_overflow`)
are added to the existing path-store JSON (`PathStore.as_json`) shown on the system page.

## Risks / Trade-offs

- **The repeater may not hear the next hop.** Prepending `R` to `[A, B]` assumes `R ↔ A` works. For
  zero-hop peers this is the usual case (R has the better antenna and is next to us); for multi-hop
  routes it is a bet. → Rule 3 prefers learned routes through R, which are proven; the operator
  chose "prepend always" knowing this; clearing the setting restores learned routing instantly.
- **Extra hop, extra airtime.** Every zero-hop send gains a hop and a retransmission, and a longer
  ack timeout. → Inherent in the feature; shown in the label.
- **1-byte hash collision.** At width 1, another repeater may share `P[0]`; a candidate starting with
  it counts as "through R" and any repeater with that hash will forward. → Same ambiguity the mesh
  already lives with; widths 2–3 (default 3) make it rare.
- **Preferred repeater goes offline.** Every DIRECT send fails until retries exhaust. → The system
  page warning reflects recency of the zero-hop route; clearing the setting is one click. No
  automatic fallback — silently rerouting would hide the failure.

## Migration Plan

Additive migration `0014`; absence of the row is the old behaviour. Rollback: downgrade drops the
table; code without the feature ignores it.

## Open Questions

None blocking. A later change could add automatic fallback to the learned route on the final DM
retry if the preferred repeater proves unreliable in practice.
