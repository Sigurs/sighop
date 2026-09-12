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

Postgres via SQLAlchemy 2.0 async. The eight-table sketch below is the original one. As of
milestone 7 **all eight exist**, and a ninth the sketch did not have joined the last of
them. Milestone 8 adds a tenth.

**Built (milestone 5, migration `0001`):**

- **entity** — id, type, name, public key, **sealed** seed, advert config (JSONB), enabled,
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
must not be sufficient to *impersonate* a room server — `entity.sealed_seed` is ciphertext
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
disk. Milestone 8 gave it its first **read** — `recent(limit)`, newest first, capped, under
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
expects, and never migrates as a side effect of starting.

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

`on_channel_message` is **absent, and its absence is intent.** No channel key store exists
and nothing in `net/` decrypts `GRP_TXT`, so a hook declared here could never fire — a
promise the runtime cannot keep, that a driver author would nonetheless write against. It
arrives with channels.

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

- **The interface binds to loopback by default, and a wider bind is a deliberate,
  announced operator decision.** *(Corrected in milestone 8. This section previously said
  "no unauthenticated mode, at any milestone"; milestone 8 ships the WebUI before
  authentication exists, so that rule described something the code does not enforce. What
  is enforced instead: `sighop run --web` defaults `--web-host` to `127.0.0.1`; any other
  address is permitted and is reported at startup — in the run's output and as its own
  logged event, with no option that suppresses it — as unauthenticated, reachable from the
  network, and able to transmit and reveal private key material. Milestone 9 closes the
  gap; until then it is stated rather than implied.)*
- **Because there is no authentication, request provenance is enforced.** Every
  state-changing request carries a token issued by this process and present only in pages
  it served, and every request's `Host` header must be one the interface was configured to
  answer to. Neither is authentication: they are the difference between "reachable by
  anything that can route to the port" and "reachable by anything that can render a page in
  the operator's browser", and CSRF and DNS rebinding are what make that difference matter
  (milestone 8, design D9).
- Actions that reveal a private key, enable transmit, or raise the duty-cycle ceiling are
  re-authenticated and logged as their own wide events with the acting user recorded. Until
  milestone 9 the re-authentication is a per-action confirmation carrying a one-shot nonce,
  and the actor field reads `unauthenticated` — the only part of those events that changes
  when real users arrive.

Reverse-proxy trust is deliberately *not* supported in v1. It is a reasonable deployment
pattern, but "trust this header" is a footgun that turns one proxy misconfiguration into
unauthenticated key access, and it can be added later without disturbing anything.

### What the interface deliberately does not expose

The browser reaches every `sighop` capability an operator administers a node with, with two
exceptions. Both are deliberate, both are stated *in the interface* at the point an
operator would look for them rather than only here, and neither is a gap waiting to be
closed by whoever notices it first.

- **Applying a migration.** `sighop db upgrade` has no browser equivalent. §6 makes
  applying a migration an act an operator takes on purpose and never a side effect of
  starting something, and this build's port has no authentication — so offering it here
  would make schema migration reachable by anything that can route to that port. The schema
  page shows the applied and expected revisions, says the two disagree when they do, gives
  the command that reconciles them, and says why the button is not there.
- **Generating the sealing secret.** `sighop keys secret` has no browser equivalent for a
  smaller reason: it prints a value once that must be kept and must never be regenerated —
  losing it makes every stored identity unrecoverable — and a browser is a poor place to
  hand somebody something they must not lose. The identities page says so.

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
│   │                   contacts.py, dm.py (direct messages, both directions),
│   │                   room.py (the room server), acks.py, pathbodies.py
│   ├── monitor/        render.py (pure formatting), run.py (`sighop monitor`)
│   ├── keystore.py     entity keyfiles (`sighop keys`) — file I/O, so not
│   │                   under protocol/
│   ├── runtime.py      the whole pipeline wired together (`sighop run`)
│   ├── bots/           base.py (the Bot protocol and BotContext),
│   │                   runtime.py (dispatch, limits, mode, state),
│   │                   drivers.py (the registry), greeter.py
│   ├── db/             models.py (the four tables), repositories.py (what net/
│   │                   calls), engine.py (pool, bounds, degraded state, probe),
│   │                   writer.py (bounded write-behind), sealing.py (seeds at
│   │                   rest), packetlog.py (feed rows, pruner),
│   │                   persistence.py (the wiring), migrations.py (alembic)
│   ├── web/            state.py (the read seam as Protocols), app.py (the
│   │                   application and the bound socket), guard.py (host check,
│   │                   provenance token, one wide event per request),
│   │                   guarded.py (confirm-then-act and its audit event),
│   │                   feed.py (one bus subscription, per-connection queues),
│   │                   chat.py (this run's own conversations),
│   │                   serialize.py, render.py (view models), deps.py,
│   │                   routes/, templates/, static/ (vendored htmx, no bundler)
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
`web/state.py` that `Runtime` satisfies structurally, and `cli.py` is the only module in the
project that knows both sides (milestone 8, design D2).

`bots/` never imports `monitor/`, for the same reason `net/` does not: it emits typed events
and `monitor/render.py` turns one into a line. It imports no SQLAlchemy either — its storage
seam is read-only-property `Protocol`s the way the room server's is, which is what keeps
every dispatch, limit, mode and gate test runnable with no database configured.

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
     peer decrypts by trying the contacts it holds — and `[redacted]` had been created
     eight minutes earlier with a 24 h flood interval, so it had never adverted and the peer
     held nothing for it. **Greeting a stranger is a two-packet problem and the design had
     modelled it as one.** The greeter now adverts first: zero-hop for a direct neighbour,
     and a flood only on a retry to a peer heard over a repeater, because that one is
     repeated by the whole mesh and is not spent on a guess. The advert is *awaited* — class
     3 against the message's class 2 means an un-awaited send is transmitted first and lands
     just as unreadable.
   - **Deferring the flood to the next cooldown reproduced the same failure at one hop.** The
     second exercise fixed the zero-hop case — two direct neighbours greeted and acknowledged
     — and then `[redacted]`, at one hop, was greeted bare, four times, `announced=0`, and never
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
   browser — was met on 2026-09-12: a direct message composed in Chromium as `[redacted]`,
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
   opportunity to observe it, since the client learned `[redacted]` purely from its zero-hop
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
