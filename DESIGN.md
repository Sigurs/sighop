# sighop — Design

A virtualization platform for MeshCore. One physical LoRa modem in KISS mode hosts any
number of virtual MeshCore entities — room servers, companions, and bots — each with its
own identity, keys, and state.

Status: design. No code yet. This document is the contract the implementation follows;
when reality disagrees with it, update this file in the same change.

---

## 1. The central constraint

The [MeshCore KISS modem firmware](https://docs.meshcore.io/kiss_modem_protocol/) is a dumb
TNC. It gives raw LoRa frames in and out with **no mesh routing and no encryption** — that
is explicitly its design purpose. It is not a MeshCore node we drive; it is a radio.

Therefore **sighop implements the MeshCore protocol stack itself, in Python.** That is the
bulk of this project. The room server and the bots are comparatively thin layers on top.

This is tractable because the protocol is publicly documented:

- [Packet format](https://docs.meshcore.io/packet_format/) — header, transport codes, path, payload
- [Payload formats](https://docs.meshcore.io/payloads/) — ADVERT, TXT_MSG, ACK, REQ/RESPONSE, ANON_REQ, GRP_TXT, PATH
- [Cryptography walkthrough](https://jacksbrain.com/2026/01/a-hitchhiker-s-guide-to-meshcore-cryptography/) — the piece the official docs omit

The modem's `SetHardware` crypto sub-commands (`GetIdentity`, `SignData`, `KeyExchange`)
are **unusable to us**: they are bound to the modem's single hardware identity, and the
entire point of sighop is hosting N identities. All cryptography happens in Python.

### What the modem does give us

| Capability | Use |
|---|---|
| `Data` (0x00) frames, ≤255 B | TX and RX of raw MeshCore packets |
| `RxMeta` (0xF9) after each RX | SNR (signed, ×4 for 0.25 dB) and RSSI (signed, dBm) |
| `TxDone` (0xF8) | TX completion + success/failure — **the gate for our scheduler** |
| `Error` (0xF1) + `TxBusy` (0x07) | Rejection when a TX is already pending |
| `SetRadio`, `SetTxPower`, `GetRadio` | Frequency, BW, SF, CR, power |
| `GetAirtime`, `IsChannelBusy`, `GetNoiseFloor`, `GetCurrentRssi` | Channel telemetry for the scheduler |
| `GetBattery`, `GetMCUTemp`, `GetVersion`, `GetStats` | Health surface for the WebUI |

**Only one packet may be pending transmission at a time.** This single fact drives the
whole outgoing-path design (§4.3).

---

## 2. Decisions taken

| Decision | Choice | Why |
|---|---|---|
| Repeating | **No.** sighop does not forward others' traffic. | N virtual repeaters at one antenna are the same RF point — meaningless. Forwarding would also consume the airtime budget the room servers need. Revisit only if a deployment is the sole link between segments. |
| Radio attachment | **Direct device passthrough** (`/dev/ttyUSB0` into the container). | Chosen for operational simplicity. See §10 for how this coexists with the arbitrary-UID goal. |
| Transport abstraction | Serial is behind a `KissTransport` interface anyway. | A TCP implementation (ser2net) is ~40 lines if the deployment ever needs it. Costs nothing now. |
| Concurrency | Single asyncio process. | The radio is a single serialized resource; there is nothing to parallelize below it. Postgres and the web server are the only real I/O fan-out. |
| DB access | SQLAlchemy 2.0 async + asyncpg, Alembic migrations. | Spec requires ORM + Alembic. **Re-confirmed against the live development database at milestone 5**, not assumed: `.env.dev` had arrived saying `postgresql+psycopg`, and the URL was rewritten to match this row rather than the row rewritten to match the URL. The cost is paid once — asyncpg has no synchronous mode, so `alembic/env.py` bridges through `connection.run_sync`, about fifteen lines. asyncpg also does not read libpq's query parameters (`?sslmode=`, `?options=`), so `config.py` rejects them at startup rather than letting them surface as a driver error at the first TLS-terminating deployment. |
| Web | FastAPI + server-rendered HTML with HTMX, WebSocket for live feeds. | Avoids a separate SPA build in the container image. |
| Logging | structlog, JSON, wide events. | Spec requirement; see §11. |
| Packaging | uv with a locked `uv.lock`. | Spec requirement. |
| Region | Fully configurable; **default EU/UK 868 "narrow"** (869.618 MHz, BW 62.5 kHz, SF8, CR8 — confirmed against [meshcore.ch/settings](https://www.meshcore.ch/settings/), the "default for pretty much all of Europe and the UK"; 869.525 MHz is the firmware's unrelated *legacy* default frequency, paired with the old 250 kHz/SF11 preset). | Target deployment. The 10% duty-cycle ceiling is treated as a hard limit (§4.3). |
| Scale target | **2–5 virtual entities.** | Sets how much machinery the airtime budget justifies. Explicitly not designing for 20+. |
| UI direction | Instrument panel / radio-ops. | See §8. |
| Transmit | **Receive-only mode is a first-class setting, and the default on a fresh install.** | We develop against a live mesh with real users on it. Nothing transmits until an operator deliberately enables it. |
| Modem hardware | **Heltec WiFi LoRa 32 V3 or V4** — either, interchangeably. | Both are ESP32-S3 + SX1262 running the same board-agnostic KISS example; time-on-air is identical, so nothing above the transport can tell them apart. §4.1 names the four seams where the board is visible, all probe-detected. Capture on the V3 to date. |
| Test peer | A second dedicated board. | Repeatable, automatable testing at milestone 4 without involving the live mesh's real users. The spare V4 fills this: the peer needs the full companion firmware, where its proper board definition, 16 MB flash and OLED support are genuinely better. |
| Test entity naming | **Every test room, companion, bot, identity and other entity is named with a `dev-` prefix, and existing ones are reused when possible** rather than a fresh one created per exercise. | Test entities advertise on a live mesh with real users; the prefix makes them recognisable as ours at a glance, in their contact lists and in our own database. Reuse keeps contact lists and the database from filling with near-duplicates, and keeps each test identity's history in one place. |
| Radio count | **One radio. No separate RX and TX devices.** | Co-sited transmitter and receiver on the same frequency is one radio destroying another, not two independent radios (§4.3). The second board is better spent as the test peer. A second radio on a *different* preset or band is a separate question, left open by the multi-modem non-goal. |
| Deleting and renaming in the panel | **A room, a bot and a stored identity can each be deleted in the browser, and identities, rooms, channels and webhooks renamed.** Removing an identity is the one that asks for a password *and* the identity's name typed out; deleting a room or a bot is nonce-only. Renaming is neither. | Three tiers, matched to what is destroyed rather than to how alarming the verb sounds. Removing an identity destroys key material irrecoverably, which is the tier `reveal` and `export` are already in. A room or bot destroys stored content — `remove_channel`'s tier. A rename is reversible by renaming back and destroys nothing, so guarding it would only train operators to click through confirmations. Rooms and bots gained `delete` on the command line at the same time, so the surface parity `web-admin` requires stays true. |
| A live rename reaches the air | **Renaming a loaded identity mutates the one shared `EntityStub` in place**, and the keystore's frozen `LocalEntity` is replaced beside it. | `runtime.py` hands `adverts.stubs` to the direct messenger, the channel messenger, the room servers and the bot host, so those are all the *same objects*: one assignment reaches the next advert, the next channel post's sender name and every log line at once. Re-registering instead would reset the flood schedule and be refused for colliding with itself. `entity_id` moves with the name because `_adopt_entity` derived it from the name; the consequence is that an advert confirmation minted against the old id is refused, which is the safe direction. Decoupling `entity_id` from the name is the better long-term shape and is deliberately not done here. |
| Stored identities, rooms and bots apply without a restart | **`Runtime.reconcile_entities()` diffs the store's openable set against `adverts.stubs` by public key** — adopt, withdraw, re-configure — under an `asyncio.Lock`, then runs `reconcile_rooms()` and `reconcile_bots()` in the same pass. The panel calls it right after a write for immediacy; a 60 s `_entity_refresh_loop` (the same interval and shape as `_channel_refresh_loop`) picks up a change from another process. A collision on adoption is refused and remembered — reported once, cleared when the identity it collided with is withdrawn — rather than raised: `EntityRegistry._register` and `AdvertScheduler.add_identity` keep their raising form for startup, where a collision must still be fatal, but a run on the air must not be ended by a write another process made to the store. | Change `apply-store-changes-without-restart`. Before this, only a rename and a deletion of each object were live — added at different times for different reasons — and everything else on the same three objects (`create`, `enable`, `disable` an identity; `create` a room or a bot) needed a restart, which is what made an identity created through the panel invisible to the run that served the page. One reconcile shared by both callers, rather than a create-hook per surface, is deliberate: a hook gives the panel path a code path the periodic path lacks, which is exactly how the rename/delete asymmetry happened in the first place. Withdrawal order is bot, then room, then the stub, then the registry entry, so each attachment still has something to release when its turn comes. Fixed as a prerequisite: `PathBodyReader.__post_init__` used to copy its `entities` argument into a fresh list on construction, which broke the deliberate alias to `adverts.stubs` (see the row above) before a room was ever served. It now copies only when the caller did not already hand over a list. `stop_serving_room` and `_serve_room` used to *rebind* `path_bodies.entities` to exclude a room-claimed entity from path-body matching; switching that to an in-place `.remove()`/`.append()` was tried first and was itself a bug — that list **is** `adverts.stubs`, so removing from it stopped a served room's own identity from advertising. `PathBodyReader` instead gained `claim_for_room()`/`release_from_room()`, an exclusion set checked at read time, mirroring `DirectMessenger._room_entity_ids` exactly; neither method touches `entities`. |
| Identity and room names are validated | **`parse_entity_name` and `parse_room_name`, applied by create, import and rename alike.** An identity name is bounded at 23 UTF-8 bytes and may not contain the channel-post separator. | Channels and webhooks already validated their names; identities and rooms validated nothing, so an identity could be created named `""`, or named with the `": "` a channel post puts between sender and text — which then failed at *post* time — or long enough that `build_appdata` raised when it tried to advert. Both were reachable at creation and only failed later, against the radio. 23 bytes is what always fits the 32-byte appdata beside a flags byte and a location. |
| WebUI auth | Built-in session login: accounts in the database, Argon2id, an `HttpOnly; SameSite=Strict` cookie over plain HTTP (milestone 9 — not `Secure`, see §8). | Standalone, no external dependency. The UI holds private keys and can key a transmitter; it does not ship unauthenticated. |

### Explicit non-goals for v1

- Repeating / packet forwarding
- Transport codes (region/sub-region scoping) — parse and preserve them, do not originate
- Multipart payloads (`0x0A`)
- Bridging to other mesh protocols
- Multi-modem support (the architecture leaves room; the scheduler assumes one)

---

## 3. Domain model

A **virtual entity** is an addressable MeshCore node hosted by sighop. Every entity owns:

- an Ed25519 keypair (its identity; its **node hash** is the first byte of the public key),
  held as MeshCore's 64-byte `prv_key` — the clamped SHA-512 expansion the firmware stores,
  not the 32-byte seed behind it. That expansion is the only form an operator can bring in
  from a device, and it is sufficient: its two halves are the signing scalar and the nonce
  prefix, so nothing in sighop needs a seed (`create-entity-with-known-key`)
- a name, advert flags, optional location
- an advert schedule
- type-specific state and configuration

Entity types in v1:

- **Room server** — accepts logins, stores messages durably, syncs history to members
- **Companion** — an addressable chat identity a human drives from the WebUI
- **Bot** — a companion whose behavior is automated by a plugin (greeter first)

A companion and a bot are the same runtime with a different driver: one driven by a human
through the WebUI, one by code. Build them as one type with a pluggable driver rather than
two parallel implementations.

A **channel** is not an entity and belongs to no entity: it is **station state**, a key the
station holds (change `channel-messaging`). `GRP_TXT` has no recipient, and its only "sender" is
a name inside the plaintext, so per-identity membership would model something the wire cannot
express — a handheld's channel slots are a property of the device, and sighop is the device.
Every loaded identity that may chat can read and post in every stored channel, and a channel has
one history, not one per identity. Path knowledge is platform-wide (§4.2) for the same reason.

### The node-hash collision problem

MeshCore addresses packets by a **1-byte** destination hash. With 256 possible values,
collisions are routine on a busy mesh — and sighop makes this worse by hosting many
identities behind one radio.

Consequences the implementation must handle:

1. **Inbound:** a destination hash may match several of our entities. Deliver the packet to
   *every* matching entity; each attempts MAC verification and only the true recipient
   succeeds. This is exactly how MeshCore itself disambiguates.
2. **The 2-byte MAC is weak.** A false MAC match is ~1 in 2^16 per candidate key. Combined
   with the 1-byte hash the practical false-positive rate is acceptable, but the code must
   never treat a MAC match as proof of anything security-critical.
3. **Key generation:** when creating an entity, reject a keypair whose node hash collides
   with an existing local entity. Regenerate. Cheap, and it removes self-inflicted ambiguity.
   A key the operator *supplies* cannot be regenerated, so the same rule becomes a refusal
   there — along with the `0x00`/`0xFF` prefixes `Identity.cpp::validatePrivateKey` rejects.

---

## 4. Architecture

```
                        ┌──────────────────────────────────────────┐
   /dev/ttyUSB0 ◄──────►│  KissTransport   (framing, escaping)     │
                        └───────────────┬──────────────────────────┘
                                        │ frames + RxMeta
                        ┌───────────────▼──────────────────────────┐
                        │  Modem            (SetHardware, TxDone)  │
                        └───┬──────────────────────────────▲───────┘
                            │ RxPacket                     │ one at a time
            ┌───────────────▼──────────┐      ┌────────────┴───────────────┐
            │  RX pipeline             │      │  TX scheduler              │
            │  · decode                │      │  · priority classes        │
            │  · dup cache (shared)    │      │  · airtime budget          │
            │  · path learning         │      │  · advert jitter           │
            └───────────────┬──────────┘      │  · retry/backoff           │
                            │                 └────────────▲───────────────┘
                        ┌───▼─────────────────────────────┬┴───┐
                        │        Virtual network bus      │    │
                        │   fan-out RX          submit TX │    │
                        └───┬─────────────┬─────────────┬─┘    │
                            │             │             │      │
                     ┌──────▼─────┐ ┌─────▼──────┐ ┌────▼──────────┐
                     │ RoomServer │ │ Companion  │ │ Bot (greeter) │
                     └──────┬─────┘ └─────┬──────┘ └────┬──────────┘
                            └─────────────┼─────────────┘
                                   ┌──────▼──────┐   ┌──────────────┐
                                   │  Postgres   │   │  FastAPI/UI  │
                                   └─────────────┘   └──────────────┘
```

### 4.1 KISS transport and modem

`KissTransport` handles FEND/FESC framing and escaping over a 115200 8N1 link, and nothing
else. `Modem` sits above it and owns the semantics: correlating `RxMeta` with the data
frame it follows, matching `SetHardware` responses (`response_code = request_code | 0x80`),
surfacing `TxDone`, and translating `Error`/`TxBusy`.

Reconnection matters: USB serial adapters disappear. The modem layer owns a reconnect loop
with backoff, and re-applies radio configuration on every reconnect — never assume the
device came back configured.

### Modem board: Heltec V3 or V4

**Either board works, and sighop must not care which.** Verified against the MeshCore
source (`companion-v1.17.1`): `KissModem` is constructed from abstract `Stream&`,
`mesh::Radio&`, `mesh::MainBoard&` and `SensorManager&` references, there is not one
per-board `#ifdef` in `KissModem.cpp`, and the same `examples/kiss_modem/` source compiles
for every variant behind a single `KISS_FIRMWARE_VERSION`. Both boards are ESP32-S3 +
SX1262 driven through the same RadioLib, so **time-on-air is identical between them** —
which is what §4.3's duty-cycle enforcement rests on.

The board is observable through exactly four seams, none of them structural:

1. `GetDeviceName` (0x16) returns a different string — and on the V4 it is chosen at
   *runtime* from the detected PA chip (`"Heltec V4 OLED"` vs `"Heltec V4.3 OLED"`). Device
   name is a probe result, never a per-firmware constant.
2. `GetSensors` (0x15) returns a CayenneLPP buffer whose *shape* follows build flags — the
   V4 base env sets `ENV_INCLUDE_GPS=1`, the V3's does not. Parse it dynamically; never
   against a fixed schema.
3. `SetTxPower` differs in meaning: the V4 drives an external PA (GC1109 / KCT8103L) with a
   roughly +12 dB offset, so the value we set is not the value radiated. Never render the
   logged figure as absolute dBm without the device name beside it.
4. RX sensitivity may differ — the V4 base env carries `SX126X_REGISTER_PATCH=1` ("improved
   RX") which the V3 lacks. This changes what arrives, not how we parse it.

**No firmware debug output, on either board.** The KISS firmware is silent by construction:
`KissModem.cpp` contains no `Serial.print`, no `MESH_DEBUG`, and the noisy mesh `Dispatcher`
is not even compiled into this example. sighop's own logging is therefore the *only*
observability into the radio layer — which raises the value of logging every frame at the
transport boundary, including ones that fail to parse. A malformed frame we silently drop is
invisible forever. The log stream and the packet log's `raw` column carry that rule; since
change `packet-archive`, `packet_archive` (§6) keeps every frame's bytes durably as well.
That rule also quietly recovers ESP32 panic backtraces, which arrive interleaved in the KISS
stream as unparseable bytes.

The link is also a **single point of failure with no side channel**, so the reconnect loop
and its wide events matter more than they would otherwise.

*Escape hatch, unused:* `examples/kiss_modem/main.cpp` supports moving KISS onto a hardware
UART via the `KISS_UART_RX` / `KISS_UART_TX` build defines, freeing USB entirely. No variant
defines them and the docs never mention it, so taking it means maintaining our own firmware
build — for a debug stream that does not currently exist. Recorded so it is not rediscovered
as new; not worth taking until we are chasing a radio-layer bug that needs custom
instrumentation anyway. Avoid GPIO 43/44 if we ever do: that is UART0, where the ESP32-S3
ROM bootloader chatters at reset.

Treat every `SetHardware` telemetry sub-command (`GetBattery`, `GetMCUTemp`, `GetSensors`)
as **optional and probe-detected** at startup: query once, record what the board answers,
and let the UI hide what is unavailable. Do not hard-code an assumption that any given board
supports any of them.

Bind the device by its stable path — `/dev/serial/by-id/usb-...` — not `/dev/ttyUSB0`.
USB enumeration order changes across reboots and replugs, and on a machine with any other
serial device the numeric path will eventually point at the wrong hardware.

### 4.2 RX pipeline

Per received frame:

1. Decode header (`0bVVPPPPRR`), transport codes if the route type calls for them, path, payload.
2. Reject malformed packets: path extent > 64 B, payload > 184 B, total > 255 B.

   **`path_length` is a packed byte, not a length.** Bits 0-5 are the hop count (0-63); bits
   6-7 are `hash_size - 1`, so a hop is 1, 2 or 3 bytes and code `0b11` is reserved. The path's
   byte extent is `hop_count * hash_size`, and that is what `MAX_PATH_SIZE` (64) bounds — the
   raw byte can legitimately reach 0xBF. Multi-byte hashes are not hypothetical: of the 351
   frames in the milestone 0 corpus, 152 use 1-byte hashes, **106 use 2-byte and 93 use
   3-byte**. Reading them as 1-byte hashes silently corrupts everything after the path, which
   is how this error was found — adverts decoded to garbled flags and truncated names.
3. **Deduplicate.** Flood routing means we hear the same packet repeatedly. Keep a shared
   cache keyed on a hash of (payload type, payload bytes) — deliberately excluding the path,
   which mutates at each hop. TTL on the order of minutes, sized in entries not time.
4. **Learn paths.** Record the reverse path from flood packets so replies can go DIRECT.
   Path knowledge is shared platform-wide, not per-entity — it's a property of the RF
   neighbourhood, and duplicating it per entity wastes memory and learns slower.
5. Fan out to every entity. Entities filter by destination hash and MAC.

Dedup and path learning are shared; interpretation is per-entity. This is the split the
spec's "fan-out queue" was reaching for.

### 4.3 TX scheduler — the risky part

Everything downstream of "the modem accepts one packet at a time" lives here. With N
entities each wanting to advert, a naive queue will jam your own channel and drown out the
mesh around you.

**Receive-only gate.** The scheduler has a global transmit-enable that is **off by default**.
When off, packets are accepted, scheduled, logged and counted exactly as normal, but dropped
at the final hand-off to the modem. This makes the entire RX path, the entity logic, the
budget accounting and the dashboard developable against a live mesh without ever keying the
transmitter — and it means a misconfigured or half-built instance cannot pollute a real
network. It is the single most useful safety property in the system; it is not a debug flag.

**Serialization.** Exactly one packet in flight. Submit, await `TxDone`, then take the next.
On `TxBusy`, treat it as a lost race and requeue at the head — never busy-loop.

**Priority classes**, highest first:

| Class | Contents | Rationale |
|---|---|---|
| 0 | ACKs | The sender is retrying until it hears one; delay multiplies traffic. |
| 1 | Direct replies to a live request (RESPONSE, PATH, room sync) | A human is waiting. |
| 2 | Originated messages (room broadcast, bot DMs) | Normal traffic. |
| 3 | Adverts | Purely periodic; always yields. |

**Airtime budget.** A **sliding window** of `(charged_at, airtime_ms)` covering the preceding
3600 s — not a token bucket, which only approximates it. The regulation says no more than
360 s of transmission in any hour, and a window is that sentence; the test asserts it verbatim
(milestone 3, design D4). Airtime, not packet count: time-on-air varies several-fold across
presets.

Time-on-air is calculated from the *live* radio configuration read back via `GetRadio`, never
from an assumed preset — a board that resets silently reverts to its build defaults, and
`SetRadio` is not persisted. Two corrections to what this section used to claim, both from
milestone 3:

- The EU narrow and legacy 250 kHz/SF11 presets do **not** "differ by more than an order of
  magnitude": they are within ~7% across the payload range, and legacy is marginally the
  *faster* of the two above 32 B. Four times the bandwidth against eight times the symbol time
  nearly cancels. The reason for reading parameters back survives — SF12/125 kHz really is
  over four times the narrow preset, and a wrong preset receives nothing — but the arithmetic
  offered for it did not.
- The firmware's preamble is **spreading-factor-dependent**: 32 symbols at SF ≤ 8, 16 above
  (`RadioLibWrappers.h:56`), not RadioLib's default of 8. Every time-on-air figure below was
  computed with the wrong preamble and understated the cost by ~15%.

Both are now checked against the board itself: `GetAirtime` (0x0F) returns the firmware's own
estimate for a given length, and sighop compares it against its computation at startup over a
ladder of lengths. Measured live on the V4, 2026-09-04: agreement to **sub-millisecond** at
16/64/128/255 B (deltas 0.09-0.59 ms, which is the firmware's truncation to whole
milliseconds). A disagreement is an error-level wide event and changes nothing — the computed
value is what the budget uses.

On EU 868 this ceiling is a **regulatory limit, not a tuning knob**. The 869.4–869.65 MHz
sub-band permits 500 mW e.r.p. conditional on ≤10% duty cycle — 360 s of transmit time per
hour. Note the stock firmware ships a 50% duty-cycle default, which does not satisfy that.
Accordingly:

- the ceiling defaults to **10%** and is enforced in sighop, independent of any modem setting
- raising it requires explicit configuration and surfaces a standing warning in the UI
- exhaustion stalls classes 2 and 3; classes 0 and 1 draw from a small reserve
- a packet that cannot be sent within its deadline is **dropped and logged as dropped** —
  silent unbounded queueing is the worse failure

A scheduler bug here is a compliance problem, not merely a bad user experience. It gets
tests that assert the ceiling holds under load.

*Deferred:* per-entity fairness shares. At 2–5 entities, strict priority plus round-robin
within a class is sufficient, and a starving room server is immediately visible on the
dashboard. Revisit only if the entity count grows.

**Advert policy.** Adverts are the one traffic class entirely under our control, and the
only one whose cost lands on other people's networks: a *flood* advert is rebroadcast by
every repeater in the mesh, so its true cost is mesh-wide, not local.

Community norms are far more conservative than the firmware defaults — repeaters and room
servers commonly run 47–49 h flood intervals, with [MeshCore Switzerland](https://www.meshcore.ch/settings/)
recommending 24 h as an absolute minimum and zero-hop adverts disabled entirely. The
firmware's own default moved from 3 h to 12 h expressly to cut airtime.

sighop therefore:

- defaults to a **24 h minimum** flood advert interval per entity, well above firmware default
- **disables zero-hop adverts** by default (interval 0), matching community practice
- applies independent jitter per entity (`base ± 25%`) and enforces a **minimum gap between
  flood adverts from any two local entities**, so N entities never burst together
- staggers initial adverts across the interval at startup rather than firing N back-to-back
- refuses to configure an interval below the minimum without an explicit override
- treats a flood advert requested outside the schedule — by a bot's greeter or by an
  operator from the panel — as that identity's scheduled flood: the next one moves a full
  jittered interval out rather than arriving on top of it, and the request counts toward
  the inter-entity gap like any other flood

At the 2–5 entity target these defaults put sighop at roughly the advert load of the 2–5
real nodes it is standing in for, which is the correct amount. The failure mode this guards
against is scale: 20 entities on the 12 h firmware default would emit 40 flood adverts a day
from one antenna, each amplified across every repeater in the mesh. The shared minimum gap
and the 24 h floor keep that from arising by accident if the deployment ever grows.

**A stored identity's own interval was written, displayed and never read, until change
`apply-store-changes-without-restart`.** `advert_config_for` has stored
`flood_interval_seconds` and `zero_hop_interval_seconds` per identity since milestone 4, and
the identity page has always displayed them — but `_adopt_entity` passed neither through to
`AdvertScheduler.add_identity`, so every stored identity adverted at the 24 h floor
regardless of what its row said, and no surface ever wrote a non-default value either. This
is why the drift was never noticed: reading and writing were both silent. Resolved by
reading the stored configuration at adoption (`None` is the floor, exactly the prior
behaviour, so no database gained a different advert schedule the day this shipped) and by
re-reading it on every live reconcile, so a future control that lets an operator edit the
interval has somewhere real to write to. Adding that control is still out of scope: it needs
its own override rules, confirmation and audit event, on the terms every other
transmit-affecting control already has.

**Half-duplex and the deaf window.** The radio cannot receive while it transmits, and at the
default preset (BW 62.5 kHz, SF8, CR 4/8) a single packet is a long time to be deaf:

| Packet size | Time on air ≈ deaf window |
|---|---|
| 64 B (typical) | **~0.74 s** |
| 255 B (maximum) | **~2.31 s** |

(These were ~0.64 s and ~2.2 s until milestone 3, computed with an 8-symbol preamble the
firmware does not use. The figures above are the board's own, confirmed by `GetAirtime`.)

The duty-cycle ceiling bounds the total: at 10% the receiver is deaf **at most 360 s/hour by
construction**, and the design's own defaults — 2–5 entities, a 24 h advert floor, zero-hop
adverts off, receive-only on a fresh install — put real usage far below that. The regulatory
limit already being enforced is simultaneously the bound on how much RX this costs us.

This is why sighop uses **one radio, not a separate receiver and transmitter**:

- Two SX1262s on the same frequency a few centimetres apart do not give continuous RX. At
  +27 dBm with ~11 dB of free-space loss over 10 cm at 869 MHz, the receiver sees ~+16 dBm
  against a +10 dBm absolute maximum — desense at best, a dead LNA at worst. Recovering the
  deaf window needs metres of separation or an attenuator, which returns us to where we
  started with twice the hardware.
- A second transmitter does not relax the one-packet-in-flight invariant. On a shared
  half-duplex channel, two simultaneous transmissions collide with each other.
- Two duty-cycle budgets is regulatory arbitrage this design explicitly rejects.
- A TX-dedicated radio still needs its receiver for CCA, so it is not TX-only; and it would
  add a second serial link and reconnect loop to a design that already names the single USB
  link as its SPOF (§4.1), plus dedup that must recognise our own transmissions as inbound.

The genuinely useful second radio is a *different channel or preset* (see the multi-modem
non-goal, §2), or — for v2 — **RX diversity at a distance**: a cheap capture-only board sited
somewhere physically separate, feeding the shared dedup and path-learning store over TCP
KISS. The separation is the point, and it improves path learning (§4.2) in a way a co-located
receiver cannot.

**CSMA.** The modem does p-persistent CSMA in half-duplex. Do not fight it; use
`IsChannelBusy` only as a scheduling hint and telemetry input.

### 4.4 The virtual network bus

An in-process asyncio pub/sub. RX is fan-out to all entities; TX is a submission API that
returns a handle the caller can await for `TxDone` and, where relevant, for an ACK.

Deliberately in-process for v1. If entities ever need to be separate processes, this
interface is the seam — but do not pay for that flexibility before it is needed.

---

## 5. Cryptography

Implemented in Python with `cryptography` / `PyNaCl`. Match the wire format exactly; the
weaknesses below are MeshCore's, and interoperability requires reproducing them.

The authority for this section is the firmware source — `src/Utils.cpp`, `src/Identity.cpp`,
`src/Mesh.cpp` and `src/helpers/BaseChatMesh.cpp` in the vendored submodule. The published
payload documentation does not describe the cryptography at all, and this section was wrong
about the ACK construction until it was checked against `BaseChatMesh.cpp`.

**DM shared secret.** ECDH over X25519. Ed25519 identity keys are converted to Montgomery
form (`crypto_sign_ed25519_pk_to_curve25519` for the peer's public key); MeshCore private
keys already carry a pre-clamped scalar, so the usual Ed25519 hashing step is skipped — it is
the first 32 bytes of the stored `prv_key`, used as-is and never re-hashed or re-clamped.
Cache derived secrets per (local entity, peer) — scalar multiplication per packet is
wasteful given the fan-out.

**Cipher.** AES-128-ECB. Key is the **first 16 bytes** of the shared secret. Plaintext is
zero-padded to a 16-byte boundary — not PKCS#7. No IV, no nonce; uniqueness rests entirely
on the 4-byte timestamp inside the plaintext.

**MAC.** HMAC-SHA256 over the ciphertext, truncated to the **first 2 bytes**. The MAC key is
the **full 32-byte** shared secret, where the cipher key above is only its first 16 — the two
keys are different slices of the same secret (`Utils.cpp:133` keys HMAC with `PUB_KEY_SIZE`
while `encrypt()` uses `CIPHER_KEY_SIZE`). Compare MACs in constant time.

Note also that AES-128-ECB decryption yields a multiple of 16 bytes and the original plaintext
length is **not** recoverable from the ciphertext: a body that legitimately ends in zeros is
indistinguishable from padding. Strip trailing zeros in the body parser, which knows the
layout — never centrally in the cipher, which would corrupt binary bodies like GRP_DATA.

**Channel keys (GRP_TXT).** Either a pre-shared 16- or 32-byte key, or derived from a hashtag
as `sha256(b"#roomname")[:16]`. The stock **Public** channel is a pre-shared key everybody
has — `izOH6cXN6mrJ5e26oRXNcg==`, `protocol.PUBLIC_CHANNEL_KEY` — and is exactly as private as
a hashtag. The channel hash in the payload is the first byte of `sha256(channel_key)` **taken
over the key at its real length** (`BaseChatMesh.cpp:907-910`): Public's hash is `0x11`, and
over the zero-extended 32-byte buffer it would be `0x17`, which no corpus frame carries. The
cipher and MAC key with that zero-extended buffer. **HMAC cannot tell the two apart** — it
zero-pads a short key to its block size itself, so a 16-byte key and its zero extension yield
the same MAC — which leaves the hash length the only falsifiable negative, and the known-answer
test asserts it. As of change `channel-messaging` this was also confirmed against a foreign
implementation, over 85 recorded `GRP_TXT` frames on `0x11` and 108 on `0x81` that did not open;
change `synthetic-corpus` withdrew those recordings with the recorded corpus (they were other
people's chat), and `tests/protocol/test_corpus_channel_decrypt.py` now does the same over the
synthetic corpus's frames — a self-consistency test, which says so. Hashtag-derived keys have a small keyspace and are brute-forceable — surface this in the
WebUI when a user creates a hashtag channel.

**Adverts** are unencrypted but Ed25519-signed over `public key ‖ timestamp ‖ appdata`, in
that order (`Mesh.cpp::createAdvert`). Signing is written out rather than delegated to
libsodium's `crypto_sign`, which takes a seed sighop no longer keeps: the standard RFC 8032
construction over the stored `prv_key`'s scalar and nonce prefix, pinned by a fixed vector on
the firmware's own keypair and by equality with `crypto_sign` for every key whose seed is
known. **Always verify the signature before trusting any
advert content**, including the name shown in the UI. An unsigned or badly-signed advert is a
discard, not a warning. The firmware additionally rejects an advert whose timestamp is not
newer than the last one seen from that identity, as a replay check — worth matching once
contacts are persisted (§6).

The **advert appdata flags byte is not purely bitwise**: its low nibble is a node-type enum
(`0` none, `1` chat, `2` repeater, `3` room server, `4` sensor) and only its high nibble is a
bit field (`0x10` location, `0x20`/`0x40` reserved features, `0x80` name). So `0x03` means
"room server", *not* chat-or-repeater. Observed live: `0x92` (repeater, located, named) and
`0x93` (room server, located, named).

**ACKs** are the **first 4 bytes of SHA-256** over `(4-byte timestamp ‖ txt_type/attempt byte
‖ text) ‖ sender public key` — see `BaseChatMesh.cpp:243`. **Not a CRC32**, as this section
previously claimed; an implementation built on that reading would have produced ACKs no
MeshCore node accepts. A received ACK payload may be **4 or 6 bytes** and only its first 4 are
compared (`:245`, `:740`); both forms are live, and the 6-byte one was observed acknowledging
a message sighop sent. It remains a checksum rather than a cryptographic proof — it is
unkeyed, so anyone who can read the plaintext can reproduce it. Treat an ACK as delivery
evidence only, never as authentication.

**Recorded history: this was once verified against a foreign implementation.** As of milestone
4 the shared-secret derivation, the AES-128-ECB key slice, the 2-byte HMAC truncation and both
directions of the ACK construction were confirmed against a **foreign implementation**: a
Heltec V3 running stock `companion_radio` v1.17.1-d929643 encrypted a DM to a key sighop held,
and it decrypted on every commit for as long as the recorded corpus stood. That exchange
existed and passed once; it was **withdrawn** by change `synthetic-corpus`, because the
recording and the burned private key that opened it were real data in the repository, and a
synthetic corpus can only prove sighop against itself. What anchors interoperability now is the
firmware-embedded signing keypair and the fixed known-answer vectors transcribed from the
firmware source (`tests/protocol/test_crypto.py`). `tests/protocol/test_corpus_decrypt.py` keeps
the negative half of the old vector, over generated frames — the full 32-byte secret used as the
cipher key, or the MAC keyed on only the first 16, must fail — so the two distinct key slices
stay distinguishable by evidence rather than by comment, and says plainly that it proves
agreement with our own encryptor and not interoperability.

Group messages carry **no sender authentication** — the sender name is plain text inside the
ciphertext (`<name>: <body>`). Anyone with the channel key can claim any name. The WebUI
must not render channel sender names in a way that implies verified identity.

---

## 6. Persistence

Postgres via SQLAlchemy 2.0 async. The eight-table sketch below is the original one. As of
milestone 7 **all eight exist**, and a ninth the sketch did not have joined the last of
them. Milestone 8 adds a tenth, milestone 9 an eleventh that is not about the mesh at
all, change `webhook-notifications` a twelfth, change `channel-messaging` a thirteenth and
fourteenth, change `repeater-metrics-collection` four more for what sighop collects from
the repeaters around it, and change `packet-archive` two for the permanent record of every
frame.

**Built (milestone 5, migration `0001`):**

- **entity** — id, type, name, public key, **sealed** private key, advert config (JSONB), enabled,
  created_at
- **contact** — public key (PK), node hash, name, node type, flags, `advert_verified`,
  first/last heard
- **path** — destination public key *or* node hash, path bytes, hash size, hop count, SNR,
  `confirmed_at`, packet id, unique on (destination, path bytes, hash size)
- **packet_log** — ring buffer of recent RX/TX for the observability UI (bounded; not the
  audit trail), with `raw` and `reason` for frames that could not be decoded

**Built (milestone 6, migration `0002`):**

- **room** — id, **unique** entity id, name, admin password hash, nullable guest password
  hash, `guest_open`, `allow_read_only`, nullable `retention_days` and `retention_messages`,
  created_at
- **room_member** — PK (room id, public key), node hash, permissions, `sync_since`,
  `last_timestamp`, first login, last activity
- **message** — id, room id, author public key, `post_timestamp`, nullable
  `sender_timestamp`, text as **bytes**, posted_at, unique on (room, `post_timestamp`)

**Built (milestone 7, migration `0003`):**

- **bot_state** — PK (bot id, key), value as JSONB, updated_at; the durable per-bot
  key/value store a driver persists in, and the only place a driver may write anything
- **bot** — id, **unique** entity id, driver name, enabled, mode, config (JSONB), created_at

**Built (milestone 8, migration `0004`):**

- **direct_message** — id, entity public key, peer public key, direction, text as **bytes**,
  `wire_timestamp` (`BIGINT`, the peer's clock as it travels), `handled_at` (`TIMESTAMPTZ`,
  ours), `ref`, `packet_ids`, attempts, route flood, route path, outcome,
  `ack_latency_ms`, unique on (entity public key, `ref`)

**Built (milestone 9, migration `0005`):**

- **web_user** — id, username (stored normalised: NFKC then `casefold()`; **unique**),
  Argon2id `password_hash` with its parameters, enabled, `created_at`, `password_set_at`
  (both `TIMESTAMPTZ`)

**Built (change `webhook-notifications`, migration `0006`):**

- **webhook** — id, **unique** name, **sealed** URL (seal version 2), `url_host` (scheme and
  host, clear), format (`json`|`discord`, checked), triggers (`TEXT[]`, validated in the
  repository rather than a database enum), nullable `max_hops` (checked `>= 0`), enabled,
  `created_at`, nullable `last_delivered_at`, `last_failed_at`, `last_failure`

**Built (change `channel-messaging`, migration `0007`):**

- **channel** — id, **unique** name, kind (`public`|`hashtag`|`psk`, checked), `hashtag`
  (required iff `hashtag`), **sealed** key (seal version 2, required iff `psk`), `channel_hash`
  (clear), `created_at`
- **channel_message** — id, channel id (`ON DELETE CASCADE`), direction, `ref`, nullable
  `entity_public_key` (outbound), nullable `unverified_sender_name` (inbound), text as
  **bytes**, `wire_timestamp` (`BIGINT`), `handled_at` (`TIMESTAMPTZ`), `packet_id`,
  `hop_count`, `snr_db`, `rssi_dbm`, outcome, nullable `outcome_reason` (the scheduler's
  reason for a post not transmitted), `repeats_heard`, unique on (channel id, `ref`),
  indexed on (channel id, `handled_at`)

**Built (change `repeater-metrics-collection`, migration `0011`):**

- **repeater_collection** — one row (`id = 1`, checked): `enabled`, nullable `entity_id`
  (`ON DELETE SET NULL`), `interval_minutes` (5–1440), `recent_days` (1–365),
  `retention_days` (1–365), and the last cycle's `started_at`, `finished_at`, polled and
  succeeded counts and skip note. No row reads as the defaults: disabled, 60 minutes, 3 days,
  30 days
- **repeater_target** — the selected repeaters, public key (PK) and `selected_at`
- **repeater_poll** — id, public key, nullable `entity_id` (`ON DELETE SET NULL`),
  `started_at`, outcome (`succeeded`|`not_sent`|`login_unanswered`|`status_unanswered`|
  `neighbours_incomplete`, checked), nullable reason, route, the repeater's eighteen status
  fields (nullable; SNR stored in dB), nullable `neighbours_total`; indexed on (public key,
  `started_at`) and on `started_at`
- **repeater_neighbour** — id, poll id (`ON DELETE CASCADE`), key prefix, seconds since heard,
  SNR in dB

**Collection settings are not a check constraint away from being stuck.** Removing the login
identity nulls `entity_id` and leaves `enabled` as it was; a constraint tying the two would
make the identity impossible to remove. The repository refuses enabling without an identity
when the settings are saved, and the collector skips every cycle with the reason until another
is chosen. **Selection is not a `contact` column**, so an advert refreshing a contact can never
clear it, and it has no foreign key to `contact`. Pruning deletes `repeater_poll` rows older
than the retention window and the neighbours go by cascade. **A downgrade from `0011` drops all
four tables**: the settings, every selection and all collected history.

**Built (change `packet-archive`, migration `0016`):**

- **packet_archive** — partitioned `BY RANGE (at)` into monthly children
  `packet_archive_yYYYYmMM`; `at`, identity `id` (PK `(at, id)`, because a partitioned
  table's key must carry the partition key), `kind` (`rx`|`unparsed`|`tx`, checked),
  `packet_id` (indexed), `raw` (never null), nullable `snr_db`, `rssi_dbm`, `reason`,
  `tx_result`, `airtime_ms`, `entity_id` (no foreign key) and `priority_class`
- **packet_archive_settings** — one row (`id = 1`, checked): nullable `retention_days`
  (30–3650), seeded NULL, meaning keep forever

**The archive is what the packet log deliberately is not.** `packet_log` is a feed: bounded,
bytes only for what failed to decode, and nothing depends on a row. `packet_archive` keeps
the exact wire bytes of every reception — duplicates, undecodable frames and bytes the modem
could not frame included — and of every resolved transmission, suppressed and dropped ones
included, so that a feature added later can be filled in from past traffic rather than
starting on the day it ships. It stores **no decoded fields**: the bytes are the source, and
a backfill decodes them with the decoder of its own day. The two are joined by `packet_id`.

It is written the way the feed is, not the way content is: off the reception path, on a
write-behind lane of its own, and a row that cannot be buffered or written is **dropped and
counted** (`arch_drop=` on the status line, and on the system page) rather than spooled to
disk. That was an operator decision: the archive is complete while the database keeps up,
and a gap is visible as a gap rather than silent. A replay run writes nothing to it.

**The runtime creates the partitions, and there is no DEFAULT partition.** The maintainer
creates this month's and next month's at startup (before the archive writer starts), every
six hours and on database recovery, so the app role needs CREATE on its schema — which it
already has, because it runs the migrations. A DEFAULT partition was rejected: once it holds
a month's rows, that month's own partition can no longer be created until they are moved,
which turns a counted gap into a manual repair. Without one, a missing month fails the
insert, the batch is counted as dropped, and the next pass fixes it.

**Retention is by whole month and defaults to forever.** With a bound set, the maintainer
drops every child whose month ended before `now - retention_days` — detach, then drop, never
row deletes — so records may outlive the bound by up to a month. At the measured ~191
receptions an hour the archive grows by roughly 0.3 GB a year. It is read back two ways: an
ordered, keyset-paged range read for in-process backfill, and an export from the system page
as a capture-format JSONL file whose header says `source: packet_archive`, which
`radio/replay.py` and the corpus tools read unchanged. **A downgrade from `0016` drops the
whole archive.**

**The thirteenth table holds channels, and it seals by kind** (§7 Channels). A pre-shared key
is a read-and-post credential, the same class as a webhook URL, so a `psk` row carries it
sealed. A `hashtag` row stores the hashtag and a `public` row only its kind, and both derive
their key when loaded: sealing a key anyone can derive from the row's own name protects
nothing, and storing Public as a kind is what lets the migration **seed the Public channel
without `SIGHOP_SECRET_KEY`**, which migrations do not have. `channel_hash` is clear so a
listing needs no secret. Seeding transmits nothing — posting still needs the transmit gate —
and an operator who removes Public keeps it removed; no later start re-adds it.

**The fourteenth table is `direct_message`'s shape for the same reasons.** `ref` is the post's
id outbound and the reception's `packet_id` inbound, every write is `ON CONFLICT (channel_id,
ref) DO UPDATE`, ordering is by `handled_at`, and a post left `awaiting` by a restart is
rewritten `unknown` at startup. The claimed sender column is `unverified_sender_name` so nobody
reads it without reading the claim. **Stored channel text is not encrypted at rest**, as for
direct messages, and the migration says so. Removing a channel deletes its history — history is
only meaningful under its key, and a channel re-added later is a new row — and both surfaces
state the count before doing it. Channel history is not pruned otherwise. **A downgrade from
`0007` deletes every channel and all channel history**; pre-shared keys have to be re-added from
wherever they were shared.

**The twelfth table holds webhooks** (§7). The URL is sealed like a seed, because a Discord
or n8n webhook URL is itself the posting credential and a dump must not hand one out;
`url_host` is kept beside it so a listing needs no secret and shows no path. Triggers are
text, so adding one is a code change and never a migration. **A downgrade from `0006` deletes
every webhook**, URLs included; they have to be copied again from wherever they were issued.

**The eleventh table holds operator accounts** for the web interface (§8). They are durable
state because revoking one has to reach a running process: each session re-reads its row at
most once a minute, and `password_set_at` is the credential epoch it compares, so no
separate session-generation counter can drift from the thing it describes. Normalising the
username in one function (`normalise_username`, used by the repository, the CLI and sign-in)
makes the plain `UNIQUE` case-insensitive without `citext`, an extension the measured role
may not be able to create. **A downgrade from `0005` deletes every account** — only hashes
were ever stored, so nothing is recoverable — and `run --web` then cannot start until the
schema is upgraded again and accounts are re-added. The migration's docstring says so.

**The tenth table exists because `message` is room-scoped.** `message` hangs off `room_id`
because a room server's whole purpose is to hold what was posted to it; a person's own
conversation belongs to no room and so had no store at all, which made every direct message
a thing that existed only in the process that saw it. `ref` — the send's `message_id`
outbound, the reception's `packet_id` inbound — is what makes "written at submission,
updated when it resolves" *one* row: every write is `ON CONFLICT (entity_public_key, ref)
DO UPDATE`. A message in flight is therefore visible, a resolved one does not appear twice,
and a restart mid-send leaves an outcome that is *unknown* rather than a claim of delivery.
Ordering is by `handled_at` and never by `wire_timestamp`: a peer with a wrong clock must not
be able to reorder a conversation.

**Stored direct message text is not encrypted at rest.** §6 is careful that a database dump
must not be sufficient to *impersonate* a room server — `entity.sealed_private_key` is ciphertext
under a key held only in the environment — and it makes no equivalent promise about content.
A dump of `direct_message` exposes conversation content in the clear. That is a deliberate
trade rather than an oversight: the key that would encrypt it is one the interface reading
those rows would have to hold anyway. It is stated in the migration's own docstring, so it
cannot be discovered from a column type.

**`bot` was not in the sketch, and the reason it exists is worth stating.** §6 imagined a
bot's whole durable state as key/value rows. That predates a bot having a *driver name*, a
*mode* and *configuration*, and those are per bot rather than per key — there is nowhere in
a key/value table for them to live. The rejected alternative was keeping them in
`entity.advert_config`: that column describes how an identity adverts, and putting a
greeting template in it would make one column mean two things and make "list the bots" a
scan of every entity. `bot` is `room`'s shape deliberately — unique `entity_id`, enablement,
JSONB configuration — so the loader, the CLI noun and the startup reporting each have a
direct analogue and milestone 7 wrote little new structure.

Two columns on `bot` carry the safety posture rather than configuration. **`mode` is
`observe` or `active` and a new row is `observe`**: a bot that transmits is an explicit
operator act, stored rather than passed per run, so a bot cannot become active because
somebody forgot which flags the last run had. And **a downgrade of `0003` loses every
greeting record**, which is stated in the migration's docstring rather than discovered — a
node already greeted can be greeted again if its `contact` row is lost with them.

The list above was always a sketch and never final DDL, and milestone 6 was right that
building the three untested would have made the first real migration a rewrite. **Three
shapes turned out to differ from the sketch**, and each is a thing the firmware forced
rather than a preference:

- **The ACL is a routing and cursor table, not a permission table.** The firmware's
  `ClientInfo` (`src/helpers/ClientACL.h`) keeps `sync_since`, `last_timestamp`,
  `permissions` and `out_path` on one record, and `room_member` has to as well: the sync
  cursor and the replay guard are per member and have nowhere else to live. One consequence
  is better than it looks — revocation removes membership, permissions, cursor and replay
  guard together, because all four are one row, so there is no partial revocation to get
  wrong.
- **`message` needs an ordering value the wire protocol can name, not a bare timestamp.**
  `post_timestamp` *is* the cursor: the push carries it, the acknowledgement advances
  `sync_since` to it, and a keep-alive may force a cursor to a specific one. So it has to be
  a **total order within its room** — `max(now, last + 1)`, enforced by `UNIQUE (room_id,
  post_timestamp)`, which is what makes a clock that steps backwards produce a *stall in
  stamping* rather than a duplicate or a reordering. The alternative — row id as the cursor,
  timestamp cosmetic — was rejected because the id is ours and the timestamp is the peer's,
  and a cursor the peer cannot name is not a cursor.
- **Retention needs two independent bounds, not one policy field.** Age and count bound
  different things and an operator wants either or both; both are nullable and default to
  NULL, which is "keep everything". §13's unknown #1 asks what a sensible default is, and it
  is answerable only from observed volume — a default that deleted history before the
  question was asked would have answered it by accident.

A fourth detail is not a shape but a type: `sync_since`, `last_timestamp` and
`post_timestamp` are **`BIGINT`, not `TIMESTAMPTZ`**, because they are MeshCore's unsigned
32-bit epoch seconds *as they appear on the wire* rather than instants. Mixing the two
representations in one column is how a comparison silently changes meaning.

Three details of milestone 5's four are worth stating because they look like mistakes:

- **`node_hash` is indexed but not unique**, on any of the three tables that carry one —
  `entity`, `contact` and now `room_member`. §3 says one byte of identity collides at 1 in
  256 and the whole design is built on candidate sets; a unique constraint there is a bug
  waiting for a busy mesh. Two members of one room may share a node hash, and eventually
  will.
- **`path.path_bytes` may be empty**, and an empty path is a *zero-hop route* — the most
  useful route a node can have — which is a different thing from no row at all.
- **Every timestamp is `TIMESTAMPTZ`** and every value crossing the boundary is
  timezone-aware UTC, with naive datetimes rejected rather than assumed. The development
  server's own `TimeZone` is `Europe/Helsinki`, so a `TIMESTAMP WITHOUT TIME ZONE` column
  would record local wall-clock time here and something else against a UTC server.

Message history is durable and survives reboot, per spec — the whole reason a room server
beats a walkie-talkie.

`packet_log` is a bounded ring buffer, aggressively pruned. It exists to power the live
feed, not to be a permanent record; unbounded packet logging on a busy mesh will fill a
disk. The permanent record is `packet_archive` (above), which is partitioned by month so its
retention costs a `DROP TABLE` rather than a delete. Milestone 8 gave it its first **read** — `recent(limit)`, newest first, capped, under
the engine's existing statement bound — so the WebUI's feed can paint what happened before
the browser connected. It stays a feed: nothing on the reception, dedup, dispatch or
transmit path consults it, and a degraded database answers the read as unavailable rather
than queueing it. `direct_message` is deliberately *not* pruned with it — a conversation is
content and the feed is a sample.

**Memory stays the authority.** Contacts and paths keep their in-memory stores and their
interfaces, are loaded from the database once at startup, and answer every lookup from
memory — including while the database is unreachable. Writes happen behind the reception
path, and the three stores differ only in how much a lost write costs: a contact is written
promptly and never dropped (re-acquiring one means waiting for the peer to advert, and the
advert floor is 24 h), while a route and a log row are dropped freely and counted, because
the next reception regenerates one and the other is a feed. Migrations are the only
authority on schema; `sighop run` refuses a database that is not at the revision the code
expects, and never migrates as a side effect of starting — unless asked by name with
`run --migrate`, which the compose deployment passes (milestone 9) and which still refuses a
database ahead of the code.

**`bot_state` is the exception to write-behind, and it is the sharpest one.** A dropped
contact costs a re-learn; a dropped greeting record costs a *second unsolicited message to a
stranger* after the next restart. Worse, the greeter needs the record to have **landed**
before it transmits, and a queue answers "written" before that is true. So bot state is a
bounded repository call from the bot's own worker task, under the connect and statement
timeouts the engine already sets, and a write that fails is reported as a failure and
suppresses the action it was guarding.

### Private keys at rest

Entity private keys are the platform's crown jewels — they *are* the identities. Encrypt
them at rest with a key from the environment (`SIGHOP_SECRET_KEY`), never in the database.
A DB dump must not be sufficient to impersonate a room server. Provide `sighop keys export`
/ `import` so operators can back identities up deliberately, and make the WebUI's key
display an explicit, audited action.

**How, as of milestone 5.** The private key is sealed with **XSalsa20-Poly1305 secretbox**
(PyNaCl's `SecretBox`, already a dependency because the identity code uses libsodium), and
the stored value is a **version byte followed by the sealed box** so a future re-key has
somewhere to declare itself. The box generates its own nonce, which is the one thing most
likely to be got wrong by hand, and Poly1305 supplies the authentication tag — a tampered
`sealed_private_key` fails loudly instead of yielding some other key. On load the public key
derived from the decrypted private key is compared against the `public_key` column stored
beside it, and a mismatch names the entity rather than preferring either value.

**What is sealed changed in `create-entity-with-known-key`.** Until then the column held the
32-byte seed. It now holds MeshCore's 64-byte `prv_key`, because that is the only
representation an operator can bring in from a device — the seed behind one is behind a
SHA-512 nobody can walk backwards, while the expansion is everything signing needs. Migration
`0008` renames the column to `sealed_private_key` and reads no key material, so it still runs
without the secret; it reports how many rows it strands. A row still holding a seed decrypts
but is **refused by name**, not reported as corrupt: the row is intact and the secret is
right, and only the format is gone. `sighop keys delete` removes such a row so the same
identity can be imported again from its private key — without it the stranded row blocks its
own replacement, because it still holds the public key.

`SIGHOP_SECRET_KEY` is base64 of **exactly 32 bytes**, generated by `sighop keys secret`
from the system CSPRNG. A value of the wrong length or encoding is a startup failure that
says which; it is never padded, truncated or hashed into shape, and there is no passphrase
KDF — accepting a passphrase invites `hunter2` and then requires an Argon2 parameter
conversation for something no human needs to type. Losing the secret makes every stored
identity unrecoverable; that is what encryption at rest means, and `sighop keys export` is
the mitigation §6 already asked for.

Keyfiles remain, demoted to what §6 wanted: an interchange format. `sighop keys import`
seals one into the store and `sighop keys export` writes one back out, owner-only, refusing
an existing path. Both say — as does `sighop keys new` — that a keyfile holds an
*unencrypted* private key protected only by its permissions, because the same key inside the
store is encrypted and an operator must not conclude the two offer the same protection.

Keyfiles are at **version 2**, recording `private_key_hex`. Version 1 recorded `seed_hex` and
is refused with its own message rather than read or converted: a keyfile is material this
system does not own, and rewriting an operator's file on their behalf is not its call. Both
`keys new` and `keys import` take `--private-key` — 128 hex characters, the clamping checked
and never applied, since clamping a key that arrived unclamped would derive a *different*
public key from the one that identity advertises elsewhere. There is no way to supply a seed.

---

## 7. Entity behaviors

### Room server

- Advert with the room-server node type (`0x03` in the appdata flags byte's low nibble — a
  type enum, not a bit flag; see §5) on its schedule.
- **Login** via `ANON_REQ` (0x07): destination hash, sender public key, MAC, then an
  encrypted payload of `timestamp ‖ sync_timestamp ‖ password`. Validate against the guest
  or admin password, then store the sender's public key in the ACL with its permission level.
- After first login, the peer is known: subsequent traffic arrives as ordinary `REQ`, no
  re-login. Persist ACLs — they must survive restart or every member re-authenticates.
- **History sync:** on login, the client supplies a sync timestamp; deliver messages newer
  than it. Push through the TX scheduler at priority 1, and respect the airtime budget — a
  member returning after a week must not monopolize the channel. Chunk and pace it.
- **Broadcast:** a message posted to the room goes to members. With no repeating and one
  antenna, "delivery" means transmitting; members out of range get it on next sync.
- Retention is policy-driven per room (age and/or count), enforced by a periodic task.

**Password policy.** The client sends the password inside the *encrypted* `ANON_REQ` payload,
so the server sees plaintext only momentarily at login and never needs it recoverably:

- **Argon2id hashes, never plaintext**, for both guest and admin passwords.
- **A distinct admin password per room server entity**, not one platform-wide. Each virtual
  room server is a separate identity to the mesh; a shared admin credential would mean
  compromising one exposes all, and it forecloses handing one room server to someone else
  to administer.
- An **empty guest password** is legal (MeshCore treats it as an open room) but must be an
  explicit choice in the UI, never the default.
- **Rotation evicts nobody.** Because the ACL stores the peer's public key after first
  login, existing members keep working after a password change — rotation only gates *new*
  joins. This is counterintuitive enough that the UI must say so, and it means removing a
  member requires explicit ACL deletion. The room member list therefore needs a revoke
  action, or there is no way to remove anyone.

**What the firmware actually does, where it differs from the summary above** (milestone 6,
read from `examples/simple_room_server/MyMesh.cpp` and `src/helpers/ClientACL.h` rather than
from `docs/payloads.md`). Four of these are not what the section above would lead you to
build, and each is matched deliberately:

- **A failed login is answered with silence** (`:353`) — no refusal payload, no error, no
  acknowledgement. That behaviour is load-bearing rather than incidental: it is what makes
  *an unauthenticated stranger cannot make sighop transmit* true, which is the same rule §7
  states for the greeter bot, one milestone early. A *successful* login can make sighop
  transmit, and a replayed one too, so the reply path is bounded three ways — a per-source
  token bucket, a global rate, and the Argon2id concurrency semaphore — and every refusal is
  counted by reason and reported. A throttle that drops silently is indistinguishable from a
  mesh that went quiet.
- **An existing member logging in with an *empty* password skips the timestamp check
  entirely.** `:335-342` short-circuits before both the password check and the replay guard.
  It is how a client re-establishes a lost route, a client that cannot re-establish one has
  silently left the room, and diverging would break interop with every stock client. It is
  also the single most replayable packet in the protocol, which is precisely why the
  throttle exists. Matched on purpose, and the trade is stated rather than hidden.
- **The read-only fallback is `PERM_ACL_GUEST`, which is zero.** A wrong password with
  `allow_read_only` set is admitted at `:350` as the level whose posts are refused at
  `:479`. So what this section calls *read-only* is the byte `0`, and `PERM_ACL_READ_ONLY`
  (1) is declared by the firmware and never assigned by the room server. Byte 7 of the login
  response carries the permission byte in `v1.17.1`, where older firmware read it as an
  unsynced count; we emit the current form, and a client old enough to disagree shows a
  wrong badge rather than failing to log in.
- **A room keeps 156 bytes of post text, and truncates rather than refuses.** The receive
  path has **no length check at all**: `addPost` runs and `send_ack = true` whatever the
  length (`:484-488`), and the acknowledgement is computed over the **full received** text
  (`:461-462`), not over what was kept — which is exactly what lets a client stop retrying a
  post the room shortened. We originally refused an over-long post instead; the milestone 6
  live exercise showed a stock client composing 156 bytes and reading our silence as a lost
  packet, retrying until it gave up with its user told nothing. On this protocol a refusal
  *is* silence. Three numbers are in play and only one of them is a limit:
  **167** is the hard ceiling — a push payload is `dest_hash(1) + src_hash(1) + MAC(2) +
  ciphertext` within `MAX_PACKET_PAYLOAD` (184), so 176 bytes of ciphertext, less the push
  prefix of 9. **156** is what a stock client can send and be shown: `queueMessage`
  (`companion_radio/MyMesh.cpp:432`) bounds the frame the radio hands the phone app against
  `MAX_FRAME_SIZE` (176) and spends `4 + 6 + 1 + 1 + 4 + 4 = 20` on prefix, which is why the
  composer stops there. **150** is what stock firmware keeps —
  `StrHelper::strncpy(text, postData, MAX_POST_TEXT_LEN)` (`:57`) copies while `buf_sz > 1`
  (`TxtDataHelpers.cpp:3-9`), one below the 151 its constant reads as, and that 151 is
  `MAX_TEXT_LEN` (160, ten cipher blocks chosen for *chat*) less the same 9. We store 156,
  the only one derived from what a client can actually do, so nothing stock composes is ever
  shortened. It costs byte-identity with a firmware-served room for posts of 151–156 bytes,
  and one extra cipher block of airtime on posts above 150. Truncation above 156 is reported
  alongside the post with the length as received, so the operator sees the drop even though
  the author cannot be told. A post made *locally* is still refused, because that author is
  present to shorten it.

**The push loop's constants**, copied as-is (`:5-11`, `:995-1039`): round-robin over
members at `SYNC_PUSH_INTERVAL` 1200 ms, one outstanding delivery per member, eight times
faster when the current member had nothing to send, a new post held `POST_SYNC_DELAY_SECS`
(6 s) before it is eligible at all, an author never sent its own post, and three consecutive
unacknowledged deliveries before a member is left alone until it is next heard from. The
acknowledgement window is the firmware's own — 12 s flooded, `4000 + 2000 × (hops + 1)` ms
direct — rather than an airtime multiple, because a push may be the first packet a returning
member has seen in a week. The push's `attempt` field is drawn at **random**, not counted:
that is what gives a retried push a different packet hash, and therefore a different
expected acknowledgement, so a repeater cannot deduplicate the retry away.

**One thing sighop does that the firmware cannot.** The firmware acknowledges a post after
putting it in a 32-entry RAM ring, which cannot fail. Ours can, so the acknowledgement is
sent **only once the row has landed**, bounded by the sender's own acknowledgement window
and never on the bus handler. An acknowledgement is a promise the client will not retry and
will show the message as delivered; sending it before the row lands would make sighop lie
about the one property a room server exists to provide. While the database is degraded a
room accepts nothing and says so — a room is exactly as available as its history, which is
the price of the history being real.

**One thing sighop does that the firmware does not.** A post that arrives flooded is
acknowledged by a flooded `PATH` return that carries the path the post travelled and bundles
the ACK, the way a companion answers a flooded direct message (`BaseChatMesh.cpp:328-340`)
and the way sighop already answers a flooded login. `simple_room_server` instead floods a bare
ACK when it has no out-path for the client (`MyMesh.cpp:494-497`), and sighop used to send the
ACK along whatever route it had learned — for a client sending zero-hop floods, a zero-hop
DIRECT route learned from the post itself, transmitted once, relayed by no one, and missed by
a client that then retried forever. A client floods because it has no route to us, so the
reversed flood path is not evidence our reply gets back over it. The path return costs about
22 bytes instead of 6, and buys a client that stores a route to the room and stops flooding
every later post across the whole mesh. A post that arrives direct still gets a bare ACK along
the member's known route, flooded only when none is known. The route taken is logged as
`ack_route` on `room_post_stored`.

**Posts tolerate a clock split the firmware does not.** A companion stamps posts with the
phone's clock (`companion_radio/MyMesh.cpp:1089-1107`) but logins and keep-alives with the
radio's (`BaseChatMesh.cpp:577`). The firmware's single guard (`MyMesh.cpp:448`) compares the
two as one clock, and a radio running 15 s fast locked a stock client's posts out: every post
fell below the timestamp its own login had raised, went unacknowledged, and showed "failed"
(`issue-room-post-2`). The companion knows the problem exists, since it restamps CLI commands
"to avoid tripping replay protection" (`companion_radio/MyMesh.cpp:1103`), but not for posts.
So a post is accepted up to `POST_CLOCK_SKEW_TOLERANCE_S` (300 s) below the member's recorded
timestamp, which never moves down. A replay inside that window is found by the stored-post
lookup and only acknowledged again, and `retry` on `room_post_stored` means exactly that it
was already stored. A post further below is refused with the gap in seconds in its detail.
Requests (keep-alive, status, telemetry) keep the strict check: a replayed keep-alive can
move a member's sync cursor, and the mirror case, a phone ahead of its radio, has not been
seen. Separately, sighop used to raise the recorded timestamp on an existing member's
blank-password login, which the firmware does not: that path bypasses the whole update block
(`MyMesh.cpp:335-376`). It now leaves the timestamp alone, so a route re-establishment cannot
lock posts out, and `room_login_admitted` logs `empty_password` so a report shows which path a
client took.

### Companion

An addressable identity driven by a human in the WebUI: send/receive DMs, join channels,
maintain a contact list. Effectively the reference implementation of the client side of the
stack — build it early, since it exercises nearly every protocol path.

### Bots

A bot is a companion with a driver plugin. **As shipped in milestone 7 the interface has two
handlers, not three:**

```python
class Bot(Protocol):
    driver_name: str

    async def on_advert(self, ctx: BotContext, event: AdvertEvent) -> None: ...
    async def on_direct_message(self, ctx: BotContext, event: DirectMessageEvent) -> None: ...
```

`on_channel_message` is **absent, and its absence is intent.** Until change
`channel-messaging` the reason was that nothing decrypted `GRP_TXT`, so a hook could never
fire. That is no longer true, and the hook stays absent **by operator decision**: a bot reacting
to channel traffic is triggered by unauthenticated content from anyone holding a key that is
often guessable, and a bot posting to a channel floods the mesh. Both deserve their own change
rather than arriving as a side effect of channels existing.

`BotContext` exposes sending, contact lookup and durable state, and nothing else: not the
scheduler, not the bus, not the modem, not a database session. That is what makes the rate
limit, the mode and the never-flood rule properties of the seam rather than checks a driver
could forget — there is no path along which a driver could reach the radio to bypass them.
It also carries a *non-consuming* `can_send`, split from `send` the way the room server's
throttle splits `check` from `spend`, because the greeter must write its greeting record
before it transmits and a limit discoverable only by attempting the send would burn that
record on an action the limit was going to refuse.

An `AdvertEvent` exists only for a signature-verified advert, because it is built from a
`ContactObservation` and the contact store produces one only from a `VerifiedAdvert`. Rule 3
below is therefore carried by the type system rather than by a check anybody has to
remember.

Driver work runs on a bounded queue with one worker task per bot, never on a bus subscriber
and never in front of an acknowledgement. Overflow drops the *oldest* pending dispatch and
counts it, matching the bus's own subscription semantics; a driver that raises is reported
with the bot and the triggering event, counted, and otherwise ignored. Bots ride on the
existing direct messenger's reports and the contact store's observations rather than
subscribing to the bus themselves — doing that would decrypt one packet twice and, if it
also acknowledged, put two acknowledgements on the air for one message.

**Bots require a database, and a run without one says so.** Rule 1 below is a statement
about the persistent `contact` table; without Postgres the store starts empty every run and
every contact looks new, so a DB-less greeter would greet the whole neighbourhood on every
restart. Rooms already refuse to exist without a database, and bots take the same rule and
the same startup line.

**Greeter bot.** Sends a welcome DM to nodes not seen before.

Four things it must get right — three from the original sketch and one milestone 7 added:

1. *"New"* means **never greeted**, recorded durably per bot — not new since process start,
   and (as built) not "never heard of" either. A restart must not re-greet the whole
   neighbourhood, and the greeting record is what stops it.

   The original sketch read "new" as *never seen in the persistent `contact` table*, and
   implementing it showed those are two different facts. "We have never heard this key" is
   the platform's memory of the mesh; "we have never said anything to this node" is one
   bot's. Gating on the first refuses every peer heard before the greeter existed — the one
   advert that would have qualified it is gone and will not come again — and silently
   refuses every peer we skipped once because the rate limit was exhausted or the database
   was degraded. Both are nodes with an unsent welcome, which is precisely what a greeter is
   for.

   So the record decides alone. The risk that inverts — a greeter created on an established
   node owing a greeting to the whole contact table — is answered by making the debt
   explicit rather than by a gate: **creating a greeter seeds a record for every contact
   already present**, marked `seeded` rather than sent, and reports the count. A new greeter
   starts owing nothing, and `sighop bot greeted` is where an operator sees that and changes
   it — releasing one contact so it is greeted when it next adverts, or marking one so it
   never is. One mechanism, readable, at the granularity a decision to message a stranger
   deserves.
2. Rate-limit greetings globally. A busy mesh, or a burst of adverts after an outage, would
   otherwise produce a flood of DMs — antisocial and airtime-expensive. One token bucket per
   bot and no per-source bucket: the per-contact gate is absolute, so the only bucket that
   can be exhausted is the global one.
3. Only greet after verifying the advert signature (§5). Greeting an unverified identity
   means a spoofed advert can make sighop transmit on demand.
4. **Greet only what is nearby.** The reception's own hop count against a configured
   `max_hops`, defaulting to 1 — nodes we hear directly and nodes one repeater away, which
   is the neighbourhood a greeting is for. A flood advert that reached us from half a mesh
   away is heard and recorded and not answered.

   **What this does not do, stated rather than implied:** `max_hops` bounds *network*
   distance, not radio distance. A tropospheric or ducted path delivers a zero-hop advert
   from a node hundreds of kilometres away, and no hop count will ever separate that from a
   neighbour across the street. `min_snr_db` is offered alongside and is null by default,
   because a strong ducted signal defeats it too. The honest position: `max_hops` bounds how
   much of the mesh can trigger us, the once-ever rule bounds the damage when one gets
   through, and neither claims to identify a duct.

A fifth gate is not about who to greet but about how: **a greeting is never flooded.** It is
unsolicited traffic to a peer that has never contacted us, so a peer with no known route is
suppressed with that reason rather than shouted across the mesh. A sixth restricts node
type to ordinary chat nodes by default, so no repeater or room server is sent a message no
human will read.

**Observe is the default mode, and transmitting is the opt-in.** A newly created bot runs
its whole decision path, records and renders every action it *would* have taken, and puts
nothing on the air. Moving it to `active` is a deliberate `sighop bot mode` invocation, and
the run's `--enable-transmit` gate and duty-cycle ceiling apply on top of that. Two
independent gates for the reason milestone 4 kept `--enable-transmit` after adding the
airtime ceiling: the mode says *this bot is meant to act*, the flag says *this run is
allowed to transmit*, and they answer to different people. Observe mode still spends from
the rate limit, so a dry run's counters are what an active run would have done.

**The greeting record is written before the greeting is transmitted.** A crash between the
two costs one un-sent greeting, which is invisible to its recipient; the reverse order costs
a duplicate message to a stranger after every crash, which is the failure rule 1 exists to
prevent. This is the room server's "acknowledge only once the row has landed" pointed the
other way: there the row was the promise, here the record is the guard. Its consequence is
stated rather than hidden — while the database is degraded the greeter greets nobody, and
says why.

**The record holds an attempt, not a delivery.** Only an acknowledgement settles a contact
for good (along with a seed, an operator's mark, and an observe-mode decision). An
unacknowledged greeting is retried on a later advert from that contact, fifteen minutes
apart, three times in all. The live exercise is why: its one greeting went unacknowledged
because the peer could not read it, the record settled the contact anyway, and the peer could
never be greeted again — a certain permanent failure traded away to avoid a possible
duplicate. The two bounds are what keep the retry from becoming the spam the original
reasoning feared.

**A peer is introduced to before it is messaged.** A direct message is encrypted under a
secret derived from the *sender's* public key, so a peer that has never heard the greeter's
advert cannot read a byte of a greeting and has nothing to acknowledge — which from our side
is indistinguishable from a peer that is not listening. So the greeter adverts first: a
zero-hop advert for a contact heard directly, which stops at direct neighbours and costs the
mesh nothing, and a flood advert — repeated by every repeater — only on a retry to a contact
heard further away, where the cheap attempt has already proved insufficient. The advert is
waited for, because an advert is class 3 and a message is class 2, so a send that did not
wait would overtake the very advert that makes it readable.

**That escalation happens in the same reaction as the silence, not at the next cooldown.**
A second exercise proved the point at one hop: the bare greeting went out four times and was
never answered, because the peer held no key for us — the same failure as before, merely
deferred. Silence from a distant peer is not ambiguous enough to sleep on. It will mean the
same thing in fifteen minutes, and the peer is adverting *now*, which is the one moment it
is known to be awake. So the flood advert and the second greeting follow immediately, **once**,
after which the cooldown governs everything further. A direct neighbour never escalates: it
was introduced to before its first greeting, so its silence means something the escalation
cannot fix.

**Silence is believed only after a grace period.** The message path spends four attempts in
about forty seconds; the escalation it triggers is a flood the whole mesh repeats. An
acknowledgement returning over a longer path than the one we sent on is late rather than
absent, so a greeting keeps listening past the last attempt — thirty seconds by default,
configurable — with the expectations it already registered still armed. It buys listening
and never a packet. This is opt-in at the message path rather than default, because an
interactive send that returned half a minute after it had already failed would read as a
hang; a bot deciding whether to spend the mesh's airtime is exactly the caller that should
wait.

Every suppression is counted by reason and reported, because a greeter that silently greets
nobody looks exactly like a mesh that went quiet — and exactly like a broken greeter.

### Channels

Change `channel-messaging`. Reading group text on the station's channels (§3) and posting into
them as a loaded identity. A channel message has no sender authentication, no recipient and no
acknowledgement, and every surface says so rather than hiding it.

- **Kinds.** `public` (the stock Public channel, §5), `hashtag` (key derived from the name,
  brute-forceable) and `psk` (a 16- or 32-byte key, sealed at rest). Hashtag and Public channels
  are marked *guessable* wherever a channel is added or listed. A key equal to one already stored
  is refused naming that channel, as is a name in use; a different key with the same one-byte
  hash is accepted, because it collides routinely (§3). All rules live in `ChannelRepository`,
  so the command line and the panel refuse identically. Channels require a database: a run
  without one loads none and says so at startup, and the channel commands refuse.
- **Receive.** `net/channels.py`'s `ChannelMessenger` is a bus subscriber, never inside
  `net/rx.py`, so replay determinism is untouched. For each `GRP_TXT` it trials **every** loaded
  channel whose hash matches (no firmware cap of four — a local store is small), decrypts only
  under a channel whose MAC matched, and stops at the first whose plaintext parses. No matching
  hash is counted as *unknown channel*; a hash with no matching MAC as *undecryptable*.
- **Plain text only**, as the firmware does (`onGroupDataRecv` drops `txt_type >> 2 != 0`): a
  decrypted message of another text type is counted as unsupported and not presented, and
  `GRP_DATA` is never decrypted — it stays in the feed as the payload it is.
- **A sender name is a claim.** It is shown only as the name the plaintext claims, marked
  unverified wherever it appears — in events under a field named `unverified_sender_name`, in
  run output with the same marking as other unauthenticated content, in the panel with a
  presentation that shares nothing with a verified identity — and never linked, coloured or
  badged as a contact or local identity of the same name. A message with no `": "` has no sender.
- **Compose.** A post is plain group text: the station's clock in epoch seconds, text type plain,
  attempt zero, `"<identity name>: <text>"` (`protocol.build_group_text_body`), encrypted under
  the channel key and carried in one `GRP_TXT` under its hash — byte-identical to
  `sendGroupMessage`. The timestamp is `max(now, last + 1)` station-wide, so two identical posts
  never share a packet hash and a repeater never drops the second as a duplicate.
- **The 160-byte limit, refused rather than truncated.** `name: text` must fit 160 bytes of
  UTF-8 — the firmware's `MAX_TEXT_LEN`, which stock senders produce and stock clients display in
  full (the phone frame gives 165). Both ends agree, unlike the room post constant milestone 6
  found. A name containing `": "` is refused too, because receivers split on the first one. A
  post is also refused for a channel or identity not loaded and while the transmit gate is closed.
  Refusals happen before composition and record nothing; the author is present to edit.
- **One flood, class 2, no retry, no acknowledgement.** The outcome is `awaiting` →
  `transmitted` | `not transmitted` with the scheduler's reason, and never a claim that anyone
  received it. There is no cooldown: the gate and the airtime ceiling are the controls, as for
  direct messages. Every post is run output naming the identity, and the account when it came
  from the panel.
- **Our own post heard back is a repeat, not a message.** The dedup cache sees receptions only,
  so a repeater's copy of our flood arrives fresh and would decrypt as someone using our name. A
  transmitted post is remembered by its dedup key in a registry local to the messenger (256
  entries, 1 h). A reception observer — which sees every copy, duplicates included — counts each
  hit as a repeat heard on the post; the bus handler skips a registered key entirely, so the first
  copy is not counted twice and never recorded as inbound. It matches by payload and **never by
  claimed name**: a message claiming `dev-companion` that we did not post must appear as
  received. The count is labelled as repeater evidence, and *none heard* is stated not to mean
  not received. The shared dedup cache is not seeded instead: that would change dedup semantics
  for all traffic, its 300 s TTL is shorter than a slow flood echo, and our own adverts' echoes
  would vanish from counters that show them today.
- **Refresh.** The channel set is an immutable snapshot held in memory, loaded at startup and
  swapped atomically. A change in the panel applies at once through `reload_channels()`; a change
  from another process is picked up by a 60 s reload, and a reload that adopts a *different* set
  says so — `channel_set_changed`, named channels and a run-output line, because the CLI's promise
  that a change reaches a running run cannot otherwise be checked from the run. A failed reload
  keeps the last set and emits `channel_config_read_failed` — decryption sits on every group
  reception, and memory answers during an outage (§6), so unlike webhooks nothing is read per
  event.
- **Replay and receive-only.** A replay decrypts (pure over captured bytes) and records only with
  `--persist-replay`. Receive-only mode decrypts and records normally and refuses posts.
- **Not in this change:** bot channel hooks and sends (§7 Bots), channel webhook triggers,
  `GRP_DATA`, posting from the command line (a CLI process cannot reach a running run's radio, and
  unlike a room post a channel post has no stored-row delivery loop), per-identity membership, and
  region-scoped floods.

### Webhooks

Change `webhook-notifications`. Tells systems outside sighop — Discord, n8n, Home Assistant —
when something worth an operator's attention happens, without anyone watching the panel.

- **Triggers.** `new_repeater` and `new_companion`: a verified advert that **creates** a
  contact of node type repeater or chat. "New" is the contact store's own `created`, so a key
  heard before, restored from the database or pasted in by an operator is not new, and a
  flood copy of the same advert raises nothing. Room servers, sensors and undefined node
  types raise nothing. The dispatcher is a contact-store listener of its own beside the bot
  host; the store isolates listeners, so a raising bot host cannot starve webhooks.
- **Formats.** `json` is sighop's documented event, `schema: 1`: `event`, `event_id` (fixed
  at the moment of the sighting, identical across retries and webhooks), `occurred_at`,
  `test`, `node` (`public_key` hex, `node_hash` as two hex digits — always the first key byte,
  `hash` as the key's leading `hash_size` bytes in hex, `hash_size`, `name`, `node_type` as a
  lower-case name, `position` as `{latitude, longitude, map_url}` or null) and `reception`
  (`hop_count`, `snr_db`, `rssi_dbm`,
  `received_at`, each null when unknown, and `path`: a list of `{hash, name, matches}` hops in
  travel order, empty for a zero-hop reception). Fields may be added within a schema version;
  never removed or repurposed. `discord` is one embed showing the node hash at its heard size,
  the full 64-hex public key as inline code, the location, and the path (`heard directly` when
  empty, cut on a hop boundary with `…` at Discord's 1024-character field limit); every
  advert-derived string, including contact names in the path, is markdown-escaped and
  `allowed_mentions` is empty, because advert names are written by strangers. **The two formats
  do not carry the same fields:** the embed shows no `snr_db` — link quality of a first sighting
  is for a machine, and stays in `json`, which never loses a field within schema 1.
- **Location.** Both formats build the same
  `https://www.google.com/maps/search/?api=1&query=<lat>,<lon>` from the advert's own
  coordinates, formatted to six decimals — the wire's 1e-6° precision, fixed-point so a small
  value cannot reach a URL as `5.9e-05`. `json` carries it as `position.map_url`, present only
  when a position is; Discord's `Location` field is always there, a masked link labelled with
  the coordinates or `not advertised`. Coordinates are numbers sighop formats, not advert text,
  so they are not escaped — but they are self-declared and shown as heard, like the name.
- **Hash size and path.** The hash size is the advert packet's path hash size (1–3 bytes, from
  its path length byte); a zero-hop advert declares 1 byte and is not inferred larger. Each hop
  is resolved against the contact store once, when the event is raised, so every retry and
  webhook names the same hops: the advertising node itself is excluded, one named match shows
  its name, one unnamed match its key prefix, none `<unknown>` and several `<ambiguous>`.
- **Best-effort delivery, off the reception path.** The listener builds an event and offers it
  to a bounded queue (64, drop-oldest, counted). One task reads the queue and starts a task
  per matching webhook; each POSTs with the standard library in a worker thread (no runtime
  HTTP dependency), 10 s timeout, no redirects followed, up to 4 attempts at 2 s, 10 s and
  60 s, retrying connection failures, timeouts, `5xx` and `429` (waiting at least
  `Retry-After`, capped at 300 s). A semaphore of 4 is held only for an attempt, never across a
  backoff, so one webhook in backoff delays no other. Pending events are not persisted: a
  restart loses them. Each webhook's last success and last failure with its reason are
  written without waiting, and a failed write is only logged.
- **Configuration is read at event time.** The enabled webhooks are read from the database
  when an event is taken off the queue, so `sighop webhook …` in another process and the panel
  both apply to the next event without a restart. A failed read delivers to the last good list
  and is logged. A row whose URL does not open under `SIGHOP_SECRET_KEY` is reported and
  skipped; the others proceed.
- **The URL is never shown after it is stored.** Every surface shows scheme and host only; the
  URL is read from standard input on the command line and from a password field in the panel,
  never re-filled after a refusal, and never in a log event.
- **No database, no webhooks; a replay, no webhooks.** A replayed reception carries an earlier
  session's timestamps, so announcing it would be false — whatever `--persist-replay` says.
  Receive-only mode does not affect webhooks: they are not transmissions. A run with a
  database but no usable secret sends none and says so at startup.
- **First run.** A node with an empty contact table announces every repeater and companion it
  hears within a day. `max_hops` narrows it (an unknown hop count passes only a webhook with
  no limit), and Discord's own rate limit arrives as `429`. No seeding step is needed: every
  restored contact is already not new.
- **Later: `new_chatter`.** A new trigger member, its own event source calling
  `WebhookDispatcher.offer`, and a renderer case — no schema change and no dispatcher change.
- **SSRF.** Only signed-in operators configure webhooks, and they already control the host;
  schemes are limited to `http`/`https` and redirects are not followed. Not otherwise
  mitigated.

### Repeater collection

Change `repeater-metrics-collection`. sighop as a **guest client** of the repeaters around it:
everywhere else it answers, and here it logs in, asks, and records (`net/collect.py`).

- **Blank guest password, nothing else.** Stock repeater firmware compares a blank login
  first against its ACL and then against `guest_password`, which is empty by default, so a
  blank login is admitted as a guest (`simple_repeater/MyMesh.cpp:90-143`). A guest may ask
  for `GET_STATUS` (`:216`, "guests can also access this now") and `GET_NEIGHBOURS` (`:276`,
  no admin check). A refused login is **silence**, not a code, so a repeater with a guest
  password set and one out of range look the same: the outcome is `login_unanswered`. No
  other password is configurable or sent.
- **Selection and recency.** Only repeaters an operator ticked on the contact table, whose
  advert was last heard within the recency window (default 3 days), are polled. A selected
  repeater outside it is skipped without a record and polled again once heard.
- **One exchange at a time.** Repeaters are polled sequentially; each poll is login →
  status → neighbour pages (prefix 6 bytes, 11 entries a page into the firmware's 130-byte
  buffer, at most 8 pages), each step waiting for its answer. So an answer is matched against
  exactly one outstanding request: decrypted under the one shared secret of the login identity
  and the repeater asked — never a candidate trial — and, for status and neighbours, only if
  its first four bytes echo that request's timestamp (`:214`). A login answer carries the
  repeater's clock rather than an echo, so it is matched by MAC and shape. Anything else is
  counted unmatched and dropped.
- **Timestamps strictly increase per repeater**, because the firmware drops a login or request
  whose timestamp is not above the last it saw from that client. The next value is
  `max(now, last + 1)`; not persisted, since cycles are minutes apart.
- **Log in every poll.** Guest entries live only in the repeater's RAM (32 by default) and are
  evicted; reusing a session fails silently after a reboot and costs a timeout to discover.
- **Routing.** Only a route keyed by the repeater's public key is used directly; a node-hash
  route is ignored for a flood. A flooded request is answered by a path return bundling the
  `RESPONSE`: `PathBodyReader` adopts the route and hands the bundle to the collector, so the
  next request goes direct. Each step's timeout is the peer ACK formula over the larger of the
  request's and the reply's airtime, plus the firmware's 300 ms response delay.
- **Flooded answers.** A repeater with no route to us answers a direct request by flood, and
  learns a route only from a `PATH` we send. The first live run showed every answer flooded
  and requests sent into the re-floods lost. So after an answer heard by flood the collector
  waits `max(3 s, 2 × 3.6 × airtime)` and then sends a direct path return with the route the
  flood took, as stock clients do (`BaseChatMesh::handleReturnPathRetry`); the repeater keeps it
  in its guest entry and answers direct from then on.
- **Yields and gates.** Submissions are `ADVERT`, the lowest class. A cycle that finds
  transmission disabled sends and records nothing and is retried an interval later; a
  submission the scheduler drops (a budget-stalled one expires) or suppresses ends the poll as
  `not_sent` with the scheduler's reason.
- **Settings are read from the database every 60 s**, with the last good read kept on a
  failure, so the system page's changes apply without a restart or a reconcile call. Cycles
  run inside that tick, so two never overlap; disabling mid-cycle stops before the next
  repeater. A missing or room-serving login identity skips the cycle with a note the system
  page shows. Pruning runs at each cycle's end and at least daily, also while disabled.
- **Live runs only.** A replay builds no collector, whatever the stored settings say.
- **Firmware variance.** `RepeaterStats` is 56 bytes; the room server's `ServerStats` shares
  its first 48 and diverges after `n_flood_dups`. A shorter answer ending at `n_flood_dups`
  parses with the two receive counters absent — though a decrypted body is block-padded, so in
  practice older firmware reads zeros there rather than absent.

---

## 8. WebUI

All four areas confirmed in scope. Priority order for building:

1. **Admin & config** — CRUD virtual entities, key management, radio settings, enable/disable bots, room passwords and retention.
2. **Observability dashboard** — live packet feed (WebSocket), airtime and duty-cycle usage against budget, per-entity TX/RX counters, contact table with learned paths, SNR/RSSI, modem health.
3. **Room browsing** — message history from Postgres, per-room member lists.
4. **Chat client** — send DMs and channel messages as any companion entity. This makes sighop usable without a handheld and is the strongest argument for the project. Channels (change `channel-messaging`) are listed beside direct conversations with unread indication and read without choosing an identity; posting requires choosing one, and the Public composer says the post is flooded to the whole mesh. A received sender is a claimed name, never a verified identity, and the conversation says names are not authenticated. The same channel list on `/chat` administers them: it adds hashtag and pre-shared-key channels, re-adds Public, lists stored channels this run could not load, and removes a channel through confirm-and-nonce stating how many messages go with it. Conversations with a contact are started from the contact list, one link per loaded identity.

The **system page** carries the repeater collection settings (§7): enabled, the login identity
(stored identities not serving a room), interval, recency and retention windows, validated
strictly — junk or out-of-range values are refused with the reason and nothing is stored. It
states that collection uses a blank guest password, how many repeaters are selected and how
many are in the window now, the last cycle, and that no poll is sent while transmission is
disabled. The **contact table** gives repeater rows a "collect metrics" checkbox (htmx, stored
at once), the last poll's time and outcome, a skipped-for-recency marker, and a link to the
repeater's **metrics page**: the latest status with its time (a field not returned is "not
reported", counters as reported, not rates), its neighbours attributed to a contact only when
exactly one key starts with the prefix, and up to 200 polls of history, newest first.

### Design direction: instrument panel

sighop is a radio operator's tool, and the UI should read as one — dense, technical, and
high in information density, closer to a spectrum analyser or a TNC front-end than to a
SaaS dashboard. Concretely:

- **Live state is the centre of gravity.** Duty-cycle usage against the ceiling, channel
  busy, noise floor, TX queue depth and the packet feed are always visible, not buried a
  click deep. The duty-cycle meter is the single most important element on the screen —
  it is the limit operators will actually hit, and on EU 868 it is a legal one.
- **Monospace and tabular.** Node hashes, paths, SNR/RSSI and packet hex are all
  fixed-width data. Let them align.
- **Colour carries meaning, not decoration.** Reserve it for budget state, verification
  status, and TX/RX direction.
- **Never present unverified data as verified.** Channel sender names (no authentication
  at all — §5) and unsigned advert content must be visually distinct from cryptographically
  verified identities. This is a hard rule, not a stylistic preference.
- Dark-first, since it will sit on a screen alongside other radio tooling.

The chat client is the exception: inside a conversation, ordinary readable chat typography
wins over instrumentation.

### Authentication

The WebUI controls radio transmission and holds private keys, so it ships with real auth
rather than assuming a protective network. **As built in milestone 9:**

- **Accounts live in the database** — a `web_user` table (§6, migration `0005`): a
  username stored normalised (NFKC, then `casefold()`, so uniqueness is case-insensitive
  without `citext`), an Argon2id hash with its parameters, an enabled flag, and
  `password_set_at`, which doubles as the credential epoch. `run --web` therefore requires a
  database, and refuses at startup — before any socket exists — without one, or when the
  database holds accounts and none is enabled, naming `web user enable` and `web user add`.
  A database with *no account at all* is served in first-run setup instead (below). A run
  without `--web` needs neither.
- **Accounts are managed from a terminal, except the first**: `sighop web user
  add|list|passwd|disable|enable|remove`. Passwords come from a prompt (asked twice) or
  standard input, never argv — the rule room passwords already follow — and disabling or
  removing the last enabled account needs `--allow-no-accounts`; removing the only account
  says the next `run --web` will offer first-run setup. The browser offers none of it (see
  below) beyond creating that first account.
- **First-run setup** (change `web-first-run-setup`). When `web_user` is empty at startup,
  `run --web` serves the panel anyway, and the only thing it offers is `GET`/`POST /setup`:
  a one-time code, a username and a password entered twice. The code is 20 Crockford base32
  characters (100 bits, no I/L/O/U, shown `XXXXX-XXXXX-XXXXX-XXXXX`, accepted in any case
  and without separators), generated at startup and printed **only in the run's own
  output** — so `docker compose logs sighop` shows it — never in an event, a page or the
  database; the startup event says `setup_pending: true` and nothing more. A wrong code is
  refused with one fixed sentence before any hashing, compared in constant time, and is
  **not throttled**: at 100 bits guessing is hopeless at any rate the process can serve,
  and in compose every client arrives from the Docker gateway address, so a per-client
  delay would be a global lockout anyone on the network could trigger. The account is
  created by `WebUserRepository.add_first` — `LOCK TABLE web_user IN SHARE ROW EXCLUSIVE
  MODE`, count, insert only into an empty table, one transaction — which serialises two
  setups and a racing terminal `web user add` alike, so setup never creates an account
  beside another. Success signs that browser in exactly as `/login` does and closes setup;
  so does any account appearing from a terminal. The code dies at completion or restart,
  and a restart prints a new one. Setup is decided at startup from the *total* count, not
  the enabled count: a database whose accounts are all disabled was locked deliberately,
  and setup must not be a way around that. While setup is pending, a refused page request
  goes to `/setup` and `/login` redirects there; completion is also announced in the run's
  output.
- **Every page, form and the feed's WebSocket require a session, by default-deny.** The
  public set is fixed in `web/guard.py` — `GET`/`POST /login`, `GET`/`POST /setup` and
  `/static/` — and anything not named in it refuses a request with no session (`303` to
  the sign-in form, or the setup form while setup is pending, for a safe method, `401` otherwise; the WebSocket closes with `1008` before `accept()`, and also
  checks `Origin`). A test walks the route table to prove it. There is no option,
  environment variable or constructor argument that turns authentication off, on loopback
  or anywhere else — `create_app` requires an authenticator, and tests sign in through the
  production session store.
- **Sessions are in memory**, keyed by `sha256` of the cookie (the store never holds a
  presentable token), bounded at 256, ending after 12 h idle or 24 h absolute, at sign-out
  (a POST — there is no `GET /logout`), and at restart. Sign-in issues a new identifier
  whatever the browser held. Each session re-reads its account at most once a minute and
  ends when the row is gone, disabled, or its `password_set_at` moved — which is how a
  terminal `disable` or `passwd` reaches a running panel within a minute with no IPC. If
  that read fails because the database is degraded, pages keep working and every guarded
  action is refused with "this account cannot currently be verified".
- **Sign-in reveals nothing to a guesser.** One response, byte for byte, for an unknown
  user, a disabled account and a wrong password; exactly one Argon2id verification in each
  case (an unknown user verifies against a hash computed at startup); throttling by
  normalised username and by socket address, five free failures then `min(2^(n-5), 900)`
  seconds, refused *without* verifying, in LRU maps bounded at 4096 keys. The panel has its
  own `PasswordHasher`, so a burst of room logins cannot queue an operator's sign-in (peak
  Argon2id memory 4 × 64 MiB). `web_login` records the outcome and reason; the password
  never reaches an event.
- **The cookie is `sighop_session; HttpOnly; SameSite=Strict; Path=/` — and not `Secure`.**
  *(Corrected in milestone 9. This section said "secure cookie".)* sighop serves plain HTTP
  by operator decision and does not terminate TLS; a `Secure` cookie set over HTTP on
  anything but `localhost` is discarded by the browser, so sign-in would succeed on the
  server and loop back to the form — the kind of failure that gets "fixed" by removing the
  check. `__Host-` requires `Secure` and is unavailable for the same reason. `Strict` rather
  than `Lax` costs one thing — following a link to the panel from another site shows the
  sign-in form — and makes "GET never changes state" a second wall rather than the only one.

Three rules follow from what the UI can do:

- **The interface binds to loopback by default, and a wider bind is announced.** Any other
  address is permitted and is reported at startup — in the run's output and as its own
  logged event, with no option that suppresses it — as reachable from the network over
  plain HTTP, with passwords and session cookies unencrypted in transit, and the remedy (a
  tunnel). *(Milestone 8 announced "no authentication" here; that sentence is gone because
  it is no longer true, and the plain-HTTP one replaced it because it still is.)*
  `--web-allowed-host` names further host names the rebinding check accepts — a panel bound
  to `0.0.0.0` in a container is reached as `localhost:8080` — and extends, never replaces,
  the bind-derived set; a wildcard is refused at startup.
- **Request provenance is enforced alongside authentication, not instead of it.** A page on
  another origin can ride a signed-in browser, and a rebound name can make its script
  same-origin with the panel. Every state-changing request carries the *session's own*
  token (the sign-in and setup forms carry a per-process one, which is all a pre-session
  request can be bound to; another session's token is refused like none), and every request's `Host`
  must be one the interface answers to (milestone 8 design D9, milestone 9 design D6).
- **Actions that reveal or export a private key, enable transmit, or raise the duty-cycle
  ceiling are re-authenticated**: the confirmation carries the one-shot nonce *and* the
  acting user's password, verified against a fresh read of the account. A wrong or missing
  password refuses the action as its own event and counts toward the sign-in throttle, but
  does not end the session. A room post stays confirm-and-nonce: it is content, not a change
  to what the station may do, and a password prompt on every post would train operators to
  type it without reading. Every `web_request` and `web_guarded_action` event carries
  `actor` — the username, or `unauthenticated` — and `audit()` takes it as a required
  keyword with no default. Transmit and ceiling changes made in the browser are also printed
  in the run's own output, naming the account.
- **"Advert zero-hop now" and "advert flood now"** are confirm-and-nonce guarded actions
  per loaded identity, like a room post: an advert is an ordinary transmission by an
  identity the operator already runs, not a change to what the station may do. Each has its
  own nonce bound to action and identity, so a zero-hop confirmation cannot be spent as a
  flood or on another identity, and each is printed in the run's output naming the account.
  Unlike a room post, **both refuse rather than defer** — with nothing submitted — when the
  transmit gate is closed, when the board has not answered with its radio parameters, or when
  the identity is not loaded. A post is a stored row that is delivered once the gate opens;
  an advert is a packet, and a suppressed one is charged and gone — for a flood, also moving
  the identity's next scheduled one a day out with nothing on the air. A flood is further
  refused, stating the seconds remaining, while any flood from this run is inside the
  inter-entity gap (§4.3): the gap exists so local identities never burst together, and an
  operator clicking flood on three identities in a row is exactly that burst. There is no
  other cooldown.

Reverse-proxy trust is deliberately *not* supported in v1: forwarding headers are never read
for the client address, the throttle key or the cookie's attributes. It is a reasonable
deployment pattern, but "trust this header" is a footgun that turns one proxy
misconfiguration into a bypassed throttle, and it can be added later without disturbing
anything. Behind a proxy the per-address throttle degrades to a global one, which is the
safe direction.

### What the interface deliberately does not expose

The browser reaches every `sighop` capability an operator administers a node with, with five
exceptions. All are deliberate, all are stated *in the interface* at the point an operator
would look for them rather than only here, and none is a gap waiting to be closed by
whoever notices it first.

- **Applying a migration.** `sighop db upgrade` has no browser equivalent. §6 makes
  applying a migration an act an operator takes on purpose and never a side effect of
  starting something, and the interface has no roles — every account is an operator — so
  offering it here would make a stolen session enough to change the schema. The schema page
  shows the applied and expected revisions, says the two disagree when they do, gives the
  command that reconciles them, and says why the button is not there.
- **Managing accounts beyond the first** (milestone 9). `sighop web user` has no browser
  equivalent; first-run setup creates the first account once, into an empty table, and
  nothing else. With no roles, anyone signed in could create a second account for
  themselves, and a stolen session would become a credential that outlives it; terminal access to the host is the
  stronger proof of being the operator. "Change my own password" was the one safe subset
  and is also left out — it still lets a stolen session lock the owner out — and can be
  added later without disturbing anything. The schema page names the command and says why.
- **Generating the sealing secret.** `sighop keys secret` has no browser equivalent for a
  smaller reason: it prints a value once that must be kept and must never be regenerated —
  losing it makes every stored identity unrecoverable — and a browser is a poor place to
  hand somebody something they must not lose. The identities page says so.
- **Revealing a stored channel pre-shared key** (change `channel-messaging`). `sighop channel
  key` has no browser equivalent. The key is the credential for reading and posting in the
  channel, and a stolen session that could display it would hand out a channel for good — a key
  cannot be revoked from the people who already have it. The pre-shared key is entered in a
  password field, never re-filled after a refusal and never rendered; the channels page names the
  command and says why.
- **Removing a stored identity** (change `create-entity-with-known-key`). `sighop keys delete`
  has no browser equivalent. Disabling is the reversible action the panel offers and it covers
  the ordinary case; removal cannot be undone, and it is refused outright for an identity a room
  or a bot is bound to because `room.entity_id` and `bot.entity_id` cascade — deleting the
  identity would take that room's members and its whole history with it. An irreversible act
  belongs where the other one already lives. The identities page names the command and says why.

`capture`, `monitor` and `run` are not candidates at all and the reasoning is worth
recording: `run` *is* the process serving the panel, and `capture` and `monitor` are
offline tools against a serial device a running platform already holds open.

**A room post is a stored row, not a transmission.** `sighop room post` is
`MessageRepository.store` plus one length check: it does not go through `RoomServer`, and
whichever run is serving that room picks the row up through its own push loop. The browser
posts through the same one call, which means three things an operator can be surprised by,
and the confirmation says all three: delivery happens after the reference implementation's
hold rather than immediately; a closed transmit gate does not refuse the post, it delays
what goes on the air; and a post to a room *this* run does not serve is stored and
delivered by nobody until a run that loads that room's identity is started. None of that is
new behaviour — it is how the command has worked since milestone 6. The panel is simply the
first surface where somebody might expect otherwise, because it is showing them a running
platform at the time.

---

## 9. Logging

Per `logging-best-practices`: structlog, JSON, one context-rich wide event per unit of work,
two levels (`info` / `error`), no unstructured strings.

The unit of work is not an HTTP request. sighop has four:

| Unit | Emitted when |
|---|---|
| **Packet RX** | A frame is decoded, deduplicated, and dispatched |
| **Packet TX** | `TxDone` resolves, or the packet is dropped/expired |
| **Entity action** | An entity completes a logical action (greet sent, login processed, history synced) |
| **HTTP/WS request** | A WebUI request completes |

Shared context on every event: `service`, `version`, `commit_hash`, `instance_id`,
`event_type`, `duration_ms`, `outcome`.

A packet RX event carries: `packet_id`, `route_type`, `payload_type`, `path_len`, `path`,
`size_bytes`, `snr`, `rssi`, `dup` (bool), `src_hash`, `matched_entities`,
`decrypt_outcome`, `airtime_ms`.

A packet TX event carries: `packet_id`, `entity_id`, `entity_name`, `entity_type`,
`priority_class`, `queue_wait_ms`, `airtime_ms`, `budget_remaining_pct`, `attempt`,
`tx_result`, `acked`.

`packet_id` is generated at ingress or submission and threaded through everything that
touches the packet — including the entity action it triggers and the reply it produces. It
is the join key that makes "why did this member never get their message" answerable, and it
is the single most valuable field in the schema.

Business context here means mesh context: entity name and type, room name, member identity,
contact name. "Room server *skogen* failed to sync 47 messages to *sigurs* because the
airtime budget was exhausted" — not "sync failed".

Webhook delivery (§7) emits `webhook_event_raised` (trigger, node hash, `event_id`),
`webhook_delivered` (webhook name, `url_host`, status, attempts, `duration_ms`),
`webhook_attempt_failed` (status or error, next delay), `webhook_abandoned`,
`webhook_dropped`, `webhook_config_read_failed`, `webhook_url_unsealable`,
`webhook_outcome_record_failed` and `webhook_test_sent`. Only `url_host` ever appears — never
a URL's path or query, which is where its token lives.

Channels (§7) report as typed events that `monitor/render.py` formats: `channel_message_received`
(channel, `packet_id`, hop count, SNR, and the claimed name only as `unverified_sender_name`),
`channel_unknown` and `channel_undecryptable` (channel hash, `packet_id`),
`channel_unsupported_text` (channel, text type), `channel_post_submitted` (channel, identity,
post id, `actor` when from the panel), `channel_post_refused` (reason),
`channel_post_resolved` (outcome and the scheduler's reason), `channel_repeat_heard` (post id,
hop count, SNR, running count), `channel_config_read_failed`, and `channel_set_changed` (the
names added and removed, and how many are loaded) for a reload that adopted a different set — a
reload that changes nothing says nothing. No event, log line, error or page ever carries a
pre-shared key.

---

## 10. Container and deployment

**As built in milestone 9** — `Dockerfile`, `.dockerignore`, `compose.yaml`, `build.sh` —
with the compose file reduced to one service against an external database by change
`compose-external-database`, and the command line removed by change
`require-database-web-drop-cli`: `sighop` is now an entry point that takes no arguments,
reads every setting from the environment, requires a database and always serves the web
interface. The paragraphs after this list are the original intent; where the build departs
from them it says so here.

- **The image.** Two stages on the same digest-pinned `python:3.13-alpine` (musl), `uv`
  copied from a pinned `ghcr.io/astral-sh/uv` image. `uv sync --locked --no-dev
  --no-install-project` (the cached dependency layer), then the source and `uv sync --locked
  --no-dev --no-editable`, bytecode compiled at build. **`--locked`, not `--frozen`**:
  `--frozen` installs from a stale lock without complaint, and a lock that disagrees with
  `pyproject.toml` must fail the build (it does — verified). The final stage holds
  `/app/.venv`, `alembic/` and `alembic.ini` — one `COPY` of `/app`, byte-compiled and
  `chmod -R a+rX` in the build stage, no `RUN` — sets `SIGHOP_ALEMBIC_DIR`, `PATH`,
  `PYTHONDONTWRITEBYTECODE`, `PYTHONUNBUFFERED`, **no `USER`**,
  `ENTRYPOINT ["sighop"]` with no `CMD` (the node takes no arguments), OCI version/revision labels and
  `SIGHOP_COMMIT_HASH` from build arguments — so every event from a container names its
  build (verified: `"version": "0.1.0", "commit_hash": …`). The image adds no compiler, no
  `uv` and no package installer to its base, and no `tests/`, `captures/`, `keys/`, `.env*` or `.git` — `.dockerignore` is an allowlist
  (`*`, then `!src !alembic !alembic.ini !pyproject.toml !uv.lock`), so nothing arrives by
  someone forgetting to list it. The migration chain is in the image, so starting it
  against a database behind the code migrates that database with no checkout mounted.
- **Alpine, not slim.** Built first on `python:3.13-slim-trixie` (307 MB, 56 HIGH/CRITICAL
  Debian-package findings), then switched by operator decision to `python:3.13-alpine`
  (200 MB, 7 HIGH, all `libuuid`, no CRITICAL). Every native dependency ships a musl wheel,
  so nothing compiles. The test suite runs on glibc, so `build.sh` has a `replay` gate that
  renders every committed capture with `python -m sighop.replay` on the host and inside the
  image and requires identical output. **Nothing the base ships is deleted.** An earlier version removed `pip` and `apk`
  in the final stage: that saves no bytes, since they stay in the base layers, and it hides
  them from the scan while still shipping them — removing `/lib/apk/db` too made the scan
  report zero OS findings, a blind scan that looked clean. The base's `pip` and `apk` stay,
  reported by the scan, and cannot install anything under a read-only root as a non-root
  user.
- **Deviation: a base with a shell, not Wolfi.** §10 below says "no shell utilities
  beyond what the runtime needs". Wolfi/Chainguard `python` would honour that, and was
  rejected: its free tier publishes `latest` only, so an unrelated rebuild would move the
  interpreter to 3.14 under a lock resolved for 3.13. Alpine keeps busybox's shell and
  applets; the compensating controls are the deployment's read-only root, all
  capabilities dropped and `no-new-privileges` (milestone 9 design D13).
- **The deployment.** `sighop` runs as `user: "${UID}:${GID}"` with `group_add:
  ["${DIALOUT_GID}"]` — **numeric**, because a group *name* resolves against the image's
  `/etc/group`, not the host's, and because the name differs by distribution: on the
  development host (Arch) serial devices belong to `uucp` (GID 984), not `dialout`. The
  variable keeps §10's name; its comment gives `stat -c %g /dev/serial/by-id/…` as the way
  to find the value. The modem is mapped by `/dev/serial/by-id/…` to `/dev/modem` in the
  long `devices:` syntax, because by-id names carry colons (the V4's USB serial is its MAC
  address) and the short form splits on them. `read_only`, a `/tmp` tmpfs, `cap_drop: [ALL]`,
  `no-new-privileges`, `init: true` (signal forwarding: `docker compose stop` is the same
  graceful stop as Ctrl-C, exit 0), `restart: unless-stopped`, `stop_grace_period: 20s`,
  `json-file` log rotation. The panel is published on
  `${SIGHOP_WEB_BIND:-127.0.0.1}:${SIGHOP_WEB_PORT:-8080}`, with `SIGHOP_WEB_ALLOWED_HOSTS`
  naming `localhost` and `127.0.0.1` on the *published* port — the one a browser's `Host`
  header carries; milestone 9 hardcoded `:8080` there, which broke whenever
  `SIGHOP_WEB_PORT` was set — plus `${SIGHOP_WEB_ALLOWED_HOST}` for one more name, such as a
  reverse proxy's. The list is comma-separated, because an environment variable cannot
  repeat the way the `--web-allowed-host` flag it replaced could. Compose cannot drop an
  entry whose variable is unset, so the third defaults to `localhost:<port>` and
  `validate_allowed_hosts` discards the duplicate: the defaults answer to exactly the
  loopback pair, as before. The service has no `command:`; every setting is in
  `environment:`. Inside the container the panel binds `0.0.0.0`,
  so the plain-HTTP warning always prints there — correctly, since whether the published
  port is host loopback is compose's decision, not something the process can see.
  `UID`, `GID`, `DIALOUT_GID` and `SIGHOP_MODEM` are `${VAR:?reason}`: compose refuses to
  start and names the missing one.
- **One service, and migration on start** (operator decisions). Starting `sighop` applies
  outstanding migrations before the schema-version check and emits `database_migrated` with
  the revision before and after — unconditionally, in and out of a container, since change
  `require-database-web-drop-cli` removed the `--migrate` opt-in along with every other flag.
  Only a database *behind* is moved; one ahead of the image is refused as anywhere else, so
  restarting an older image after a newer one migrated still fails loudly. This replaced a
  separate `migrate` service under a profile, judged too complicated for a single-container
  deployment where starting the new image *is* the deploy.
- **The database is external, on every host** (change `compose-external-database`,
  operator decision). Milestone 9 shipped a digest-pinned `postgres:17-trixie` beside
  `sighop` on an `internal: true` network with a named volume and a `pg_isready`
  healthcheck; development then needed a `compose.dev.yaml` override that reset the
  environment, dependency and networks to reach the shared dev database instead. Production
  uses an external database too, so the bundled service served neither, and both it and the
  override are gone. Compose runs no database, creates no volume and declares no networks:
  `sighop` sits on the project's default bridge, which routes to an off-host database.
  Restricting who reaches that database is its own host's job. The file carries no `name:`,
  so it is byte-identical on every host and the project name is the checkout directory's
  unless `COMPOSE_PROJECT_NAME` says otherwise.
- **Secrets come from `.env`** (gitignored, operator decision), and so does everything else
  that differs per host: `DATABASE_URL` and `SIGHOP_SECRET_KEY` are both `${VAR:?…}` in
  `environment:`, so compose refuses to start naming the unset one. **Interpolation, not
  `env_file:`** — `env_file:` does not refuse a missing key, and would copy `UID`,
  `SIGHOP_MODEM` and `COMPOSE_*` into the container too. Compose reads only `./.env`;
  `.env.dev` keeps its role for `uv run --env-file`, so a development host repeats the two
  lines. The values are visible to `docker inspect`, i.e. to the `docker` group, which is
  root-equivalent anyway.
  The account is created through first-run setup at `http://localhost:8080/setup`, with the
  code from `docker compose logs sighop`, and the password never touches the compose file,
  the environment or shell history. **It is the only account**: the `web user` commands that
  added more, changed a password or disabled one went with the command line, and nothing
  replaces them yet (change `require-database-web-drop-cli`, an accepted loss to be closed by
  a follow-up that adds account management to the panel).
- **Outbound HTTPS to webhook hosts.** The container needs to reach whatever hosts the
  operator's webhooks name (Discord, an n8n instance, Home Assistant); nothing inbound is
  added. The default bridge already routes there; a host with egress filtering has to allow
  them.
- **No healthcheck on `sighop`**, deliberately: the image has no HTTP client, every panel
  route requires a session, and an unauthenticated `/healthz` would be the first public
  route that reflects platform state. A probe of `/login` would prove the web server
  answers, not that the radio is alive — a false green for the failure that matters. A fatal
  error exits, and `restart` handles that.
- **`build.sh`** runs `lock` (`uv lock --check`), `lint`, `types`, `test`, `image`,
  `smoke`, `replay` and `scan`, stopping at the first failure and naming it. `smoke` starts the image
  as UID 52037 on a read-only root with every capability dropped and no network, twice: once
  to import the whole application (`sighop.boot`, `sighop.web.app`, `sighop.runtime`,
  `sighop.replay`) as a stranger, and once running the real entry point with an empty
  environment, which must exit non-zero naming how to generate `SIGHOP_SECRET_KEY`. `test`
  needs a database — the suite refuses to run without one — named by
  `SIGHOP_TEST_DATABASE_URL` or `DATABASE_URL`, or failing both by the gitignored `.env.dev`. `scan` feeds a `docker save` tarball
  to a digest-pinned `aquasec/trivy` container — never the Docker socket, which would hand a
  third-party image root on the host — and **prints** every HIGH/CRITICAL finding, fixed or
  not, without failing the build; only a scan that cannot run fails the gate. That is an
  operator decision taken when the newest `python:3.13-slim` digest (the base then) carried 12 fixable
  Debian-package findings and no image could be built; distroless was measured and not
  adopted (6 fixable findings of its own, patchable only by Google's rebuild, and `User=0`
  in its config). A `.trivyignore` entry must sit under a `#` comment giving the reason, or
  the build fails before scanning. The version comes from `uv
  version`, the commit from git with `-dirty` on a modified tree; the image is tagged
  `sighop:<version>-<commit>` and `sighop:local`, and never pushed by the script. Images are
  built without provenance or SBOM attestations, so each is a single manifest.
- **CI** (`.github/workflows/build.yml`) runs only the image gate, `./build.sh image`, on
  every pull request, push to `master` and manual run; the other gates run locally with
  `./build.sh` and are not repeated in CI, to save CI time (change ci-image-build-only), so
  an image in GHCR is not by itself proof that they passed. On a push to `master` or a manual run it pushes the image to GHCR as
  `<commit12>-<YYYYMMDD>-<HHMMSS>` (UTC), posts the tag and digest to Discord through the
  `notify-discord` action in `Sigurs/container-rebuilds`, and deletes all but the newest
  three package versions — one push being one version is why attestations are off. Every
  action is pinned to a commit. No gate run in CI needs a database, so the job starts none.
  One pitfall found while
  building it: `set -e` does not apply inside a function run as an `if` condition, so every
  step in a gate ends in `|| return 1` — without that, the smoke gate passed an image that
  failed to start.

The original intent, kept for the reasoning:

Multi-stage build on Wolfi or `python:3.13-slim`, `uv sync --frozen` in the build stage,
only the venv and application in the final image. No build toolchain, no shell utilities
beyond what the runtime needs.

**UID and device access.** The spec asked for arbitrary-UID operation; direct device
passthrough puts pressure on that, since `/dev/ttyUSB0` is normally `dialout`-gated. The
resolution:

- The image hardcodes **no** UID. Application files are world-readable, nothing is written
  inside the image, and no path assumes a specific home directory.
- The deployment supplies both: `user: "${UID}:${GID}"` plus `group_add: [dialout]` (or the
  host's numeric `dialout` GID) in compose.
- The device is mapped by stable path, e.g.
  `devices: ["/dev/serial/by-id/usb-...:/dev/modem"]`, so the container always sees the
  same node name regardless of host enumeration order.

The container therefore runs as an arbitrary UID with a supplementary group for the device —
the intent of the requirement is preserved without loosening host permissions.

Also: `read_only: true` root filesystem with an explicit tmpfs, `cap_drop: [ALL]`,
`no-new-privileges: true`, and Postgres on an internal network with no published port.

Secrets (`SIGHOP_SECRET_KEY`, DB password) come from the environment — in compose, the
gitignored `.env` — never baked into the image or committed.

The build script (`build.sh`) covers: lint, typecheck, test, image build, and a
vulnerability scan of the result.

### Upgrading past `0008` (the private key format)

The one upgrade in this project's history that is not "deploy the new image". Migration
`0008` renames `entity.sealed_seed` to `sealed_private_key`, and **every identity stored
before it stops opening**: the ciphertext holds a 32-byte seed, which is no longer a format
this system reads (§6, change `create-entity-with-known-key`). Nothing is deleted — the rows
and any keyfiles stay exactly as they are — but a run will refuse the identities it finds.

**Step 1 happens before the upgrade and cannot be done afterwards.** On the *current* build
— one old enough to still have the command line — `sighop keys export` every identity worth
keeping. What that writes is a version 1 keyfile
holding a seed, which the new build will not read; save it anyway, because step 3 needs it.

**Step 2, deploy.** `0008` runs without `SIGHOP_SECRET_KEY` — it reads no key material — and
reports how many rows it strands. Expect that to be every row that existed.

**Step 3, carry each identity forward.** Expand its saved seed into a private key *outside
sighop*: `sha512(seed)`, then `[0] &= 248; [31] &= 63; [31] |= 64`. Feed the resulting 128
hex characters to the panel's create-identity form, in its private key field. The public key
and node hash come out unchanged, so no peer has to be told anything and no contact list
needs editing. The stranded row holds that public key, so remove it first from the
identity's page in the panel, which needs no secret to remove a row it cannot open.

That expansion is deliberately not a command. An operator performing it once, knowingly, on
material they already hold is a different thing from this system reading seeds — which it
does not, anywhere. An identity whose seed was not saved in step 1 cannot be carried forward
at all: recreate it, and every peer re-adds it under a new public key.

**Rolling back.** `alembic downgrade` reverses the rename and nothing else. Identities
created *after* the upgrade are unreadable by the previous build, and identities stranded by
step 2 become readable again — so a rollback taken before step 3 loses nothing, and after it
is a roll-forward or a restore.

---

## 11. Repository layout

```
sighop/
├── src/sighop/
│   ├── radio/          kiss transport, modem (rx + tx), probe, capture, replay
│   ├── protocol/       packet codec, payloads, crypto
│   ├── net/            rx.py (decode stage), dedup.py, paths.py, bus.py,
│   │                   tx.py (scheduler), airtime.py, adverts.py,
│   │                   contacts.py, dm.py (direct messages, both directions),
│   │                   room.py (the room server), acks.py, pathbodies.py,
│   │                   channels.py (channel set, receive, post, repeat registry),
│   │                   collect.py (repeater collection: guest login and polls)
│   ├── monitor/        render.py (pure formatting of the node's own output)
│   ├── keystore.py     entity keyfile documents (the panel's import and export) —
│   │                   file formats, so not under protocol/
│   ├── runtime.py      the whole pipeline wired together
│   ├── boot.py         the entry point: environment check, migrations,
│   │                   persistence, panel, run — takes no arguments
│   ├── replay.py       `python -m sighop.replay <capture>`, the build's parity
│   │                   harness — not a command line, and not a node
│   ├── bots/           base.py (the Bot protocol and BotContext),
│   │                   runtime.py (dispatch, limits, mode, state),
│   │                   drivers.py (the registry), greeter.py
│   ├── webhooks/       triggers.py, config.py (the stored-configuration rules),
│   │                   events.py, render.py (json and discord bodies),
│   │                   transport.py (one stdlib POST), dispatcher.py (queue,
│   │                   retries, sample sends)
│   ├── db/             models.py (the eighteen tables), repositories.py (what net/
│   │                   calls), engine.py (pool, bounds, degraded state, probe),
│   │                   writer.py (bounded write-behind), sealing.py (keys and
│   │                   webhook URLs at rest), packetlog.py (feed rows, pruner),
│   │                   persistence.py (the wiring), migrations.py (alembic)
│   ├── web/            state.py (the read seam as Protocols), app.py (the
│   │                   application and the bound socket), auth.py (accounts
│   │                   protocol, sessions, throttle, sign-in and
│   │                   re-authentication), guard.py (host check, session,
│   │                   default-deny public paths, provenance token, one wide
│   │                   event per request with its actor),
│   │                   guarded.py (confirm-then-act and its audit event),
│   │                   feed.py (one bus subscription, per-connection queues),
│   │                   chat.py (this run's own conversations and channel
│   │                   logs), routes/chat.py (direct and channel conversations,
│   │                   and channel administration), routes/admin.py (writes and
│   │                   confirmations), routes/system.py (readback, schema,
│   │                   the station's gate controls and collection settings),
│   │                   routes/collect.py (contact-table selection, metrics page),
│   │                   serialize.py, render.py (view models), deps.py,
│   │                   routes/ (session.py: sign-in and sign-out), templates/,
│   │                   static/ (vendored htmx, no bundler)
│   ├── logging.py      structlog config, wide-event helpers
│   └── config.py       every setting, from the environment and nowhere else —
│                       validated, password-redacted, every problem reported at
│                       once. No dotenv dependency: `uv run --env-file` and
│                       compose already read the file
├── alembic/            async env.py (design D1) and one migration per milestone
├── alembic.ini         no URL in it — config.py is the single source
├── tests/
├── compose.yaml        sighop, configured entirely in environment:, external DATABASE_URL
├── build.sh            lock, lint, types, test (needs a database), image, smoke, replay,
│                       scan (scan reports only)
├── .github/workflows/  build.yml: `build.sh image` only (no other gate, no Postgres);
│                       push, Discord, keep-3 on main
├── Dockerfile          two stages, digest-pinned python:3.13-alpine, no USER
├── .dockerignore       an allowlist
├── .trivyignore        suppressions, each with its reason (none today)
├── .devcontainer/      the development container — nothing here ships:
│                       devcontainer.json, its glibc Dockerfile (uv pinned to the
│                       release image's digest, node, openspec, claude, the
│                       context engine), the host script that resolves this
│                       machine's radios, post-create, the forward that puts the
│                       host's Ollama on the container's localhost, and the two
│                       gitignored agent config files it shadows
├── .claude/hooks/      cce.sh — the context engine's hook, resolved under $HOME
│                       instead of one machine's absolute paths, so the tracked
│                       .claude/settings.json works on the host, in the dev
│                       container, and on a machine with no engine at all
└── pyproject.toml
```

`protocol/` must have no dependency on `db/` or `net/` — it is pure functions over bytes.
That is what makes it testable against captured packets, and it is the layer where
correctness matters most. Since milestone 5 `db/` actually exists, so
`tests/protocol/test_import_boundary.py` asserts the direction explicitly — naming
`sqlalchemy`, `asyncpg` and `alembic` as well as `sighop.db` — rather than relying on the
package not being there to import. **`db/` is a peer of `net/`, not a layer beneath
`protocol/`**: it may import from `net/`, and `net/contacts.py` and `net/paths.py` import
no SQLAlchemy at all. Each takes a sink whose `offer` never awaits and never raises, which
is what keeps the persistent path a thin adapter rather than a rewrite. The node always
passes one — a database is required — while a unit test of the store alone may pass none.

`keystore.py` sits at the top level rather than in `protocol/` for the same reason: reading a
keyfile is I/O, and `protocol/` has none. The document → identity step stays in
`protocol/identity.py`, so the boundary test keeps passing and the split is the one the layer
rule already implies.

**There is no `entities/` package, and the sketch above has been corrected to say so.** It
was to hold the room server, the companion and the bot runtime. The room server ended up in
`net/room.py`, because it is a bus subscriber that needs the messenger's routing, the
acknowledgement registry and the path store, and putting it a package away would have meant
either duplicating those or importing across a boundary that describes nothing. The bot
runtime lives in `bots/`, a peer of `net/` that imports from it exactly as `db/` does.

**Milestone 8 settled the last occupant, and it needed no module.** §7's companion — "an
addressable identity driven by a human in the WebUI" — turned out to be a *user interface*
over behaviour that already existed: the send path is `DirectMessenger.send` (milestone 4),
the receive path is `MessageReceived` (milestone 4), and the identity is an `entity` row of
chat node type. The chat surface picks one and sends as it. That is the opposite of what §7
implied — it read as a new entity *behaviour* — and it is why `entities/` still does not
exist and now has no expected tenant at all (milestone 8, design D6).

`web/` is the second renderer and inherits `monitor/`'s rule unchanged: `net/` never imports
it, and nothing in it is on the reception path. It imports nothing from `runtime.py` and
`runtime.py` imports nothing from it — the state a page reads is described by `Protocol`s in
`web/state.py` that `Runtime` satisfies structurally, and `boot.py` is the only module in the
project that knows both sides (milestone 8, design D2).

`bots/` never imports `monitor/`, for the same reason `net/` does not: it emits typed events
and `monitor/render.py` turns one into a line. It imports no SQLAlchemy either — its storage
seam is read-only-property `Protocol`s the way the room server's is, which is what keeps
every dispatch, limit, mode and gate test a unit test over a stand-in store.

`net/dm.py` handles inbound direct messages as a **bus subscriber**, never inside `net/rx.py`.
Decryption needs local keys and a contact table; the decode stage stays a pure function of one
frame, which is what keeps a replayed capture reproducing every reception exactly. It reports
its work as typed events that `monitor/render.py` formats, so `net/` never imports `monitor/`.
`net/channels.py` follows the same rule for group text, and reaches storage the way `dm.py`
does — through record sinks and a loader callable — so it imports nothing from `db/` or `web/`.

`radio/replay.py` is the inverse of `radio/capture.py` and lives beside it deliberately:
it re-hydrates a capture file into the same event stream the modem produces, so the live
decode path can be driven offline — which is all `sighop/replay.py`, the build's parity
harness, does with it. `monitor/` is a separate top-level package rather than
part of `net/` because rendering is not networking — and keeping `render.py` a set of pure
functions is what makes the output testable by string comparison.

---

## 12. Milestones

Ordered to exploit the fact that real hardware and a live mesh are available from day one.

0. **Capture.** KISS transport and enough of `Modem` to open the serial link, plus a
   `sighop capture` command that dumps raw frames with RxMeta and timestamps to a file.
   Run it overnight against the live mesh. **Receive-only; nothing transmits.**
   *Done:* two overnight captures (152 frames, 9h16m, graceful stop; and 199 frames,
   ~7h37m, no graceful-stop event but every JSONL line well-formed), both recorded on the
   **Heltec V3**. Both runs: zero reconnects, zero unparsed/malformed frames, zero
   Data/RxMeta correlation anomalies. (The recordings were removed from the repository by
   change `synthetic-corpus`, which replaced the recorded corpus with a generated one; see
   `tests/protocol/CORPUS.md`.)
1. **Protocol core.** Packet codec, crypto, advert parse/verify — developed against the real
   captured frames from milestone 0, not synthetic fixtures. Pure functions over bytes; no
   radio, no database. The capture file becomes the permanent regression corpus.
   *The corpus's one hard limit, and where it stood:* every encrypted payload recorded
   through milestone 3 was addressed to a third party, so the corpus proved framing, adverts
   and signature verification but **could not prove decryption**. Milestone 4 closed that for
   exactly one exchange — a pair of direct messages whose key the repository held — and for no
   other ciphertext, which stayed verifiable only by round-trip and by fixed known-answer
   vectors against the firmware source.
   *The recorded corpus was not frozen:* milestone 2's live session added 91 frames, taking it
   to 442 frames across five files, and milestone 3's long receive-only run added 555 frames,
   taking it to **997 frames across six files**. A session was appended when it carried a
   shape the corpus lacked — the first brought the first `TRANSPORT_FLOOD` frame and the
   first CONTROL payloads, the second a located CHAT advert (`0x91`), a 10-byte TRACE, and the
   duplicate-timing tail that sized the dedup TTL — and was appended whole, because a corpus of
   hand-picked interesting frames stops being a sample of the mesh.
   *Superseded by change `synthetic-corpus`.* The recorded corpus held other people's node
   names, keys and Public-channel chat, and a private key, and about a third of its receptions
   were flood repeats of a packet already seen. It was replaced by a generated corpus
   (`tests/corpus/`, `tests/protocol/synthetic.py`) built from sighop's own encoders under a
   fixed seed, with a declared amount of repetition and an explicit account of what it does
   not prove. What the recording measured about the mesh — the flood repetition rate, the late
   echo and the retransmission that sized the dedup TTL — carries no identity and is kept
   below, under *Recorded history: flood repetition*.
2. **Live decode.** Point the decoder at the live link. Adverts, names, paths and SNR
   printing in real time. *This is where the design is proven or isn't* — and it is still
   entirely receive-only.
   *Done:* `sighop monitor` (live or `--replay`), the stateless `net/rx.py` decode stage,
   `radio/replay.py`, and a `SetHardware` request/response API on `Modem` with a startup
   capability probe. Both `capture` and `monitor --capture` now write the `capture_meta`
   header described below, built from that probe. Three findings from the live runs, none
   of which the offline corpus could have produced:
   - **The first `SetRadio` can be sent into a booting board and lost.** The modem answered
     nothing for ~2 s and then answered every probe sub-command within 19 ms, so the
     handshake retries within a budget instead of asking once. Milestone 0 never saw this
     because its handshake waited indefinitely for `OK`.
   - **Do not "fix" DTR/RTS.** Deasserting them before opening looks like the careful thing
     to do — those lines drive the ESP32 auto-reset circuit — and it is measurably the
     opposite: with `dtr`/`rts` deasserted the board reset 0.52 s after every open,
     reproducibly, while pyserial's defaults reset it not at all. The circuit fires on a
     difference between the two lines, so the transition is what matters, not the level.
   - **A reboot is invisible without reading the boot banner.** The USB bridge is a separate
     chip and stays enumerated across an ESP32 reset, so no disconnect is reported; the
     firmware persists no radio configuration (`handleSetRadio` writes a runtime struct), so
     the board silently reverts to its build defaults. The `ESP-ROM:` banner arriving on the
     KISS stream is therefore treated as a reconnect: re-handshake and re-probe.
   - **The V3 is faulty**, which took a while to establish because every symptom pointed at
     software first. It power-cycles on a **75.07 s timer** (`rst:0x1 (POWERON)`, measured
     repeatedly), invariant across all four DTR/RTS combinations, both USB ports, with and
     without sighop, with kernel USB autosuspend disabled (`runtime_suspended_time` 0) and
     with nothing else holding the tty. Swapping to the V4 gave three minutes with **zero**
     resets. Hardware; no software change addresses it, and the reboot handling above only
     keeps sighop honest about it.
     *Reading those logs correctly:* the `serial_disconnected` events were a **consequence**
     of the board resetting, not a USB fault — the CP2102 stayed enumerated throughout
     (`active_duration` == `connected_duration` across many resets).
   - **Board-agnosticism is now observed rather than argued.** The same unmodified code
     decoded live traffic from the **Heltec V3** behind a CP2102 bridge and from a **Heltec
     V4** on the ESP32-S3's native USB (`/dev/ttyACM0`, `USB_JTAG_serial_debug_unit`). The
     V4 reported its name as `Heltec V4 OLED`, exactly the runtime-chosen string §4.1
     predicted, which is why device name is a probe result and never a constant. The two
     present *different* failure modes on reset, though: the CP2102 stays enumerated while a
     native-USB board disappears from the bus entirely.
   - **`GetSensors` answers with an empty payload on both boards** — supported, zero bytes.
     §4.1's warning against parsing it against a fixed schema stands, and is cheap to honour.
   - **The reconnect loop could spin.** A device that is present but immediately reports
     EOF reconnects instantly, and the backoff only applied *after* a failed connect —
     seven reconnects in 60 ms, observed. The backoff now applies before every attempt.
   - **The UART loses bytes occasionally.** One frame in a 3-minute run arrived with type
     byte `0x80` and no leading `0x00`, followed 1 ms later by its orphaned `RxMeta` — a
     `Data` frame that lost a byte between the ESP32 and the CP2102, where 8N1 has no error
     detection. Reported as an unparsed frame with its raw bytes, which is exactly the
     §4.1 rule working. Not worked around: masking the KISS port nibble would accept a
     frame we know to be damaged.
   - **`GetSensors` answers on the V3 with an empty CayenneLPP payload** (0 bytes), with
     `GetBattery` and `GetMCUTemp` answering normally. §4.1's build-flag expectation holds:
     the shape is not fixed, and nothing may parse it against a schema.
   - **The overnight V4 session grew the corpus**, and this is the milestone's substantive
     protocol finding. 91 receptions over 15 hours, zero decode failures, zero unreadable
     lines, 22 adverts all verifying — and two shapes the 351-frame corpus never held: one
     **`ROUTE_TYPE_TRANSPORT_FLOOD`** advert (transport codes `0x0075`/`0x0000`) and six
     **CONTROL** payloads in three lengths, two of them carrying a repeater's public key
     inline. Both had been synthetic-only since milestone 1; both now round-trip against
     recorded air. `TRANSPORT_DIRECT` and RAW_CUSTOM remain unsighted.
   - **Repetition rate is not a property of the mesh.** The same firmware-style duplicate
     count that gave 41.6% repeats over the milestone 0 nights gives **11.0%** over this one
     (81 distinct packets in 91 receptions, at most 2 copies, max spread 4.6 s). Milestone 3
     sizes its dedup cache from its own measurement, not from either figure. The direction is
     stable, though: every repeat was a flood reception, and no direct reception repeated.
3. **Bus and scheduler.** RX fan-out with dedup, TX scheduler with airtime budget and the
   duty-cycle ceiling under test. Transmit still disabled; verify the budget accounting
   against what *would* have been sent.
   *Done:* `net/airtime.py`, `net/dedup.py`, `net/paths.py`, `net/bus.py`, `net/tx.py`,
   `net/adverts.py`, `runtime.py` and `sighop run` (live or `--replay`). The modem gained its
   transmit path — `Data` submission with `TxDone`/`TxBusy` correlation — built and tested now
   so milestone 4 is a flag flip rather than new code written on the air. The gate stayed
   closed throughout, and that is asserted rather than asserted-in-prose: `radio/` has exactly
   one `Data` send, and a test drives every priority class under load with transmit disabled
   and checks that nothing reaches the transport. Findings:
   - **The deaf-window table was wrong, and the board proved it.** §4.3's figures assumed an
     8-symbol preamble; the firmware uses 32 at SF ≤ 8. `GetAirtime` (0x0F) — which nothing
     had asked for until now — returns the firmware's own estimate, and it agrees with the
     corrected computation to **sub-millisecond** at 16/64/128/255 B on the V4. That is a
     cross-check against an independent implementation of the one number the duty-cycle
     ceiling rests on, for the cost of four queries at startup.
   - **This section's own "order of magnitude" claim was also wrong.** The EU narrow and
     legacy 250 kHz/SF11 presets are within ~7% per packet. Corrected in §4.3, and kept as a
     test because it is counter-intuitive enough to be re-derived wrongly.
   - **Learning paths only from flood packets discards the best evidence there is.** MeshCore's
     zero-hop adverts arrive as `DIRECT` with an empty path — direct RF contact, the most
     useful route a node can have. Replaying one capture learned 2 destinations under the
     flood-only rule and 5 once empty-path DIRECT receptions counted. A DIRECT packet *with* a
     path is still ignored: that route was someone else's choice, not a route back.
   - **Duty cycle is cheap to test properly.** With an injected clock a simulated 24 hours of
     sustained overload runs in 0.7 s, and asserts the regulation's own sentence: no 3600 s
     interval carries more than 360 s. Offered 664 s/hour, it transmitted 323 s/hour and
     dropped the rest on their deadlines.
   - **Repetition rate, measured over the whole 442-frame corpus:** 35.7% of receptions were
     duplicates, and the widest gap between copies was **31.1 s** — six times the 4.6 s the
     milestone 2 session suggested. Per-file it ranges from 5.4% to 45.7%, which is the same
     lesson as before: repetition is a property of the session, not of the mesh.
   - **The sliding window only becomes observable after an hour on the air, and the long V4
     session is what showed it.** Across 2 h 54 min receive-only (2026-09-04 17:06–20:00 UTC,
     the long receive-only capture), with a 15-minute advert override on two stub entities so the
     scheduler carried real load, remaining budget fell 99.70% → 98.22% over the first six
     adverts and then **held at 98.22% for the remaining ten**: charges ageing out of the
     window at exactly the rate new ones entered it. That plateau is the sliding window
     working, and it is not reachable in a short run — the 2.5-minute session before it saw
     only the monotonic decline, which a leaky bucket would have produced too. Peak load was
     **6.40 s in any 3600 s, 0.178% duty against a 10% ceiling**. All 16 adverts were charged
     and suppressed at the closed gate, none dropped, none busy, longest queue wait **0.46
     ms**, every one admitted on its first attempt. The inter-entity gap deferred the second
     entity 16 times, by 80–530 s.
   - **The dedup TTL is bounded from both sides, which nothing before this run showed.** The
     session saw 555 receptions, 33.3% of them duplicates, a median gap between copies of
     0.99 s — and two outliers that turn out to be different phenomena. One is a flood copy of
     an ANON_REQ arriving **200.7 s** late by a *different* path (`23` against `be`, SNR
     −10.25 against 14.25): a real late echo, and six times the 31.1 s the 442-frame corpus
     called its worst case. The other is a pair of **byte-for-byte identical** zero-hop DIRECT
     TXT_MSG frames **3158 s (52.6 min)** apart — same ciphertext, same path, same SNR, which
     is not a copy of one transmission but the sender **retransmitting an unacked DM**. So the
     TTL cannot simply be raised for safety: shorter than ~200 s it discards genuine flood
     copies, longer than ~50 min it starts silently swallowing real retries, which are events
     a user is entitled to see. 300 s sits inside that window with **1.5× margin over the
     worst real duplicate**, not the ten times the corpus alone implied. Peak occupancy was 49
     entries against the 4096 cap, unchanged. Both defaults stand; the argument for the TTL
     is replaced, and §13 is corrected.
   - **CONTROL is ordinary traffic, not a curiosity.** The corpus held six CONTROL frames and
     the milestone 2 finding treated them as a rarity; this session alone carried **162**, 154
     of them at 38 bytes. Nothing decodes them and nothing should — they are preserved
     uninterpreted, which is now a path taken by 29% of receptions rather than by six frames.
   - **555 receptions, zero decode failures, zero unparsed frames, all 17 adverts verifying.**
     The session also brought three shapes the corpus lacked: advert flags **`0x91`** — a
     **CHAT node carrying a location**, where all seven chat adverts recorded before it were
     `0x81`, named with no location, so the two bits had never been seen set together on a
     non-repeater — a **10-byte TRACE** against the corpus's 13 and 21, and two new nodes.
     Appended whole, per this section's rule.
4. **First transmit.** Enable TX. One hardcoded companion entity exchanges a DM with the
   second board, running stock MeshCore firmware as the reference peer. The first packet
   sighop puts on the air should be a deliberate, watched event. A dedicated peer board
   (rather than the live mesh) keeps this repeatable and keeps our debugging off other
   people's networks. **Exit criterion: the first successful decrypt of a real MeshCore DM**
   — this is the first point at which the cipher, MAC and ECDH are confirmed against another
   implementation rather than against ourselves.
   *Done, 2026-09-04 21:44 UTC.* `keystore.py`, `net/contacts.py` and `net/dm.py`, with
   `sighop keys` and `--entity/--peer/--send/--allow-flood` on `sighop run`. The exit
   criterion was met and was a **committed regression test**, not a log line, for as long as
   the recorded corpus stood: the recorded exchange and a burned key that opened it let a test
   decrypt a foreign implementation's ciphertext on every commit, which removed the corpus's
   "one hard limit" for exactly that exchange. Change `synthetic-corpus` withdrew that test, the
   recording and the burned key, because they were real data; the exchange existed and
   passed once, and `tests/protocol/test_corpus_decrypt.py` is now the self-consistency
   successor.
   The exercise ran as D13's runbook: key injected over the peer's USB link, so sighop's first
   RF transmission was the DM itself and not an advert timer's side effect. Findings:
   - **The first transmission worked on the first attempt, and every construction matched.**
     A 54-byte zero-hop `DIRECT` TXT_MSG; the peer displayed it, and its acknowledgement
     `2b03574b` was byte-identical to the checksum sighop had computed before sending. The
     peer's DM back decrypted first try — `candidates_tried=1` — and the acknowledgement
     sighop computed for it, `1df0f21a`, is exactly the value the peer reported as its
     `expected_ack` and accepted as delivery. Both halves of `BaseChatMesh.cpp`'s ACK
     construction are now confirmed against another implementation, in both directions.
   - **The 6-byte acknowledgement is real, and the tail is what the source says.** The peer
     answered with `2b03574b0091`: checksum, then the extended attempt byte `0x00`, then a
     random `0x91`. Reading only the first 4 bytes (`:740`) is what makes it match. This is
     the first time the 6-byte form has been seen acknowledging something sighop sent.
   - **The retry formula is right and generous.** Predicted acknowledgement window
     `500 + (6 x 640 + 250) = 4590 ms`; measured latency **2481 ms**, so no retry ever fired.
     The peer reported its own `suggested_timeout` as 4782 ms for an equally sized packet,
     which brackets our 4590 ms — `SEND_TIMEOUT_BASE_MILLIS` 500, `DIRECT_SEND_PERHOP_FACTOR`
     6 and `DIRECT_SEND_PERHOP_EXTRA_MILLIS` 250 match this build (§13 open question 1
     answered). The 192 ms difference is the two boards' own airtime estimates disagreeing by
     ~5%, not the constants.
   - **Milestone 3's `TxDone` timeout factor met a real transmission and had room to spare.**
     Hand-off to `TxDone` took **1343 ms** for 640 ms of airtime, and 1336 ms for a 247 ms
     ACK: roughly 700–1100 ms of CSMA before the radio keys, against a timeout of
     `2 x airtime + 6 s`. The fixed 6 s term is what that overhead needed; the factor alone
     would have been marginal.
   - **The peer answers direct, not flood-scoped** (§13 open questions 2 and 5 answered).
     Injecting the contact with `out_path` empty and length 0 was what bought that, and it
     matters more than it looks: after sighop's own zero-hop advert, the peer re-learned the
     contact with `out_path_len = -1` (**unknown**), because a zero-hop advert carries no
     path. A node that learns us only from an advert will therefore **flood** its replies.
     D2's choice to inject over USB rather than advert first is what kept this exercise off
     other people's repeaters, and the reason is now measured rather than argued.
   - **A one-shot advert queued before the radio readback arrived, and was dropped.** The
     scheduler refused to compute airtime without a `GetRadio` answer and dropped the packet
     with `no_radio_readback` — the correct refusal, reported rather than silent, but the
     ordering was wrong: nothing may be queued before startup has adopted the board's own
     parameters. Fixed by gating the one-shot paths on startup completion, with a regression
     test. A `--send` had survived the same bug only by accident, having waited for its peer's
     advert in the meantime.

     **Seen a second time, on the reactive side, and fixed for the class rather than the
     instance.** Gating the *one-shot* paths on startup left every path that answers a
     *reception* ungated: `_consume` is started as a sibling of the task that awaits the probe,
     so frames the modem buffered while the database opened arrive before the parameters do. A
     stock companion messaged sighop during the `create-entity-with-known-key` exercise and its
     acknowledgement was dropped with the same refusal. The rule that came out of it, and that
     the next reactive path inherits: **anything composed in reaction to a reception waits for
     the readback within a bounded budget before it is refused** —
     `net/readback.py::wait_for_readback`, watching a signal `Runtime.set_radio` sets and clears
     so a reconnect is covered too. The refusal survives for a board that answers nothing, and
     says which case it is. Waiting costs only the waiting subscriber: `bus.py::subscribe` gives
     each its own task and queue. `_post_ack_window` stays outside the rule on purpose — it
     estimates *someone else's* window rather than pricing a transmission of ours, so it
     degrades instead of waiting.

     **Confirmed on the air, 2026-09-18**, V4 KISS modem against the same stock V3 companion
     ([redacted]) that found the bug, with sighop's adverts suppressed for the exercise and the
     companion's contact given a zero-hop path so nothing flooded. Both halves of the rule were
     exercised by varying one number — how long startup was held before it adopted the readback:
     - **Held 4 s, message at T+2 s.** Received at `16:48:24.450` with `radio` still `None`; the
       acknowledgement waited ~2.2 s, was submitted the moment the readback landed and reached
       the air at `16:48:27.436` (`origin=ack`, `queue_wait_ms=749.8`, `airtime_ms=246.8`). The
       companion reported the message **delivered**, `trip_time=3518 ms`. Under the old code this
       is the frame that produced `ack_not_routed` and nothing else.
     - **Held 8 s — deliberately past the 3 s budget.** The refusal arrived instead, naming its
       own case: `no GetRadio readback after waiting 3s for the board`. The message was still
       reported, honestly, as `acknowledged=False`, and the run kept receiving.

     `adverts_sent=0` in every run.
   - **Contacts being in-memory has an operational consequence worth stating.** The run that
     sends must hear the peer's advert *in that same run*; a restart forgets every contact.
     That is what `--peer-wait` exists for, and it is milestone 5's `contact` table that
     removes the need.
   - **Total cost on the air: 3 frames, 1.5 s, 0.2% of the hourly duty-cycle ceiling.** The
     session was appended whole to the recorded corpus, taking it to **1003 records — 1000
     received and 3 transmitted**, and bringing it its first frames sighop sent and its first
     decryptable payload (both since withdrawn with the recorded corpus).
5. **Persistence.** Postgres, models, Alembic, entity identity storage with key encryption.
   *Done:* `src/sighop/db/` (models, repositories, engine, bounded write-behind writer, seed
   sealing, packet-log feed and pruner, the `Persistence` wiring), `src/sighop/config.py`,
   `alembic/` with an async `env.py` and migration `0001`, and `sighop db` / `sighop keys
   secret|list|import|export`. Four of §6's eight tables, and `run` gained `--database-url`
   and `--persist-replay`. Findings:
   - **The database was probed before the design was written, and two of the readings changed
     the plan.** The role's `rolcreatedb` is **false**, so tests cannot create a throwaway
     database and isolate in a throwaway *schema* instead; and the server's `TimeZone` is
     **`Europe/Helsinki`**, which turns a stylistic preference about timestamp types into a
     correctness rule. `rolconnlimit` is 30, which is what sizes the pool at 10.
   - **`ON CONFLICT DO UPDATE` refuses a batch that proposes one key twice**, and a real batch
     does: `cannot affect row a second time`, observed against the dev database on the first
     corpus replay, where it discarded **every** contact and route write in the run while the
     packet log — which does not upsert — wrote 872 rows and looked healthy. The batch is now
     collapsed to one row per conflict key before the insert, keeping the latest, which is the
     same semantics D15 already stated for the recovery flush. Two regression tests hold it.
     Worth naming as a class: the failure was invisible in the counters that were being
     watched, and only showed up as *zero rows in two tables*.
   - **`NULLS NOT DISTINCT` is load-bearing for the `path` table.** A route keyed only by node
     hash has a NULL `dest_public_key`, and Postgres's default treatment of NULLs as distinct
     would have made every re-hearing insert a new row rather than re-confirm the one already
     there — the accumulation D12 exists to prevent, arriving through the back door.
   - **The bounded connect is not theoretical, and it was measured.** Against a proxy that
     accepts the TCP handshake and then says nothing — which is what a host that is *down*
     looks like, and is not what closing a listener looks like — a write failed in **5.003 s**,
     the configured bound, against asyncpg's own 60 s default. Detection of the outage took
     **15.0 s** end to end: the time until something wanted to write, plus that bound.
   - **Recovery detection is bounded by the backoff, and the backoff is the slow part.** Over a
     4-minute outage the probe interval walked 30 → 60 → 120 → 240 s, so a database that came
     back was noticed **222.8 s later** — by the probe, with no restart and no further advert,
     and the two contacts observed during the outage were then written carrying their *latest*
     state, one row each rather than one per observation. The mechanism does what D15 says;
     the measurement is that the capped backoff, not the fault, is what dominates the time
     spent reporting `degraded`. Left as designed, and recorded so the ceiling (300 s) is
     understood as the real bound rather than the 30 s default.
   - **Reception is unaffected by a database that is down, and that is now measured rather
     than argued.** Throughout the outage every peer stayed resolvable by name, contacts stayed
     addressable, and a replay of the whole corpus through a runtime whose database never
     answers reproduces the same delivered count, duplicate count, contact count and path count
     as a run with no database configured.
   - **Contacts survive a `kill -9`.** A live receive-only run was killed outright — no
     graceful stop, no flush, no dispose — and all four contacts it had heard were present on
     the next start, because they are written when the advert is observed rather than at
     shutdown. The restart reported `restored: entities=1 contacts=4 paths=4` **before the
     first frame arrived**, which is the operational difference milestone 4 asked for: the run
     that sends no longer has to hear the peer in that same run.
   - **The write rate is low enough that the retention default stands** (open question 2). A
     quiet live session recorded **101 rows/hour**, against the corpus-derived ≈191/hour — so
     the 100 000-row cap is 22–41 days either way, and is left unchanged. **Path candidates
     did not grow** (open question 1): 4 destinations, 4 rows, **1.00 candidate per
     destination**. Both samples are one quiet session and neither settles the busy-mesh case;
     they are recorded as not yet contradicting the defaults rather than as confirming them.
   - **§13 unknown #4 stays open.** The milestone's live work was receive-only and involved no
     peer exchange, so no opportunity arose to observe whether a node that learns us only from
     a zero-hop advert floods its replies. Durable contacts make it cheap to test whenever one
     does — the exchange no longer has to complete inside one process lifetime — and it is
     left open rather than closed on inference.
6. **Room server.** Login/ACL, history storage and sync, retention.
   *Offline work done; the live exercise is pending.* `src/sighop/net/room.py` (login, posts,
   the push loop, the request surface, retention), `src/sighop/net/acks.py` (the shared
   expectation registry), `src/sighop/net/pathbodies.py` (explicit path bodies and what they
   bundle), `src/sighop/passwords.py` (Argon2id off the loop, bounded), migration `0002` with
   its three tables and repositories, five new byte codecs in `protocol/payloads.py`, and the
   `sighop room` command surface. The exit criterion — a stock client logging in, posting,
   and after a restart receiving the history it missed — is the live exercise, and the
   runbook for it is written and reviewed. Findings the offline work produced, all of them
   from reading the firmware rather than the payload documentation:
   - **`docs/payloads.md` describes a room server that does not exist.** Four behaviours are
     not in it and all four are load-bearing: a failed login is answered with **silence**
     rather than an error, an existing member's *empty-password* login skips the replay check
     entirely (`MyMesh.cpp:335-342`), the read-only fallback is `PERM_ACL_GUEST` — **zero**,
     not `PERM_ACL_READ_ONLY` — and a post's text is capped near **150 bytes**, not 160,
     because the push body spends nine bytes before the text starts. Building from the
     documentation would have produced a server that answered strangers, refused legitimate
     re-logins, assigned a permission level the firmware never assigns, and stored posts it
     could not push. All four are recorded in §7.
   - **The room-server statistics struct diverges from the generic client parser, and both
     are right.** At offsets 48..52 a room server writes `n_posted` and `n_post_push`
     (`:175-176`); `meshcore_py`'s `parse_status` reads the same four bytes as a *repeater's*
     `rx_airtime`. We emit the room-server form, because interop is with the firmware — a
     client using the generic parser misreads those bytes against a stock room server exactly
     as it will against ours. Named in a constant with a test asserting **both** readings, so
     the divergence cannot later be "fixed" into a bug.
   - **CayenneLPP is the one big-endian encoding in this protocol.** Every other integer in
     MeshCore is little-endian; an LPP frame written with the project's usual byte order
     parses as garbage on the client and looks like a hardware fault. Costing a dozen lines
     rather than a dependency was the easy half of that decision; getting the byte order
     right was the half worth a test against a hand-built vector.
   - **Two components waiting on acknowledgements broke a counter's meaning.** `dm.py` owned
     a private expectation table and logged `ack_unmatched` for anything absent from it, so a
     room server waiting on push acknowledgements would have made every one of its matches
     look unmatched to the direct messenger, and vice versa. The table moved to `net/acks.py`
     with an owner per expectation; `unmatched` again means *nobody in this process was
     waiting for that*. The same table is the one place the acknowledgement-inside-a-`PATH`
     case has to reach, rather than two.
   - **A `PATH` body has to be decrypted for what is inside it, not for the route.** The
     firmware bundles an acknowledgement in a path return (`:601-620`): a client that answers
     a flooded push that way. Without decrypting the body the acknowledgement is invisible,
     the push is retried three times for nothing, and the member's cursor never advances —
     which is indistinguishable from a client that is not receiving. The route is the cheap
     half of that feature.
   - **A room server and the direct messenger would both have answered the same packet.**
     `DirectMessenger` filters inbound `TXT_MSG` by destination hash across every local
     entity, and a room-server entity is in that list: two decryptions, two acknowledgements
     on the air for one post, and two contradictory log lines. Resolved statically at wiring
     time — an entity a room server serves is skipped by the direct messenger and by the
     path-body reader — because the ambiguity is resolvable then, and a duplicate suppressed
     after the fact is still a second decryption of a message with a different meaning.
   - **A keep-alive answer is a 5-byte acknowledgement, which our own parser refuses.** The
     firmware appends the unsynced count to the ACK payload (`:574`), where `parse_ack`
     accepts the 4- and 6-byte forms the chat protocol uses. Nothing in this milestone
     receives one — being a *client* of someone else's room server is milestone 8's — so it
     is recorded rather than fixed, with the assertion that currently proves the refusal
     carrying the note. It is the one thing milestone 8 must add before it can keep-alive.
   - **The corpus is unchanged and says so mechanically.** Its 42 anonymous requests still
     parse as anonymous requests, none of them is mistaken for a login to one of our
     entities, and a replay with a room server wired in produces byte-identical delivered,
     duplicate, contact and path counts to one without. The reception path stayed a pure
     decode, which is what design D6 has been buying since milestone 2.
   - **The first live run overturned a design decision within minutes.** A stock client
     composed a **156-byte** post — past the 151 design D4 took from the firmware — and
     sighop refused it. The client showed it as undelivered and retried, and nothing on
     either side said why, because a refusal here *is* silence. Reading the firmware again
     for the case rather than the rule: the receive path has no length check at all,
     `addPost` runs and `send_ack = true` whatever the length (`:484-488`), and the
     acknowledgement is computed over the **full received** text (`:461-462`). D4 was
     revised to truncate and acknowledge over the text as sent, with the drop reported to
     the operator, and a local `sighop room post` still refused because *that* author is
     present to shorten it. "Refuse rather than corrupt" is right where a refusal can be
     *heard*, and this protocol has no way to say no.
   - **Then the limit itself turned out to be two conventions stacked on each other.** The
     first fix truncated at **150**, matching what stock firmware keeps —
     `StrHelper::strncpy(text, postData, MAX_POST_TEXT_LEN)` (`:57`) copies while
     `buf_sz > 1`, one below the 151 its own constant reads as, and that 151 is
     `MAX_TEXT_LEN` (160, ten cipher blocks chosen for *chat* messages, whose comment asks
     only that it stay under 177) less the push prefix. Neither number is a limit. The
     packet format allows **167**: 184 bytes of payload, less 4 for hashes and MAC, rounded
     down to 176 of ciphertext, less the 9-byte push prefix. And the client's own receive
     path allows exactly **156** — `queueMessage` bounds the frame the radio hands the phone
     app against `MAX_FRAME_SIZE` (176) and spends 20 bytes on prefix
     (`companion_radio/MyMesh.cpp:432`). 176 − 20 = 156, which is precisely the composer
     limit the exercise ran into: the same budget sizes both ends, so the number the client
     stopped at was derivable from the firmware all along. Storing 156 means nothing a stock
     client can compose is ever shortened, and the truncation rule guards only a range
     nothing stock reaches. The cost is byte-identity with a firmware-served room for posts
     of 151–156 bytes, and one extra cipher block on the air for posts above 150. Worth
     naming as a class: **an inherited constant is not a constraint**, and this one was
     copied through three files before anyone asked what enforced it.
7. **Greeter bot** and the bot plugin interface.
   *Offline work done; the live exercise is pending.* `src/sighop/bots/` — `base.py` (the
   `Bot` protocol, `BotContext`, the event and decision types), `runtime.py` (the bounded
   dispatch queue, the token bucket, the observe/active mode, durable state), `drivers.py`
   (the registry), `greeter.py` (the driver and its gates) — plus migration `0003` with
   `bot` and `bot_state` and their repositories, an observation listener on the contact
   store, the receiving entity on a `MessageReceived` report, and the `sighop bot` command
   surface. The exit criterion — a greeting transmitted to a genuinely unknown test peer,
   acknowledged, and no second greeting after a restart — is the live exercise, and the
   runbook for it is written and reviewed. Findings the offline work produced:
   - **§7's third handler could not be built, and saying so was the decision.**
     `on_channel_message` has been in the sketch since the beginning, and there is no channel
     key store and nothing in `net/` that decrypts `GRP_TXT` — so a hook declared on the
     protocol could never fire. Shipping it would have been a promise the runtime cannot
     keep, that a driver author would nonetheless write against. Two handlers, and the module
     says why the third is absent. **An interface is a claim about what the runtime will do**,
     and the cheapest place to be honest about a missing capability is the type.
   - **The trigger for a transmission had to be a type, not a check.** §7 rule 3 says greet
     only after verifying the signature, which as a rule is a line somebody has to remember
     to write. `AdvertEvent` is built from a `ContactObservation`, and `ContactStore` produces
     one only from a `VerifiedAdvert` — so there is no path along which an unverified advert
     reaches a driver, and no check to forget. The same shape milestone 1's design D7
     established for adverts, applied one layer out.
   - **"New" and "not yet greeted" are different questions, and the design asked the wrong
     one.** The gate started as *the advert created the contact* **and** *no greeting record
     exists*, which reads as belt and braces and is actually a filter that refuses the cases
     the greeter is for: a peer heard in milestone 2 can never be created again, so it could
     never be greeted; a peer skipped once because the rate limit was exhausted was refused
     forever by the same mechanism. Dropping `created` inverts the risk — a greeter created
     on an established node then owes the whole contact table — and the answer was to make
     that debt **data instead of a rule**: creation seeds a record for every existing
     contact, and `sighop bot greeted` releases them one at a time. Worth naming as a class:
     **a gate that also refuses what it was never meant to refuse is worse than a gate that
     needs an explicit initial state**, because the second is visible and the first is not.
   - **A rate limit discoverable only by attempting the send would have burned the record.**
     The greeting record is written before the transmission (design D6), so a limit that only
     answered when `send` was called would spend a contact's one chance on an action the
     limit was going to refuse. Splitting `can_send` from `send` is the room server's
     `check`/`spend` split arriving for a second, unrelated reason — which is the sign it was
     the right shape the first time.
   - **Observe mode has to spend from the rate limit or the dry run lies.** The whole point
     of the observe stage is to decide the limit from what the mesh actually does. A bucket
     that only drained when transmitting would make the dry run optimistic about exactly the
     burst §7 warns about, and the operator would set the limit from numbers that could not
     occur.
   - **A second bus subscriber would have re-created milestone 6's worst bug.** A bot
     subscribing for `TXT_MSG` would decrypt every inbound message a second time and, if it
     acknowledged, put two acknowledgements on the air for one packet. Bots consume the
     direct messenger's reports instead. And for adverts, a bot asking the contact store
     *afterwards* whether a contact was new would be racing the store's own subscriber, whose
     queue is independent — a race whose wrong branch greets a peer twice or not at all. The
     store gained a synchronous listener rather than the bot gaining a subscription.
   - **The hop gate is honest about what it does not do.** `max_hops` bounds *network*
     distance; a ducted or tropospheric path delivers a zero-hop advert from hundreds of
     kilometres away and no hop count separates that from a neighbour. `min_snr_db` is
     offered and defaults to null because a strong ducted signal defeats it too. The gate
     bounds how much of the mesh can trigger us, the once-ever rule bounds the damage when
     one gets through, and neither claims to identify a duct — written into the module rather
     than left for whoever reads the counters to work out.
   - **The corpus is unchanged and says so mechanically.** A replay with a bot wired in
     produces byte-identical considered, duplicate, contact and path counts to one without,
     and an observe-mode bot driving a driver that tries to send for *every* advert produces
     zero submissions across the whole replay. `protocol/` gained nothing: a greeting is an
     ordinary `TXT_MSG` composed by code that has existed since milestone 4.
   - **A greeting nobody could read looks exactly like a greeting nobody answered.** The
     exercise's one greeting was transmitted four times to a peer at zero hops with a +13 dB
     signal, and every attempt went unanswered. Nothing in this milestone's code was wrong:
     a direct message is encrypted under a secret derived from the **sender's** key, so the
     peer decrypts by trying the contacts it holds — and the dev greeter bot had been created
     eight minutes earlier with a 24 h flood interval, so it had never adverted and the peer
     held nothing for it. **Greeting a stranger is a two-packet problem and the design had
     modelled it as one.** The greeter now adverts first: zero-hop for a direct neighbour,
     and a flood only on a retry to a peer heard over a repeater, because that one is
     repeated by the whole mesh and is not spent on a guess. The advert is *awaited* — class
     3 against the message's class 2 means an un-awaited send is transmitted first and lands
     just as unreadable.
   - **Deferring the flood to the next cooldown reproduced the same failure at one hop.** The
     second exercise fixed the zero-hop case — two direct neighbours greeted and acknowledged
     — and then a peer at one hop was greeted bare, four times, `announced=0`, and never
     answered. The lazy-flood policy was working exactly as written; what was wrong was
     treating that silence as *information to sleep on*. It is not ambiguous: a peer that
     cannot decrypt us will not be able to in fifteen minutes either, and it is adverting
     right now, which is the one moment it is known to be awake and reachable. **The
     escalation belongs in the same reaction as the silence that justifies it** — flood
     advert, greet again, once, and only then hand over to the cooldown.
   - **Believing silence needed to cost something first.** The message path spends four
     attempts in about forty seconds and reports failure, but the escalation it triggers is a
     flood that the whole mesh pays to repeat. An acknowledgement returning over a longer path
     than the one we sent on is late rather than absent, so `send` gained an opt-in grace
     window (greeter default 30 s) that keeps the already-registered expectations armed past
     the last attempt. It buys listening, never a packet: **the cheap way to avoid a wrong
     transmission is to wait a little longer before deciding.** Opt-in because an interactive
     send returning half a minute after it had already failed would read as a hang.
   - **"Unacknowledged" had been recorded as a delivery, which made one silent failure
     permanent.** The original design D6 wrote the outcome and never retried, reasoning that
     an unacknowledged greeting may well have arrived and a duplicate is worse. The exercise
     showed the premise was false in the case that actually happens: the greeting had not
     arrived, could not have, and the record closed the contact forever. A restart then
     reported `already_greeted` — the exit criterion appearing to pass for the wrong reason,
     which is the part worth remembering. **A negative result that is indistinguishable from
     the positive one is not evidence**, and the exit criterion had to be sharpened to say
     the greeting must have been *acknowledged* before the restart proves anything.
   - **Two of the suite's own tests turned out to assert probabilistic properties as
     deterministic ones**, both from milestone 6 and both found only because this milestone's
     work ran the full suite many times. `test_no_corpus_frame_is_mistaken_for_a_login_to_one_of_our_entities`
     generates a fresh room-server key per run and asserts no corpus frame produces an event
     — measured at **16 failures in 300 runs**, because a 2-byte MAC false-matches at ~1 in
     2^16 per candidate and the corpus offers many. `test_a_source_over_its_own_limit_does_not_consume_the_global_budget`
     generates two peers and assumes distinct node hashes — measured at **0.38%**, which is
     1/256 exactly, and §3 says a byte collides at that rate. Both are the design's own rules
     showing up in tests written as if they did not apply.
   - *Pending the observe stage's completion:* what the live mesh's advert volume and hop
     distribution say about the default `max_hops` of 1, and whether the ducted-path case
     predicted above actually appears. The first attempt at this stage ran for three minutes
     against two nodes, which settles nothing.
8. **WebUI**, in the §8 priority order.
   *Done.* `src/sighop/web/` — `state.py` (the
   read seam), `app.py` (the application, the bound socket, the feed's WebSocket),
   `guard.py` (host check, provenance token, one wide event per request), `guarded.py`
   (confirm-then-act and its audit event), `feed.py` (one bus subscription, bounded
   per-connection queues), `chat.py`, `serialize.py`, `render.py`, `deps.py`, `routes/`,
   `templates/`, vendored `htmx.min.js` — plus migration `0004` with `direct_message` and
   its repository and refusing writer lane, a record sink on `DirectMessenger`, a bounded
   read on `PacketLogRepository`, a service slot and a traffic-watch hook on `Runtime`, and
   `--web`/`--web-host`/`--web-port` on `sighop run`. The exit criterion — a direct message
   sent, acknowledged, replied to and surviving a restart, the whole exchange driven from a
   browser — was met on 2026-09-12: a direct message composed in Chromium as the greeter identity,
   acknowledged after 1 attempt in 2324 ms, the stock peer's reply appearing in the same
   conversation without a reload, and both messages still there after a restart. Findings
   the offline work produced:
   - **§8's authentication rule described something the code does not do, and the fix was to
     correct §8.** "No unauthenticated mode, at any milestone" cannot survive shipping the
     WebUI in milestone 8 and authentication in milestone 9. Quietly contradicting it in the
     code would have left the document as the thing an operator trusts and the binary as the
     thing that behaves differently. §8 now says what is enforced: loopback by default, a
     wider bind permitted and **announced** in the output and in its own event with no way to
     silence it. **A rule a milestone cannot keep is a rule that has to be rewritten, not
     quietly broken.**
   - **The absence of authentication is what makes request provenance load-bearing.** Two
     attacks work against an unauthenticated loopback service with no further effort: any
     page in the operator's browser can POST to `127.0.0.1`, and a hostname an attacker
     controls can be resolved to it, making their JavaScript same-origin and able to *read*
     responses — key material included. So a token issued per process and present only in
     served pages guards every state-changing request, and every request's `Host` must be one
     the interface was configured for. It is not authentication and is not presented as one.
   - **This uvicorn has no `install_signal_handlers` flag, and `serve()` takes the signals
     anyway.** Design D1 asked for the flag; the installed version wraps `serve()` in
     `capture_signals()`, which calls `signal.signal` for SIGINT and SIGTERM and would
     replace the handlers `Runtime.install_signal_handlers` set — so Ctrl-C would have
     stopped the web server and left the run going. One subclass with that context manager
     made a no-op is the whole fix, and the process's signal handling is now byte-identical
     to a run without the interface.
   - **A port that cannot be bound had to be a *startup* failure, which meant binding before
     the run.** The socket is taken in `cli.py` before `Runtime.run()` is called, so a clash
     is reported before any traffic is processed and the run does not continue with a
     silently absent interface — the same posture milestone 5 established for a configured
     database that cannot be reached.
   - **The feed could not be built on the bus alone, and the reason is a real disagreement
     between two artifacts.** `web-dashboard` requires the feed to carry whether a record was
     a duplicate; `IngressPipeline.ingest` drops duplicates *before* fan-out, which is right —
     a duplicate is a copy the platform has decided not to act on twice. But **a display is
     not an actor**: 21.2% of the corpus is duplicates, and a feed built on the bus would have
     shown a busy mesh as quiet while the dedup counters on the same screen said otherwise.
     The pipeline gained one narrow observer, on the contact sink's contract, told about every
     reception including the dropped ones. D4's actual concern — that the bus's subscriber
     list must not grow with the number of browser tabs — is untouched: still exactly one
     subscription for the process.
   - **A send from a browser cannot be a request that waits for it.** `DirectMessenger.send`
     spends four attempts across several seconds; a POST that awaited it would be a browser
     that appeared to hang. The record is written at submission, the send runs as its own
     task, and the conversation shows `awaiting transmission` → `attempt N` → the outcome.
     That is only possible because the record is written *before* the transmission and updated
     in place — design D7's `UNIQUE (entity_public_key, ref)` earning its keep somewhere it
     was not designed for.
   - **A conversation needed a second home, not a cache.** Chat has to work with no database
     and during an outage of one (`web-chat`), so the panel keeps a bounded in-memory log fed
     by the same offer the durable sink gets, and says plainly when that is all there is. The
     messenger's single record sink became a list: a run can be both recording durably and
     showing a conversation in a browser, and those are two independent consumers of one
     offer rather than one wrapping the other.
   - **Milestone 6's keep-alive acknowledgement is still owed.** The 5-byte form
     `parse_ack` refuses was recorded then as "the one thing milestone 8 must add before it
     can keep-alive". It is not added: being a *client* of somebody else's room server is not
     one of §8's four areas, and building the parser without the client would have been
     untested code with no caller. Named again so the note is not lost a second time.
   - **The corpus is unchanged and says so mechanically.** A replay with the whole interface
     wired in — the feed hub subscribed and observing, a connection open and never drained,
     the record sink attached — produces byte-identical delivered, duplicate, considered,
     contact and path counts to one without. `protocol/` gained nothing.
   - **Repeated runs found a probabilistic assertion milestone 6 left behind.** Task 16.4
     asked for the class milestone 7 found twice;
     `test_no_corpus_frame_is_mistaken_for_a_login_to_one_of_our_entities` generated a
     room-server identity at random and then asserted that none of the corpus's 57 anonymous
     requests was addressed to it — false about 5% of the time, at 1 in 256 per frame. The
     identity is now chosen to avoid every destination hash in the corpus, so the test asserts
     what it means to assert. **A test that generates a key and then asserts something about
     its node hash is asserting something §3 makes occasionally false**, and the fix is always
     to fix the byte deliberately.

   What the live exercise overturned:

   - **The feed's status line is frozen the moment it connects.** `_stream` sends
     `connection.status()` once, before the first batch, and then only again when the *drop*
     count changes; live records go out as `kind: "records"` with no status beside them, and
     `feed.js` only rewrites the line on a `status` message. On a healthy connection — one
     that never drops anything — the line therefore reads `live — 0 record(s) shown` for the
     life of the page. A 58-minute browser session watched it sit at 0 while the table grew
     to fourteen rows, and the connection's own closing event recorded `delivered=11
     dropped=0`. Worse, `delivered` counts only what was streamed live, never the history the
     same page painted, so the number can never agree with the boundary row directly above it
     ("— live from here (N recorded record(s) below) —"). The runbook tells an operator to
     read this line as the health check; the healthy reading is indistinguishable from a dead
     feed. **The count a page shows must be a count of what that page is showing.**
   - **A replay run cannot be watched, and cannot be administered at all.** `--replay` ends
     when the capture is exhausted and the panel ends with it: the corpus's 555-frame session
     is consumed in about four seconds, and `run --replay … --web` against the 34-frame file
     the runbook names for the admin walk lives for *one second*. A browser opened on it does
     load — it painted the meter, the counters and the contact table, and its numbers matched
     the run's own status line exactly (50980 of 138750 considered, against `dup=36.7%`,
     `cache=20/4096`, `paths=12`, `contacts=5`) — but the feed's WebSocket never completes its
     handshake before the run exits, so the feed paints nothing. The reader is synchronous and
     shares the runtime's event loop, so while frames remain the panel is starved as well as
     short-lived. **The admin surface and the feed are only exercisable against a live link**,
     which is where both were exercised instead; the runbook's 18.1 and 18.3 commands are
     wrong as written.
   - **Reloading a confirmation page does not invalidate the one before it.** The runbook
     says a refusal can be produced by reloading a guarded page and submitting the stale form.
     It cannot: `NonceStore` holds up to `MAX_OUTSTANDING` nonces at once and `spend` only
     pops the one presented, so a reload mints a second valid nonce beside the first. Submitting
     the stale form *succeeded* and raised the airtime ceiling to 20%, above the regulatory
     default, with the gate still shut so nothing went out. The one-shot property itself is
     sound — presenting the same nonce twice is refused, 403, with its own `refused`
     `web_guarded_action` event — so this is a defect in the runbook, not in the guard. **A
     nonce that is spent once is not the same thing as a nonce that a reload retires**, and
     only the first is implemented.
   - **The chat page said channels were absent, and that was the one true statement it could
     make.** §8 item 4 promised channel messages from the start; the page shipped saying they
     were unsupported and why, while 85 corpus frames on the Public hash sat in the feed as
     ciphertext. Change `channel-messaging` replaced the sentence with the channels themselves
     (§7 Channels). Saying so was still better than a chat page that silently had no channels.
   - **The mesh was far quieter than the corpus's busiest session, so "keeps up" is still
     untested at volume.** Fourteen receptions in fifty-nine minutes, against the 555 in 2 h
     54 min that §3's dedup tail was measured over. Every connection of the session — five of
     them, the longest 58.5 minutes — closed with `dropped=0` and `incomplete=false`, and the
     browser's JS heap stayed between 1.2 and 3.7 MB with no console error but a missing
     favicon. **The design's first open question is answered only for a quiet band:** the
     256-record per-connection queue was never stressed, its deepest observed use being the
     eleven records one hour-long connection received, and the 500-row cap in `feed.js` was
     never approached. A busy-band session is still owed.
   - **The exercise's capture carries no shape the corpus lacks, so it was not appended.**
     The runbook expected "a first browser-driven direct message" to qualify, but this
     section's rule is about frame shapes — a `TRANSPORT_FLOOD`, a located CHAT advert, a
     10-byte TRACE — and a DM composed in a browser is byte-identical to one composed at the
     command line. The session produced `ADVERT`, `TXT_MSG` and `ACK` frames and one unparsed
     startup frame, all of which the corpus already holds, milestone 4's first-transmit
     session included. It stays at 1093 frames across eight files; `tests/protocol` and
     `test_corpus_pipeline.py` pass unchanged. **Where the operator's hand was is not a
     property of the bytes**, and the corpus is a sample of the mesh rather than of the
     project's own milestones.

   *What closing the write gap turned up (`webui-write-parity`).* The panel milestone 8
   shipped could watch everything and change almost nothing: eleven pages read, five wrote,
   and the five were chosen by what was easy to reach. Closing that produced five findings,
   the first of which is about this document's own record-keeping.
   - **Three of milestone 8's checkboxes claimed work that was never done, and the shape of
     the mistake is repeatable.** Tasks 12.1, 12.3 and 12.5 each bundled a list of verbs —
     "list, create, enable, disable, import, export" — and each named *one* test as its
     verification. The test passed, the box was checked, and create, import, export and the
     read-only-fallback control had never been built. The identities page went as far as
     stating what an exported keyfile *is* without offering one. **A task that lists six
     verbs and verifies one is a task that will be marked complete when it is one-sixth
     done**; the three are now annotated in place rather than quietly re-checked.
   - **Four rules were living in `cli.py` rather than in the code that owns them.** "This
     public key is already stored", "a bot adverts as a chat node", "a room runs on a
     room-server identity" and "a new greeter is seeded from the contacts already known"
     were all implemented in command handlers. A second surface reaching the same
     repositories is what exposed them: writing them again in a route would have been two
     implementations of one rule, so each moved to the repository or the driver that owns
     it. **The browser did not need them re-written; it needed them put where they
     belonged**, and the command line now gets its refusals from the same place.
   - **An export is dated from the row, not from the moment it is asked for.** A keyfile the
     panel serves carries the stored identity's `created_at`, so two exports of one identity
     are byte-identical. Dating it "now" would have put a field that is *about the identity*
     under the control of when somebody clicked.
   - **A replay run still cannot be administered, at any file size.** Milestone 8 found this
     with a 34-frame capture; it was retried here with a 132 MB one (1200× the corpus's
     longest session) and the run still outlived the first request and not the second. The
     exercise was done against the same database with the same `create_app`, served
     standalone — which is the honest way to walk the admin surface without a radio, and
     worth writing down before somebody inflates a capture file a third time.
   - **The two surfaces agree on the development database.** An identity created in the
     browser, exported, and read back by `sighop keys show` round-trips; a re-import is
     refused by both surfaces in the same words, naming the same row. A room and a bot
     created in the browser appear in `sighop room list` and `sighop bot list` with the
     read-only fallback the browser set; a greeting record cleared in the browser makes
     `sighop bot greeted` report that contact as one the bot will greet again. The
     browser-created greeter seeded the same six contacts `sighop bot create` would have.
   - **The exit criterion is met, and the read-only fallback is what made it possible.**
     Live against the Heltec V4 on 869.618 MHz: one zero-hop advert for the
     browser-created room server (116 B, 1164 ms), after which a stock MeshCore client
     added it and logged in. **The login was admitted as `guest` by the read-only
     fallback** — the flag set by a checkbox on the browser's create form, and the clause
     milestone 8 marked done without building. Without it that login would have been
     answered with silence, which is what the firmware does, and there would have been no
     member to deliver to. A post then composed and confirmed in Chromium was stored,
     pushed `DIRECT h0` in 377.856 ms and acknowledged by the client, checksum `b3d7923e`
     matching the push's own `expected_ack` — composed, confirmed, stored and delivered
     with no command line anywhere in the chain. Session cost: five transmissions, 0.8% of
     the hour.
   - **A store of six identities started cleanly, which is the panel's collision rule
     passing its real test.** Three of the six were created through the browser, and
     `generate_identity(avoid_node_hashes=…)` is fed the loaded *and* the stored hashes;
     they came up as `ac 05 7a 58 f4 4e`. The failure this prevents is not a bad row — it
     is a run that will not start, and it is only ever observed at startup.
   - **The exercise's capture was not appended to the corpus**, by milestone 8's own rule:
     ten frames of `ADVERT`, `TXT_MSG` and `ACK`, all shapes the corpus already holds.
     Where the operator's hand was is not a property of the bytes.
9. **Hardening.** Container, compose, build script, auth.
   *Done.* `web/auth.py` (accounts protocol, in-memory sessions, login throttle),
   `routes/session.py` and `login.html`, migration `0005` with `web_user` and its repository,
   the `sighop web user` noun, `--web-allowed-host` and `run --migrate`, a required `actor`
   on every request and guarded-action event, password re-entry on the four guarded actions —
   plus `Dockerfile`, `.dockerignore`, `compose.yaml`, `build.sh`, `.trivyignore` and the CI
   workflow. The exit criterion was met on 2026-09-13: `./build.sh` passed every gate on a
   clean tree (`sighop:0.1.0-6617f90cf8a2`, all 13 captures rendering identically on the
   host and in the image); `docker compose up` ran it as UID 1000 with the V4 mapped by
   `/dev/serial/by-id`, migrated an empty database to `0005` on start and refused to serve
   until `web user add` had run; `dev-operator` signed in from Chromium on the host, opened
   the gate with a password (a wrong one first, refused as its own event, still signed in),
   and a direct message composed as the greeter identity was acknowledged by the stock peer after 1
   attempt in 3281 ms; `docker compose restart sighop` exited 0, the next navigation landed on
   `/login?next=%2Fchat` with the gate shut again, and after signing back in the message was
   still there, still delivered. Every `web_guarded_action` and every signed-in `web_request`
   named `dev-operator`; `unauthenticated` appeared only on what a signed-out browser asks for.
   Findings the offline work produced:
   - **A scan that fails the build on the base image's findings builds nothing.** The newest
     `python:3.13-slim-trixie` digest carried 12 fixable HIGH/CRITICAL Debian findings on the
     day the gate was written, none in sighop's dependencies, and no digest cleared them. The
     scan became a report (operator decision) and the base moved to Alpine: 7 HIGH, all
     `libuuid`, against 56 on slim, with every native dependency shipping a `musllinux` wheel.
     The glibc/musl split is what the `replay` gate exists for.
   - **Deleting from a base image in a later layer hides files from the scanner and ships them
     anyway.** Removing `pip`, `apk` and `/lib/apk/db` in the final stage saved no bytes and
     made the scan report *zero* OS findings — a blind scan reading as a clean one. The final
     stage now runs no command, and `tests/test_deployment_files.py` refuses one that does.
     **A scanner can only report what the image admits to containing.** The Python findings it
     still prints (`msgpack`, `setuptools`) belong to the base's own `pip`, not to `uv.lock`.
   - **`set -e` does nothing inside a function called as an `if` condition.** The first
     `smoke` gate passed an image that wrote to its root filesystem, because the failing
     `docker run` inside `gate`'s condition did not stop the function. Every step now carries
     `|| return 1`, and the gate was re-verified against a deliberately root-writing image.
   - **The serial group is a number, not a name.** This host's serial group is `uucp` (984),
     not `dialout`, and a name in `group_add` resolves against the *image's* `/etc/group`;
     by-id paths contain colons, which compose's short `devices` syntax splits on. Both are
     why the compose file takes `DIALOUT_GID` and uses the long syntax.
   - **A test fixture had carried the real development database password since milestone
     5.** `tests/test_config.py` now uses a fake value; the real one remains in git history,
     and rotating it is the operator's call.
   - **The deployment grew simpler under use, twice.** A one-shot `migrate` service and
     compose `secrets:` files (with `_FILE` companions in `config.py`) were built and then
     removed by operator decision: `run --migrate` moves only a database that is *behind*, and
     the two secrets come from the gitignored `.env`. The `_FILE` code was deleted rather than
     kept as a configuration shape nothing exercised.

   What the live exercise overturned:

   - **A board that drops off USB and comes back does not strand the container.** Replugging
     the V4 under the running container re-enumerated it as `ttyACM0` with the same `166:0`;
     `/dev/modem` inside still pointed at that number, the reconnect loop reopened it after 3
     attempts in 3.5 s, the probe re-confirmed 869.618 MHz SF8, and the next zero-hop advert
     from the stock peer was received (SNR +11.25). No device-cgroup rule was applied. What
     was **not** exercised is the stranding case itself — the board returning under a
     different minor because another ACM device took minor 0 in between — and a process that
     cannot open `/dev/modem` at start still exits rather than retrying. An RTS pulse over the
     USB-JTAG serial link, tried first, produced no disconnect at all and nothing in sighop's
     output, so it is not a substitute for the unplug milestone 2 describes.
   - **An HTMX poll that loses its session paints the sign-in page into the fragment.** The
     chat pane polls `…/messages` every 3 s; once the session is gone the guard answers `303`,
     htmx follows it, and a complete page — header, form and footer — lands inside the
     message list, under an outer header still reading "signed in as dev-operator". Observed
     by clearing the cookie on an open conversation, and what every open chat tab shows after
     `docker compose restart`. The guard's redirect is right for a navigation and wrong for a
     fragment request; an `HX-Request` should be told to navigate (`HX-Redirect`) instead.
   - **An open feed WebSocket holds shutdown to uvicorn's grace limit.** The restart exited 0
     inside `stop_grace_period`, but only after "timeout graceful shutdown exceeded" cancelled
     the two open feed connections, each printing a `CancelledError` traceback and closing
     with `web_feed_closed` at `level=error`. A stop the operator asked for is not an error, and
     the feed should close its connections when shutdown begins rather than when it is
     cancelled.
   - **Inside compose every client is the bridge gateway.** Every `web_login` and
     `web_request` recorded `client: 172.23.0.1`, so the per-address throttle keys every
     browser on the host to one address and acts as a global one — the degradation D8
     accepted for a proxy, arriving without one. The per-username throttle is unaffected.
   - **A signed-out browser's ordinary requests log as errors.** The `303` to the sign-in
     form is `level=error`, and its `route` is the raw path because routing has not run —
     full conversation keys included. Neither is secret, but the first makes a restart look
     like an incident in the log and the second makes `route` unsafe to aggregate on.
   - **The runbook's `stat -c %g` on the by-id path returns `0`.** It stats the symlink, owned
     by root, not the device; it needs `-L`. Compose would have accepted `DIALOUT_GID=0` and
     the container would have been refused the modem. Corrected in the runbook.
   - **Milestone 8's frozen feed status line is still frozen**, as that milestone left it:
     the overview read `live — 0 record(s) shown` while the closing events of the same
     connections recorded `delivered=4`.
   - **The containerised run takes no capture, so the corpus is unchanged.** The compose
     command passes no `--capture`, and a read-only root offers only `/tmp` to write one to; the frames
     seen (`ADVERT`, `TXT_MSG`, `ACK`) are shapes the corpus already holds. A deployment that
     should contribute captures needs a mounted, writable directory, which is a decision
     rather than an oversight to fix quietly.
10. **Channels.** The channel key store, the receiver, the poster, the history and both
    surfaces — §7's "Channels", tables thirteen and fourteen, `net/channels.py`, the
    `sighop channel` noun and the chat channel pages.
    *Done.* The live exercise ran on 2026-09-16 against the **Heltec V4 OLED** on native USB
    as the station's modem and a **stock V3 companion** ("[redacted]",
    `v1.17.1-d929643`) on its own USB as the peer, on the hashtag channel `#dev-sighop`[cd]
    and never on Public. The peer's post appeared in the browser as
    `✗ [redacted] — claimed, unverified … received, 0 hop(s)`; two posts composed
    in the browser as `dev-companion` went out as one flooded class-2 transmission each
    (53 B, 640 ms of air, 2.5 s of queue wait, no retry) and arrived on the peer's own
    channel slot as `dev-companion: browser post over the air` at SNR +12.00 and +12.25;
    each was heard back from a repeater once (`h1`, SNR +13.25) and the page said so —
    *transmitted; no acknowledgement exists for channel messages; repeat heard 1x — a
    repeater forwarded it*. A restart restored `channel_messages=9` and left all three rows
    in place with their states. **The whole session cost 1 s of the 360 s hour — 0.4% against
    a 10% ceiling**, with `ch_rx`/`ch_tx`/`ch_repeats` on the status line agreeing with the
    events. Findings:
    - **The reception line and the channel line contradicted each other on every decrypted
      frame.** `net/rx.py` printed `not decrypted (no key held)` for a `GRP_TXT` that the
      channel layer decrypts and prints in full on the next line. Both were true of their own
      stage — nothing in `net/rx.py` holds channel keys — but read together the first line was
      a failure report about a message that did not fail. *Fixed:* every envelope now reads
      `not decrypted at decode (keys are tried by the layer that holds them)`, which is the
      same sentence for direct messages, where decoding holds no key either.
    - **The startup line said how many direct messages were restored and not how many channel
      messages.** `persistence_restored` carried `channel_messages=9`; `restored: … held:
      conversations=3 messages=4` did not, so milestone 8's advice — *if history looks empty,
      read the startup line* — did not extend to channels. *Fixed:* the line carries
      `channel_messages=`, and names the posts a stop left mid-flight when there are any,
      since rewriting `awaiting` to `unknown` was otherwise invisible.
    - **A channel added from the terminal arrived in a running station silently.** The 60 s
      refresh picked up `#dev-sighop-refresh` and the browser listed it, while the run's
      output and its events said nothing: only `channel_config_read_failed` was reported.
      The CLI promises "a running run applies this within 60 s" and nothing in the run
      confirmed it. *Fixed:* a reload that adopts a different set emits `channel_set_changed`
      and prints `channels changed: +#dev-reload-check  (3 loaded; …)`; a reload that changes
      nothing stays silent, and the first set a run loads is its startup report, not a change.
    - **`sighop channel history` called a keyfile identity "an identity no longer stored".**
      The two posts read `-> posted as an identity no longer stored: …` from the terminal and
      `dev-companion (this station)` in the browser, because a `--entity` keyfile is never in
      the entity store. The row was right that the store cannot name it and wrong about why:
      it was never there, rather than removed. *Fixed:* the line prints the key —
      `posted as bf447b986a9f302a… (not in the entity store: a keyfile identity, or one
      removed)` — which is what tells the two cases apart, and is the key `sighop keys list`
      shows.
    - **The peer's firmware confirmed two constants the corpus could only imply.** Its slot 0
      holds `8b3387e9c5cdea6ac9e5edbaa115cd72` — `PUBLIC_CHANNEL_KEY` — and reports its hash
      as `11`; setting a slot to `#dev-sighop` produced the same 16 bytes and the same hash
      `cd` as `channel_key_from_hashtag`. The `0x11`-vs-`0x17` distinction of §5 is the
      firmware's own arithmetic, read back from a second implementation.
    - **Foreign Public traffic decrypts live, not merely in the corpus.** The first Public
      message arrived 60 ms after startup and every one after it decrypted with a claimed
      name; the 85 corpus decrypts were not a property of old captures.
    - **A companion peer holds channel messages until a client fetches them.** The first
      browser post did not appear on the peer until a client attached minutes later, and then
      arrived alongside four buffered Public messages. A live client's silence is not evidence
      that a post was not received, and a test peer driven over USB has to be listening
      *before* the post, which is how the exercise was re-run.
    - **A repeater's copy of someone else's channel message is dropped by dedup, not by the
      repeat registry.** The peer's own post came back as `h1 path=bed0` 3 s later: two
      receptions, `dup=50.0%`, `ch_rx=1`, one stored row. The registry of D6 only ever sees
      copies of *our* posts.
    - **The over-limit refusal counts what it says it counts.** 170 bytes of text plus 15 of
      name and separator was refused as *185, which is 25 over*, with a `400`, the text and
      the chosen identity both preserved in the re-rendered composer, and nothing submitted.
    - **Ctrl-C produces no record of itself.** No summary line, no stop event, nothing that
      distinguishes a stop the operator asked for from a kill — the process simply stops
      appearing. Channel rows written before the signal did survive it, including a reception
      one minute before, but that is the write-behind's periodic flush rather than a drain.
    - **The exercise's capture was not added to the corpus, and that was a decision rather
      than a rule.** 16 frames, and unlike milestones 8 and 9 it *did* hold shapes the
      corpus lacked: `GRP_TXT` on hash `0xcd` that we could decrypt, and two `tx_frame`s of
      our own posting. Appending it would have changed what "foreign" meant for the channel
      decryption test, which walked every capture and asserted 85 frames on `0x11` and 108 on
      `0x81`; a corpus that holds a channel we hold the key to needed that test split first.
      (Moot since change `synthetic-corpus`: the synthetic corpus holds a second channel we
      have the key to, and the recordings are gone.)

**Recorded history: flood repetition (input to the milestone 3 dedup cache).** These are
measurements of the real mesh, taken from the recorded corpus that change `synthetic-corpus`
removed. They carry no identity, so they are kept here verbatim rather than deleted: they are
why the dedup defaults are what they are, and why the synthetic corpus declares a late echo and
a retransmission (`tests/protocol/CORPUS.md`).

Counting duplicates the way the firmware does — `Packet::calculatePacketHash`, over the payload
type and payload bytes, plus `path_len` for TRACE — over the 351-frame milestone 0 subset:

- 351 receptions carried **205 distinct packets**: **41.6% of receptions are repeats**, 1.71
  receptions per distinct packet.
- Copies per packet: 82 seen once, 101 twice, 21 three times, 1 four times. **Maximum 4.**
- Flood frames repeat far more than direct ones: 94 of 171 flood receptions were repeats,
  against 52 of 180 direct.
- Duplicates arrive close together: median spread between first and last copy **1.3 s**, p95
  **3.6 s**, maximum **31.1 s**.
- Distinct packets within a sliding window: **16** in any 60 s, 49 in any 300 s, 56 in any
  900 s.

The 2026-09-04 session, measured the same way, came out **much quieter**: 91 receptions, 81
distinct packets, **11.0% repeats**, at most 2 copies of any packet, median spread 2.8 s and
maximum 4.6 s, 24 distinct packets in any 60 s. The directional finding survives and sharpens —
all 10 repeats were flood receptions, and **not one of the 57 direct receptions repeated** — but
the *rate* clearly is not a constant of this mesh: it moved from 41.6% to 11.0% between nights,
on a different board.

The 2026-09-05 session, being both long and busy, is the one that settled the sizing. 555
receptions, 370 distinct, **33.3% repeats**, at most 3 copies of any packet, 16 distinct packets
in any 60 s and 49 in any 300 s. Over all **1000 receptions** — the 3 frames sighop transmitted
are excluded, since the subject here is what the mesh sent us: 659 distinct packets, 34.1%
repeats, median gap between consecutive copies **0.99 s**, p95 **3.3 s**, and only **two** gaps
anywhere above 60 s. Those two are worth naming, because they are not the same thing:

- **200.7 s** — a flood ANON_REQ whose late copy arrived by a *different* path (SNR −10.25
  against 14.25). A genuine late echo, six times the 31.1 s the milestone 0 subset called its
  worst case.
- **3158 s (52.6 min)** — two **byte-for-byte identical** zero-hop DIRECT TXT_MSG frames, same
  ciphertext, same SNR. Not a copy of one transmission but the sender **retransmitting an
  unacked DM**.

So the earlier recommendation here — "~128 entries with a 60 s TTL, an order of magnitude of
headroom" — was wrong, and wrong because three short nights cannot sample the tail of a
duration. A 60 s TTL would have missed the 200.7 s copy outright. The shipped defaults are
**300 s and 4096 entries**, and the TTL is bounded from both sides: shorter discards real flood
copies, much longer starts swallowing sender retries, which are events a user should see rather
than have deduplicated away. Peak occupancy at 300 s is 49 entries, so the cap is headroom
against a busier mesh, not a fit to this one.

**Recorded history: the first-transmit interoperability exchange.** On 2026-09-04 a Heltec V4
running sighop and a Heltec V3 running stock `companion_radio` v1.17.1-d929643 exchanged three
frames each way (the peer's direct message, sighop's acknowledgement of it, sighop's message,
the peer's 6-byte acknowledgement, and a zero-hop advert each), recorded whole and committed
with a burned key so that a test could decrypt the peer's ciphertext on every commit. It
confirmed the ECDH, the AES-128-ECB key slice, the 2-byte HMAC truncation and both
acknowledgement forms against another implementation, for one exchange with one firmware
build. It existed and passed once, and was withdrawn by change `synthetic-corpus` with the
rest of the recorded corpus and the key, because both were real data; nothing replaces it as
interoperability evidence (§5).

Milestones 0–4 carry nearly all the technical risk, and 0–3 need no transmit permission at
all. Get real adverts decoded off real air before building anything else.

Capturing real traffic first is worth the small detour: it turns milestone 1 from
guess-and-check against a written spec into verification against ground truth, and every
odd real-world frame that would otherwise surface as a late bug becomes a test case.

### Capture format and provenance

A capture is JSONL, one object per line, `{ts, kind, raw_hex, rx_meta}`. Because the capture
file becomes the **permanent regression corpus** for milestone 1, it must carry its own
provenance — a corpus whose recording conditions live only in someone's memory decays into
untrustworthy fixtures.

`sighop capture` therefore writes a **header record as the first line** (`kind:
"capture_meta"`) holding at minimum: `GetDeviceName` as answered by the board, `GetRadio`
(freq/BW/SF/CR) and `GetTxPower` read back live, the firmware version from `GetVersion`, the
sighop version and commit, and the probe results from §4.1. Never infer these from config —
record what the hardware reported.

Implemented in milestone 2, and written by **both** `sighop capture` and
`sighop monitor --capture` — they share one writer (`radio/capture.py`'s `CaptureWriter`)
rather than two implementations of one format. A value the board did not answer is written
as an explicit null *with its reason*, and the read-back radio parameters are recorded
separately from the configured ones so a disagreement is visible in the file itself.
`radio/replay.py` reads the header back as provenance and tolerates its absence.

The two milestone 0 captures predated this and had no header. Their frames were left
untouched, and provenance lived in a sidecar `.meta.json` each, which separated what was
**observed** (recomputed from each file and its paired log) from what was **reconstructed**
(stated from memory afterwards) and recorded the hardware readback as explicitly absent.
Reconstructed radio settings are not evidence — if a decoder disagreement ever turned on them,
the answer was to re-capture with a real header rather than trust them. The two were kept as
separate captures rather than merged — the second run started independently (~12 min after
the first's `capture_stopped`) and each carried its own provenance.

Everything recorded from milestone 2 onward carries the header instead, so the sidecar was a
transitional form and not a second supported mechanism. What the corpus requires is that
*every* file state the conditions it was recorded or generated under; a file whose origin is
unrecorded is a fixture, not evidence, and the corpus harness refuses it. Since change
`synthetic-corpus` every corpus file is generated and its header says so, with the generator
version and seed, and the sidecars are gone with the recordings. Files from one recording
session stayed separate for the same reason as above: one night split by device restarts, each
restart re-probing the board, so each file's header described its own run.

This is also how the V3-vs-V4 RX question gets settled if it ever matters: capture on each
board from the same aerial over the same interval and diff the frame counts by device name.
Until something forces that experiment, board choice is not a decision (§4.1).

---

## 13. Resolved questions and remaining unknowns

All design questions raised during specification are resolved and recorded in the sections
above: region and duty cycle (§2, §4.3), scale target (§2), advert defaults (§4.3), test
setup and hardware (§2, §4.1), UI direction and auth (§8), room password policy (§7).

**Advert override ergonomics**, previously open, is settled: a faster interval may be set
only with a **mandatory expiry** (default 1 h, maximum 24 h), after which it auto-reverts to
the 24 h floor, with a standing banner in the UI while active. There is no permanent
override — the failure mode being designed out is someone setting a 5-minute advert to test
something and forgetting, which is exactly the behaviour the floor exists to prevent.

### Unknowns to settle during implementation

These need real hardware or real traffic to answer, and are cheap to resolve in-flight:

1. Sensible default retention policy per room — depends on observed message
   volume. **Still open after milestone 6, and deliberately a non-observation
   rather than a measurement.** The live exercise's three runs spanned roughly
   20 minutes end to end and carried 14 stored posts — 2 from the client, 12
   pushed from the server, several of them posted deliberately in a burst to
   drive a member into backoff — against 17 s of airtime out of the 360 s
   hourly ceiling. Both numbers are artifacts of testing a push loop and a
   restart, not of how a room is actually used, in exactly the sense milestone
   3's dedup-cache sizing warned about: a duration this short cannot sample a
   volume distribution, only fail to contradict any default. No retention
   policy was set for the exercise room, and none is recommended from it.
2. ~~Whether the dedup cache should be sized by entries or by time~~ — **settled in milestone
   3, and the question was a false choice.** It needs both, because they bound different
   things. *Time* governs correctness, and it is bounded from **both** sides. The widest gap
   between genuine copies of one transmission is **200.7 s** — a flood copy arriving by a
   different path in the 2 h 54 min live session, against the 31.1 s the 442-frame corpus had
   suggested — so a TTL much under 300 s discards real duplicates. But the same session also
   caught two byte-identical DIRECT frames **3158 s** apart, which is a sender retransmitting
   an unacked message rather than a copy of one, so a TTL stretched much further starts hiding
   retries the user should see. 300 s sits between those bounds with 1.5× margin over the
   worst real duplicate — not the ten times the corpus alone implied, and the lesson is that
   a duration bound cannot be sized from a capture shorter than the bound. *Entries*
   governs memory: peak occupancy at that TTL
   is 49 entries against a 4096 cap on both the corpus and the live session, and nothing
   about observed traffic bounds the
   distinct-packet count — per-file repetition ranges from 5.4% to 45.7%, so the cap is what
   makes the cache safe on a mesh busier than any we have recorded. Both defaults stand, now
   measured rather than assumed, and both are reported in `sighop run`'s status line so the
   next sizing decision is made from data too.
3. Whether path scoring needs more than "most recently confirmed wins" — only observable
   once multiple routes to the same peer exist. Still open: milestone 3 records every
   candidate route with its hop count and SNR, and deliberately scores none of them, and
   milestone 4 produced exactly one route to one peer, which is not the observation this
   needs. Milestone 5 made the observation *accumulate*: candidates are persisted rather
   than thrown away every restart, so the evidence now builds across runs instead of
   starting over. Its own quiet session still showed **1.00 candidate per destination**,
   which is the same non-observation as before, with a longer lever behind it.
4. Whether a peer that learns us **only from a zero-hop advert** will flood its replies.
   Raised by milestone 4 rather than settled by it: a zero-hop advert carries no path, so the
   peer recorded our contact with `out_path_len = -1` (unknown) after hearing one, where the
   USB-injected contact had length 0. The exercise used the injected form throughout, so what
   a node does with the advert-learned form is untested. It matters the first time sighop is
   reachable by a node it has not been introduced to over a cable — milestone 5 or 6 —
   and the answer decides whether an entity must solicit a path before it can be replied to
   cheaply. **Settled, asymmetrically, by milestone 6's live exercise** — the first
   opportunity to observe it, since the client learned the hosted room purely from its zero-hop
   advert. Read off the captured frames rather than the client's UI: every one of the
   client's own outbound frames — every `ANON_REQ` login, every posted `TXT_MSG`, its one
   `REQ` — arrived `FLOOD`-routed for the full ~20-minute session, including long after the
   server had a learned path back to it. The server's downlink did **not** stay flooded: its
   very first push after the initial login already went out `DIRECT`, having learned the
   client's path from the `PATH` return bundled with the login reply (design D7/D12). The
   client's own acknowledgements of those pushes started `FLOOD` (the first two) and then
   switched to `DIRECT` for the rest of the session, including across the restart — so a
   learned path gets used for acknowledging a direct delivery, but not for the client's own
   logins, posts or requests, which is either firmware policy or a path the client never
   solicited for its own uplink. So: yes, a peer that learns us only from a zero-hop advert
   floods its replies, and keeps flooding its own transmissions for at least 20 minutes of
   active use afterward, even once the exchange has an established two-way direct route.

**Telemetry sub-command availability**, previously unknown #1, is settled from the firmware
source: `getMCUTemperature()` comes from the shared `src/helpers/ESP32Board.h` and both the
V3 and V4 variants implement `getBattMilliVolts()`, so `GetBattery` and `GetMCUTemp` answer
on both boards. `GetSensors` answers on both but with a build-flag-dependent payload shape.
Keep the §4.1 startup probe regardless — it costs nothing, it records provenance for the
capture header, and it is what keeps a third board from being a code change.
