# Design

## Context

Four modules build originated packets, and each one hardcodes the width:

- `net/adverts.py` `build_advert_packet`: `hash_size=1`, for both flood and zero-hop.
- `net/channels.py` `build_channel_packet`: `hash_size=1`.
- `net/dm.py` `Route.hash_size` defaults to `1`. `choose_route` returns `Route(flood=True)` when
  flooding is allowed and no route is known. `build_message_packet` and `build_ack_packet`
  encode `route.hash_size`.
- `net/room.py`: `Route(flood=True)` at the login PATH return (~L897), the request PATH return
  (~L1300) and `_route_to` (~L1702). `_encrypted_packet` and `_ack_packet_with_tail` encode
  `route.hash_size`.

Routes learned from `PathStore` carry their own `hash_size`, and those paths are fixed at that
width. `room._route_for_record` rebuilds the *inbound* route only to size an ack timeout, and
never encodes it.

Configuration comes from the environment through `config.Config.from_environment`, and several
CLI commands call it (`keys`, `db`, `web`, `run`). `SIGHOP_SECRET_KEY` sets the idiom: `Config`
holds the raw text and a method validates it where it is needed. `RuntimeConfig` is built from
argparse in `cli._run_config`.

## Goals / Non-Goals

**Goals:**
- One value, read once per run and handed to every originating service.
- A missing call site fails loudly in tests rather than silently emitting 1-byte floods.

**Non-Goals:**
- A CLI flag, a database setting, or live editing on `/system`. The user chose an environment
  setting.
- Per-identity, per-room or per-channel widths.
- Re-encoding learned routes to another width.
- Changing decode, dedup, path learning or the ack-timeout estimate.

## Decisions

**D1 — Raw on `Config`, validated by `Config.path_hash_size()`.** `Config` gains a
`path_hash_size_raw: str | None` field, read from `SIGHOP_PATH_HASH_SIZE` (empty counts as
unset). A method parses it with `parse_path_hash_size(value) -> int`, which returns
`DEFAULT_PATH_HASH_SIZE = 3` for `None` and raises `ConfigError` for anything outside
`{"1","2","3"}` after stripping. The method also reports whether the value came from the
environment, for the startup line.
*Alternative:* validate eagerly in `from_environment`. Rejected because a typo would then also
break `sighop keys secret` and `sighop db migrate`, which never transmit. This follows the same
reasoning as the secret key.

**D2 — Resolved in `cli._run_config`, carried on `RuntimeConfig`.** `_run_config` calls
`Config.from_environment().path_hash_size()`. The `ConfigError` surfaces through the same
path as other run config errors, before the modem opens. `RuntimeConfig` gains
`path_hash_size: int = DEFAULT_PATH_HASH_SIZE` and `path_hash_size_from_env: bool = False`.

**D3 — Builders take a required keyword; services hold a defaulted attribute.**
`build_advert_packet(..., path_hash_size: int)` and
`build_channel_packet(..., path_hash_size: int)` get no default, so every caller has to decide.
`AdvertScheduler`, `ChannelMessenger`, `DirectMessenger` and `RoomServer` each gain
`path_hash_size: int = DEFAULT_PATH_HASH_SIZE`, and `Runtime` passes `config.path_hash_size`
when constructing them. The service defaults keep existing test construction unchanged while
matching the spec'd default.
*Alternative:* a module-level global. Rejected because it hides the dependency and tests would
leak state into each other.

**D4 — Flood routes are built with the width explicitly.** `choose_route` gains a keyword
`path_hash_size`, and its flood branch returns `Route(flood=True, hash_size=path_hash_size)`.
The three `Route(flood=True)` sites in `room.py` pass `self.path_hash_size`. `Route.hash_size`
keeps its dataclass default of `1`, because it also describes learned routes. The flood
constructions are what change. Web and runtime callers of `choose_route` (`runtime.py:910`,
`web/routes/chat.py:647,719`) call with `allow_flood=False`, which never reaches the flood
branch, but they still pass the value so the signature reads the same everywhere. `choose_route`
takes the keyword with no default.

**D5 — The zero-hop advert uses the setting too.** Its path is empty, so the width has no effect
on routing. The spec still covers "every originated packet with an empty path", which keeps the
rule simple and the header consistent with our floods.

**D6 — Startup line.** `render_run_startup` gains `path_hash_size` and `path_hash_size_from_env`
and always prints one line, gate open or closed:
`path hash size 3 bytes (default)` or `path hash size 1 byte (SIGHOP_PATH_HASH_SIZE)`.

## Risks / Trade-offs

- [Old repeaters that predate multi-byte paths may mishandle hash size 2–3 and drop our floods]
  → MeshCore's own `sendFlood` accepts 1–3, and captured traffic already carries 3-byte paths.
  An operator on an older mesh sets `SIGHOP_PATH_HASH_SIZE=1`. The startup line makes the value
  visible.
- [Larger width, fewer hops: a flood holds at most `MAX_PATH_SIZE`/width hops, 64/3 = 21, before
  repeaters stop appending] → Acceptable, and the same trade firmware makes. The existing codec
  limits already enforce it.
- [Default change alters on-air bytes for existing deployments] → Recorded as BREAKING in the
  proposal. Setting the variable to 1 restores the previous bytes exactly (spec scenario).
- [Repeated copies grow: our originated packet is the same size, since its path starts empty, but
  each repeater appends 3 bytes instead of 1] → A few bytes of airtime per hop on other nodes'
  budgets, not on ours. This is the price of fewer collisions.

## Migration Plan

No data migration. Deploys that want the old behaviour add `SIGHOP_PATH_HASH_SIZE=1` to
`./.env` and `.env.dev`. `compose.yaml` passes it through as `${SIGHOP_PATH_HASH_SIZE:-}`, so an
unset variable reaches the process empty and takes the default. Rollback means reverting the
build. Nothing is persisted.
