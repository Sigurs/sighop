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
| Radio count | **One radio. No separate RX and TX devices.** | Co-sited transmitter and receiver on the same frequency is one radio destroying another, not two independent radios (§4.3). The second board is better spent as the test peer. A second radio on a *different* preset or band is a separate question, left open by the multi-modem non-goal. |
| WebUI auth | Built-in session login, Argon2id, secure cookie. | Standalone, no external dependency. The UI holds private keys and can key a transmitter; it does not ship unauthenticated. |

### Explicit non-goals for v1

- Repeating / packet forwarding
- Transport codes (region/sub-region scoping) — parse and preserve them, do not originate
- Multipart payloads (`0x0A`)
- Bridging to other mesh protocols
- Multi-modem support (the architecture leaves room; the scheduler assumes one)

---

## 3. Domain model

A **virtual entity** is an addressable MeshCore node hosted by sighop. Every entity owns:

- an Ed25519 keypair (its identity; its **node hash** is the first byte of the public key)
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
invisible forever. That rule also quietly recovers ESP32 panic backtraces, which arrive
interleaved in the KISS stream as unparseable bytes.

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

At the 2–5 entity target these defaults put sighop at roughly the advert load of the 2–5
real nodes it is standing in for, which is the correct amount. The failure mode this guards
against is scale: 20 entities on the 12 h firmware default would emit 40 flood adverts a day
from one antenna, each amplified across every repeater in the mesh. The shared minimum gap
and the 24 h floor keep that from arising by accident if the deployment ever grows.

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
keys already carry a pre-clamped scalar, so the usual Ed25519 hashing step is skipped.
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

**Channel keys (GRP_TXT).** Either a pre-shared 16-byte key, or derived from a hashtag as
`sha256(b"#roomname")[:16]`. The channel hash in the payload is the first byte of
`sha256(channel_key)`. Hashtag-derived keys have a small keyspace and are brute-forceable —
surface this in the WebUI when a user creates a hashtag channel.

**Adverts** are unencrypted but Ed25519-signed over `public key ‖ timestamp ‖ appdata`, in
that order (`Mesh.cpp::createAdvert`). **Always verify the signature before trusting any
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

**This is no longer verified only against ourselves.** As of milestone 4 the shared-secret
derivation, the AES-128-ECB key slice, the 2-byte HMAC truncation and both directions of the
ACK construction are confirmed against a **foreign implementation**: a Heltec V3 running stock
`companion_radio` v1.17.1-d929643 encrypted a DM to a key sighop holds, and
`tests/protocol/test_foreign_decrypt.py` decrypts it on every commit from
`captures/2026-09-04-first-transmit.jsonl`. The negative half of that vector is asserted too —
the full 32-byte secret used as the cipher key, or the MAC keyed on only the first 16, must
fail — so the two distinct key slices stay distinguishable by evidence rather than by comment.
It confirms these constructions for one exchange with one firmware build, which is the whole
of what one exchange can confirm.

Group messages carry **no sender authentication** — the sender name is plain text inside the
ciphertext (`<name>: <body>`). Anyone with the channel key can claim any name. The WebUI
must not render channel sender names in a way that implies verified identity.

---

## 6. Persistence

Postgres via SQLAlchemy 2.0 async. The eight-table sketch below is the original one; four
of them exist as of milestone 5 and four do not yet, and the split is deliberate.

**Built (milestone 5, migration `0001`):**

- **entity** — id, type, name, public key, **sealed** seed, advert config (JSONB), enabled,
  created_at
- **contact** — public key (PK), node hash, name, node type, flags, `advert_verified`,
  first/last heard
- **path** — destination public key *or* node hash, path bytes, hash size, hop count, SNR,
  `confirmed_at`, packet id, unique on (destination, path bytes, hash size)
- **packet_log** — ring buffer of recent RX/TX for the observability UI (bounded; not the
  audit trail), with `raw` and `reason` for frames that could not be decoded

**Not built yet, each in the milestone that owns it and each named in `0001`'s docstring so
absence reads as intent:**

- **room**, **room_member**, **message** — milestone 6
- **bot_state** — milestone 7

The list above was always a sketch and never final DDL, and milestone 6 will discover things
about ACLs and retention that change those four tables; shipping them untested would make
the first real migration a rewrite rather than an addition.

Three details of the built four are worth stating because they look like mistakes:

- **`node_hash` is indexed but not unique**, on either table. §3 says one byte of identity
  collides at 1 in 256 and the whole design is built on candidate sets; a unique constraint
  there is a bug waiting for a busy mesh.
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
disk.

**Memory stays the authority.** Contacts and paths keep their in-memory stores and their
interfaces, are loaded from the database once at startup, and answer every lookup from
memory — including while the database is unreachable. Writes happen behind the reception
path, and the three stores differ only in how much a lost write costs: a contact is written
promptly and never dropped (re-acquiring one means waiting for the peer to advert, and the
advert floor is 24 h), while a route and a log row are dropped freely and counted, because
the next reception regenerates one and the other is a feed. Migrations are the only
authority on schema; `sighop run` refuses a database that is not at the revision the code
expects, and never migrates as a side effect of starting.

### Private keys at rest

Entity private keys are the platform's crown jewels — they *are* the identities. Encrypt
them at rest with a key from the environment (`SIGHOP_SECRET_KEY`), never in the database.
A DB dump must not be sufficient to impersonate a room server. Provide `sighop keys export`
/ `import` so operators can back identities up deliberately, and make the WebUI's key
display an explicit, audited action.

**How, as of milestone 5.** The seed is sealed with **XSalsa20-Poly1305 secretbox**
(PyNaCl's `SecretBox`, already a dependency because the identity code uses libsodium), and
the stored value is a **version byte followed by the sealed box** so a future re-key has
somewhere to declare itself. The box generates its own nonce, which is the one thing most
likely to be got wrong by hand, and Poly1305 supplies the authentication tag — a tampered
`sealed_seed` fails loudly instead of yielding some other key. On load the public key
derived from the decrypted seed is compared against the `public_key` column stored beside
it, and a mismatch names the entity rather than preferring either value.

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
*unencrypted* seed protected only by its permissions, because the same seed inside the store
is encrypted and an operator must not conclude the two offer the same protection.

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

### Companion

An addressable identity driven by a human in the WebUI: send/receive DMs, join channels,
maintain a contact list. Effectively the reference implementation of the client side of the
stack — build it early, since it exercises nearly every protocol path.

### Bots

A bot is a companion with a driver plugin. Minimal interface:

```python
class Bot(Protocol):
    async def on_advert(self, ctx: BotContext, advert: Advert) -> None: ...
    async def on_direct_message(self, ctx: BotContext, msg: DirectMessage) -> None: ...
    async def on_channel_message(self, ctx: BotContext, msg: ChannelMessage) -> None: ...
```

`BotContext` exposes sending, contact lookup, and persistent state — bots never touch the
scheduler or the database directly.

**Greeter bot.** Sends a welcome DM to nodes not seen before.

Three things it must get right:

1. *"New"* means never seen in the persistent `contact` table — not new since process start.
   A restart must not re-greet the whole neighbourhood.
2. Rate-limit greetings globally. A busy mesh, or a burst of adverts after an outage, would
   otherwise produce a flood of DMs — antisocial and airtime-expensive.
3. Only greet after verifying the advert signature (§5). Greeting an unverified identity
   means a spoofed advert can make sighop transmit on demand.

---

## 8. WebUI

All four areas confirmed in scope. Priority order for building:

1. **Admin & config** — CRUD virtual entities, key management, radio settings, enable/disable bots, room passwords and retention.
2. **Observability dashboard** — live packet feed (WebSocket), airtime and duty-cycle usage against budget, per-entity TX/RX counters, contact table with learned paths, SNR/RSSI, modem health.
3. **Room browsing** — message history from Postgres, per-room member lists.
4. **Chat client** — send DMs and channel messages as any companion entity. This makes sighop usable without a handheld and is the strongest argument for the project.

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
rather than assuming a protective network. Built-in session login: username plus Argon2id
password hash, secure `HttpOnly`/`SameSite` cookie, no external identity dependency.

Two rules follow from what the UI can do:

- **No unauthenticated mode, at any milestone.** Until auth exists (milestone 9), the
  development server binds to localhost only.
- Actions that reveal a private key, enable transmit, or raise the duty-cycle ceiling are
  re-authenticated and logged as their own wide events with the acting user recorded.

Reverse-proxy trust is deliberately *not* supported in v1. It is a reasonable deployment
pattern, but "trust this header" is a footgun that turns one proxy misconfiguration into
unauthenticated key access, and it can be added later without disturbing anything.

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

---

## 10. Container and deployment

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

Secrets (`SIGHOP_SECRET_KEY`, DB password) come from the environment or Docker secrets —
never baked into the image or committed.

The build script (`build.sh`) covers: lint, typecheck, test, image build, and a
vulnerability scan of the result.

---

## 11. Repository layout

```
sighop/
├── src/sighop/
│   ├── radio/          kiss transport, modem (rx + tx), probe, capture, replay
│   ├── protocol/       packet codec, payloads, crypto
│   ├── net/            rx.py (decode stage), dedup.py, paths.py, bus.py,
│   │                   tx.py (scheduler), airtime.py, adverts.py,
│   │                   contacts.py, dm.py (direct messages, both directions)
│   ├── monitor/        render.py (pure formatting), run.py (`sighop monitor`)
│   ├── keystore.py     entity keyfiles (`sighop keys`) — file I/O, so not
│   │                   under protocol/
│   ├── runtime.py      the whole pipeline wired together (`sighop run`)
│   ├── entities/       base, room server, companion, bot runtime
│   │   └── bots/       greeter
│   ├── db/             models.py (the four tables), repositories.py (what net/
│   │                   calls), engine.py (pool, bounds, degraded state, probe),
│   │                   writer.py (bounded write-behind), sealing.py (seeds at
│   │                   rest), packetlog.py (feed rows, pruner),
│   │                   persistence.py (the wiring), migrations.py (alembic)
│   ├── web/            FastAPI app, routes, templates
│   ├── logging.py      structlog config, wide-event helpers
│   ├── config.py       DATABASE_URL and SIGHOP_SECRET_KEY from the environment,
│   │                   validated and password-redacted. No dotenv dependency:
│   │                   `uv run --env-file` and compose already read the file
│   └── cli.py
├── alembic/            async env.py (design D1) and one migration per milestone
├── alembic.ini         no URL in it — config.py is the single source
├── tests/
├── compose.yaml
├── build.sh
├── Dockerfile
└── pyproject.toml
```

`protocol/` must have no dependency on `db/` or `net/` — it is pure functions over bytes.
That is what makes it testable against captured packets, and it is the layer where
correctness matters most. Since milestone 5 `db/` actually exists, so
`tests/protocol/test_import_boundary.py` asserts the direction explicitly — naming
`sqlalchemy`, `asyncpg` and `alembic` as well as `sighop.db` — rather than relying on the
package not being there to import. **`db/` is a peer of `net/`, not a layer beneath
`protocol/`**: it may import from `net/`, and `net/contacts.py` and `net/paths.py` import
no SQLAlchemy at all. Each takes an optional sink whose `offer` never awaits and never
raises, which is what keeps the persistent path a thin adapter rather than a rewrite, and
keeps every existing test running with no database.

`keystore.py` sits at the top level rather than in `protocol/` for the same reason: reading a
keyfile is I/O, and `protocol/` has none. The seed → identity step stays in
`protocol/identity.py`, so the boundary test keeps passing and the split is the one the layer
rule already implies.

`net/dm.py` handles inbound direct messages as a **bus subscriber**, never inside `net/rx.py`.
Decryption needs local keys and a contact table; the decode stage stays a pure function of one
frame, which is what keeps a replayed capture reproducing every reception exactly. It reports
its work as typed events that `monitor/render.py` formats, so `net/` never imports `monitor/`.

`radio/replay.py` is the inverse of `radio/capture.py` and lives beside it deliberately:
it re-hydrates a capture file into the same event stream the modem produces, so the live
decode path can be driven offline. `monitor/` is a separate top-level package rather than
part of `net/` because rendering is not networking — and keeping `render.py` a set of pure
functions is what makes the output testable by string comparison.

---

## 12. Milestones

Ordered to exploit the fact that real hardware and a live mesh are available from day one.

0. **Capture.** KISS transport and enough of `Modem` to open the serial link, plus a
   `sighop capture` command that dumps raw frames with RxMeta and timestamps to a file.
   Run it overnight against the live mesh. **Receive-only; nothing transmits.**
   *Done:* `captures/2026-09-02.jsonl` (152 frames, 9h16m, graceful stop) and
   `captures/2026-09-03.jsonl` (199 frames, ~7h37m, no graceful-stop event but every JSONL
   line well-formed), both recorded on the **Heltec V3**. Both runs: zero reconnects, zero
   unparsed/malformed frames, zero Data/RxMeta correlation anomalies.
1. **Protocol core.** Packet codec, crypto, advert parse/verify — developed against the real
   captured frames from milestone 0, not synthetic fixtures. Pure functions over bytes; no
   radio, no database. The capture file becomes the permanent regression corpus.
   *The corpus's one hard limit, and where it now stands:* every encrypted payload recorded
   through milestone 3 is addressed to a third party, so the corpus proved framing, adverts
   and signature verification but **could not prove decryption**. Milestone 4 closed that for
   exactly one exchange — the two direct messages in
   `captures/2026-09-04-first-transmit.jsonl`, whose key the repository holds — and for no
   other ciphertext in the corpus, which stays verifiable only by round-trip and by fixed
   known-answer vectors against the firmware source.
   *The corpus is not frozen:* milestone 2's live session added `captures/2026-09-04.jsonl`,
   `-02` and `-03` (91 frames), taking it to 442 frames across five files, and milestone 3's
   long receive-only run added `captures/2026-09-05.jsonl` (555 frames), taking it to **997
   frames across six files**. A session is appended when it carries a shape the corpus lacks —
   the first brought the first `TRANSPORT_FLOOD` frame and the first CONTROL payloads, the
   second a located CHAT advert (`0x91`), a 10-byte TRACE, and the duplicate-timing tail that
   sizes the dedup TTL — and is appended whole, because a corpus of hand-picked interesting
   frames stops being a sample of the mesh.
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
     `captures/2026-09-05`), with a 15-minute advert override on two stub entities so the
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
   criterion is met and is a **committed regression test**, not a log line:
   `captures/2026-09-04-first-transmit.jsonl` holds the peer's DM and
   `tests/fixtures/burned-first-transmit.json` holds the key that opens it, so
   `tests/protocol/test_foreign_decrypt.py` decrypts a foreign implementation's ciphertext on
   every commit. The corpus's "one hard limit" is removed for exactly that exchange.
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
   - **Contacts being in-memory has an operational consequence worth stating.** The run that
     sends must hear the peer's advert *in that same run*; a restart forgets every contact.
     That is what `--peer-wait` exists for, and it is milestone 5's `contact` table that
     removes the need.
   - **Total cost on the air: 3 frames, 1.5 s, 0.2% of the hourly duty-cycle ceiling.** The
     session is appended whole as `captures/2026-09-04-first-transmit.jsonl`, taking the
     corpus to **1003 records — 1000 received and 3 transmitted**, and bringing it its first
     frames sighop sent and its first decryptable payload.
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
7. **Greeter bot** and the bot plugin interface.
8. **WebUI**, in the §8 priority order.
9. **Hardening.** Container, compose, build script, auth.

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

The existing `captures/2026-09-02.jsonl` and `captures/2026-09-03.jsonl` predate this and
have no header. Their frames are left untouched; provenance lives in the sidecar
`captures/2026-09-02.meta.json` and `captures/2026-09-03.meta.json`, which separate what was
**observed** (recomputed from each file and its paired log) from what was **reconstructed**
(stated from memory afterwards) and record the hardware readback as explicitly absent.
Reconstructed radio settings are not evidence — if a decoder disagreement ever turns on them,
re-capture with a real header rather than trusting them. The two files are kept as separate
captures rather than merged — the second run started independently (~12 min after the first's
`capture_stopped`) and each carries its own provenance.

Everything recorded from milestone 2 onward carries the header instead, so the sidecar is a
transitional form and not a second supported mechanism. What the corpus requires is that
*every* file state the conditions it was recorded under, by one means or the other; a capture
whose origin is unrecorded is a fixture, not evidence, and the corpus harness refuses it.
Files from one session stay separate for the same reason as above: `captures/2026-09-04.jsonl`,
`-02` and `-03` are one night split by device restarts, and each restart re-probed the board,
so each file's header describes its own run.

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

1. Sensible default retention policy per room — depends on observed message volume.
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
   cheaply. **Still open after milestone 5**, deliberately: that milestone's live work was
   receive-only and involved no peer exchange, so no opportunity to observe it arose, and it
   is not closed on inference. What did change is the cost of testing it — durable contacts
   mean the exchange no longer has to complete inside one process lifetime, so the peer can
   be adverted to on one run and answered on another.

**Telemetry sub-command availability**, previously unknown #1, is settled from the firmware
source: `getMCUTemperature()` comes from the shared `src/helpers/ESP32Board.h` and both the
V3 and V4 variants implement `getBattMilliVolts()`, so `GetBattery` and `GetMCUTemp` answer
on both boards. `GetSensors` answers on both but with a build-flag-dependent payload shape.
Keep the §4.1 startup probe regardless — it costs nothing, it records provenance for the
capture header, and it is what keeps a third board from being a code change.
