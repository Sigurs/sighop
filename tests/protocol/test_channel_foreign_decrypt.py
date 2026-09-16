"""Channel decryption proven against another implementation (`mesh-crypto`).

`test_foreign_decrypt.py` proves the direct-message constructions against one
exchange with a stock node. This module does the same for group text, and over
far more evidence: every `GRP_TXT` frame in the capture corpus was produced by
MeshCore nodes other than sighop, and the ones on the stock Public channel open
under a key every stock client ships with.

**Provenance.** Every `captures/*.jsonl` file is read — not only the ones the
protocol corpus names — and `GRP_TXT` payloads are deduplicated by their bytes
(a repeated flood is one frame). Measured while planning change
`channel-messaging` and asserted here, per file, in first-seen order:

* `2026-09-02.jsonl` — 8 on `0x11`
* `2026-09-03.jsonl` — 33 on `0x11`
* `2026-09-03-2.jsonl` — 1 on `0x11`, 2 on `0x81`
* `2026-09-04.jsonl` — 1 on `0x11`
* `2026-09-04-03.jsonl` — 18 on `0x11`
* `2026-09-05.jsonl` — 8 on `0x11`, 104 on `0x81`
* `2026-09-06-greeter.jsonl` — 15 on `0x11`
* `2026-09-12-webui-live.jsonl` — 1 on `0x11`, 2 on `0x81`

193 distinct frames: **85 on `0x11`**, every one of which verifies and decrypts
under `PUBLIC_CHANNEL_KEY` and parses as plain group text with a claimed
sender, and **108 on `0x81`**, none of which opens — someone else's channel.

The negative that keeps the key-length rule falsifiable is in `test_crypto.py`:
the Public hash over the zero-extended buffer is `0x17`, which no frame here
carries. HMAC cannot supply that negative, because it zero-pads a short key
itself (DESIGN.md §5).
"""

from __future__ import annotations

import collections
from functools import cache

from sighop.protocol.crypto import PUBLIC_CHANNEL_KEY, mac_then_decrypt
from sighop.protocol.packet import PayloadType, decode
from sighop.protocol.payloads import (
    GroupEnvelope,
    TextType,
    build_group_text_body,
    parse_group_text_body,
    parse_payload,
)
from sighop.protocol.result import DecodeFailure
from tests.protocol.corpus import CAPTURES_DIR, _load_file

PUBLIC_HASH = 0x11
OTHER_HASH = 0x81

EXPECTED_DISTINCT_PER_FILE = {
    ("2026-09-02.jsonl", PUBLIC_HASH): 8,
    ("2026-09-03.jsonl", PUBLIC_HASH): 33,
    ("2026-09-03-2.jsonl", PUBLIC_HASH): 1,
    ("2026-09-03-2.jsonl", OTHER_HASH): 2,
    ("2026-09-04.jsonl", PUBLIC_HASH): 1,
    ("2026-09-04-03.jsonl", PUBLIC_HASH): 18,
    ("2026-09-05.jsonl", PUBLIC_HASH): 8,
    ("2026-09-05.jsonl", OTHER_HASH): 104,
    ("2026-09-06-greeter.jsonl", PUBLIC_HASH): 15,
    ("2026-09-12-webui-live.jsonl", PUBLIC_HASH): 1,
    ("2026-09-12-webui-live.jsonl", OTHER_HASH): 2,
}
EXPECTED_PUBLIC_FRAMES = 85
EXPECTED_OTHER_FRAMES = 108


@cache
def distinct_group_text() -> tuple[tuple[str, GroupEnvelope], ...]:
    """Every distinct `GRP_TXT` envelope across every capture, with its first file."""
    seen: dict[bytes, tuple[str, GroupEnvelope]] = {}
    for path in sorted(CAPTURES_DIR.glob("*.jsonl")):
        for frame in _load_file(path.name):
            packet = decode(frame.raw)
            if isinstance(packet, DecodeFailure):
                continue
            if packet.header.payload_type is not PayloadType.GRP_TXT:
                continue
            if packet.payload in seen:
                continue
            envelope = parse_payload(PayloadType.GRP_TXT, packet.payload)
            assert isinstance(envelope, GroupEnvelope), frame.describe()
            seen[packet.payload] = (path.name, envelope)
    return tuple(seen.values())


def test_the_corpus_holds_the_group_text_frames_this_vector_names() -> None:
    counts = collections.Counter(
        (name, envelope.channel_hash) for name, envelope in distinct_group_text()
    )
    assert dict(counts) == EXPECTED_DISTINCT_PER_FILE
    assert sum(counts.values()) == EXPECTED_PUBLIC_FRAMES + EXPECTED_OTHER_FRAMES


def test_every_public_frame_verifies_decrypts_and_rebuilds() -> None:
    public = [(n, e) for n, e in distinct_group_text() if e.channel_hash == PUBLIC_HASH]
    assert len(public) == EXPECTED_PUBLIC_FRAMES
    for name, envelope in public:
        match, plaintext = mac_then_decrypt(
            PUBLIC_CHANNEL_KEY.secret, envelope.mac, envelope.ciphertext
        )
        assert match.matched, name
        assert plaintext is not None
        body = parse_group_text_body(plaintext)
        assert not isinstance(body, DecodeFailure), name
        assert body.message.txt_type is TextType.PLAIN, name
        assert body.message.attempt == 0, name
        assert body.unverified_sender_name, name
        rebuilt = build_group_text_body(
            body.message.timestamp, body.unverified_sender_name, body.body
        )
        assert rebuilt == plaintext.rstrip(b"\x00"), name


def test_no_frame_on_another_channel_opens_under_public() -> None:
    other = [(n, e) for n, e in distinct_group_text() if e.channel_hash != PUBLIC_HASH]
    assert len(other) == EXPECTED_OTHER_FRAMES
    for name, envelope in other:
        match, plaintext = mac_then_decrypt(
            PUBLIC_CHANNEL_KEY.secret, envelope.mac, envelope.ciphertext
        )
        assert not match.matched, name
        assert plaintext is None
