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
| DB access | SQLAlchemy 2.0 async + asyncpg, Alembic migrations. | Spec requires ORM + Alembic. |
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

**Airtime budget.** A token bucket over a rolling window, computed from LoRa time-on-air for
the configured SF/BW/CR — not packet count, since airtime varies by an order of magnitude
across presets. Time-on-air is calculated from the *live* radio configuration read back via
`GetRadio`, never from an assumed preset: the EU narrow preset and the legacy 250 kHz/SF11
preset differ by more than an order of magnitude per packet.

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
| 64 B (typical) | ~0.64 s |
| 255 B (maximum) | ~2.2 s |

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
MeshCore node accepts. It remains a checksum rather than a cryptographic proof — it is
unkeyed, so anyone who can read the plaintext can reproduce it. Treat an ACK as delivery
evidence only, never as authentication.

Group messages carry **no sender authentication** — the sender name is plain text inside the
ciphertext (`<name>: <body>`). Anyone with the channel key can claim any name. The WebUI
must not render channel sender names in a way that implies verified identity.

---

## 6. Persistence

Postgres via SQLAlchemy 2.0 async. Sketch, not final DDL:

- **entity** — id, type, name, public key, **encrypted** private key, advert flags/config, enabled
- **contact** — public key, node hash, name, first/last heard, advert flags, location
- **path** — destination public key, path bytes, hop SNRs, learned/confirmed timestamps, score
- **room** — entity ref, guest/admin password hashes, retention policy
- **room_member** — room ref, contact ref, ACL level, last-sync timestamp, joined-at
- **message** — room ref, sender contact ref, timestamp, body, delivery state
- **packet_log** — ring buffer of recent RX/TX for the observability UI (bounded; not the audit trail)
- **bot_state** — bot ref, key/value scratch (the greeter's already-greeted set lives here)

Message history is durable and survives reboot, per spec — the whole reason a room server
beats a walkie-talkie.

`packet_log` is a bounded ring buffer, aggressively pruned. It exists to power the live
feed, not to be a permanent record; unbounded packet logging on a busy mesh will fill a
disk.

### Private keys at rest

Entity private keys are the platform's crown jewels — they *are* the identities. Encrypt
them at rest with a key from the environment (`SIGHOP_SECRET_KEY`), never in the database.
A DB dump must not be sufficient to impersonate a room server. Provide `sighop keys export`
/ `import` so operators can back identities up deliberately, and make the WebUI's key
display an explicit, audited action.

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
│   ├── radio/          kiss transport, modem, framing
│   ├── protocol/       packet codec, payloads, crypto, path learning
│   ├── net/            bus, rx pipeline, tx scheduler, airtime
│   ├── entities/       base, room server, companion, bot runtime
│   │   └── bots/       greeter
│   ├── db/             models, repositories
│   ├── web/            FastAPI app, routes, templates
│   ├── logging.py      structlog config, wide-event helpers
│   ├── config.py
│   └── cli.py
├── alembic/
├── tests/
├── compose.yaml
├── build.sh
├── Dockerfile
└── pyproject.toml
```

`protocol/` must have no dependency on `db/` or `net/` — it is pure functions over bytes.
That is what makes it testable against captured packets, and it is the layer where
correctness matters most.

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
   *Note the corpus's one hard limit:* every encrypted payload in it is addressed to a third
   party, so it proves framing, adverts and signature verification but **cannot prove
   decryption**. Ciphertext handling is verifiable here only by round-trip and by fixed
   known-answer vectors against the firmware source; milestone 4 is what actually closes it.
2. **Live decode.** Point the decoder at the live link. Adverts, names, paths and SNR
   printing in real time. *This is where the design is proven or isn't* — and it is still
   entirely receive-only.
3. **Bus and scheduler.** RX fan-out with dedup, TX scheduler with airtime budget and the
   duty-cycle ceiling under test. Transmit still disabled; verify the budget accounting
   against what *would* have been sent.
4. **First transmit.** Enable TX. One hardcoded companion entity exchanges a DM with the
   second board, running stock MeshCore firmware as the reference peer. The first packet
   sighop puts on the air should be a deliberate, watched event. A dedicated peer board
   (rather than the live mesh) keeps this repeatable and keeps our debugging off other
   people's networks. **Exit criterion: the first successful decrypt of a real MeshCore DM**
   — this is the first point at which the cipher, MAC and ECDH are confirmed against another
   implementation rather than against ourselves.
5. **Persistence.** Postgres, models, Alembic, entity identity storage with key encryption.
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

The existing `captures/2026-09-02.jsonl` and `captures/2026-09-03.jsonl` predate this and
have no header. Their frames are left untouched; provenance lives in the sidecar
`captures/2026-09-02.meta.json` and `captures/2026-09-03.meta.json`, which separate what was
**observed** (recomputed from each file and its paired log) from what was **reconstructed**
(stated from memory afterwards) and record the hardware readback as explicitly absent.
Reconstructed radio settings are not evidence — if a decoder disagreement ever turns on them,
re-capture with a real header rather than trusting them. The two files are kept as separate
captures rather than merged — the second run started independently (~12 min after the first's
`capture_stopped`) and each carries its own provenance.

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
2. Whether the dedup cache should be sized by entries or by time, once we have seen real
   flood-repetition rates in the milestone 0 capture.
3. Whether path scoring needs more than "most recently confirmed wins" — only observable
   once multiple routes to the same peer exist.

**Telemetry sub-command availability**, previously unknown #1, is settled from the firmware
source: `getMCUTemperature()` comes from the shared `src/helpers/ESP32Board.h` and both the
V3 and V4 variants implement `getBattMilliVolts()`, so `GetBattery` and `GetMCUTemp` answer
on both boards. `GetSensors` answers on both but with a build-flag-dependent payload shape.
Keep the §4.1 startup probe regardless — it costs nothing, it records provenance for the
capture header, and it is what keeps a third board from being a code change.
