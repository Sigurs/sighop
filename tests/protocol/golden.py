"""Renderer for the corpus golden file.

Per design D8 the golden file holds **structural fields and ciphertext digests
only — never decrypted plaintext**. The corpus is other people's traffic and
sighop holds no key for any of it, so today the rule costs nothing; it is
written down now so it still holds at milestone 4, when we will hold keys and
the temptation to snapshot a decrypted body will be real.

Regeneration is deliberately not a test flag (design D10). `generate_golden.py`
is a script you run and whose diff you review.
"""

from __future__ import annotations

import hashlib

from sighop.protocol.crypto import VerifiedAdvert, verify_advert
from sighop.protocol.packet import Packet, decode
from sighop.protocol.payloads import (
    Acknowledgement,
    Advert,
    AnonRequestEnvelope,
    DirectEnvelope,
    DiscoverRequest,
    DiscoverResponse,
    GroupEnvelope,
    NodeType,
    TracePayload,
    UnparsedPayload,
    parse_payload,
)
from sighop.protocol.result import DecodeFailure
from tests.protocol.corpus import CorpusFrame, load_corpus

GOLDEN_HEADER = (
    "# sighop protocol corpus golden file\n"
    "# Structural fields and ciphertext digests only - never decrypted plaintext.\n"
    "# Regenerate with `uv run python -m tests.protocol.generate_golden` and review "
    "the diff.\n"
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def _ciphertext(data: bytes) -> str:
    return f"len={len(data)} sha={_digest(data)}"


def render_payload(packet: Packet) -> str:
    parsed = parse_payload(packet.payload_type, packet.payload)
    if isinstance(parsed, DecodeFailure):
        return f"UNPARSEABLE {parsed.reason} {parsed.detail}"

    match parsed:
        case Advert():
            return _render_advert(parsed)
        case DirectEnvelope():
            return (
                f"envelope dest={parsed.dest_hash:02x} src={parsed.src_hash:02x} "
                f"mac={parsed.mac.hex()} {_ciphertext(parsed.ciphertext)}"
            )
        case AnonRequestEnvelope():
            return (
                f"anon_req dest={parsed.dest_hash:02x} "
                f"sender={parsed.sender_public_key.hex()} "
                f"mac={parsed.mac.hex()} {_ciphertext(parsed.ciphertext)}"
            )
        case GroupEnvelope():
            return (
                f"group channel={parsed.channel_hash:02x} mac={parsed.mac.hex()} "
                f"{_ciphertext(parsed.ciphertext)}"
            )
        case Acknowledgement():
            tail = parsed.tail.hex() if parsed.tail else "-"
            return f"ack checksum={parsed.checksum.hex()} tail={tail}"
        case TracePayload():
            return f"trace {_ciphertext(parsed.raw)}"
        case DiscoverRequest():
            since = "-" if parsed.since is None else str(parsed.since)
            return (
                f"discover_req flags={parsed.flags:x} filter={parsed.type_filter:02x} "
                f"tag={parsed.tag.hex()} since={since}"
            )
        case DiscoverResponse():
            # The key is public and unauthenticated — a claim, recorded as sent.
            return (
                f"discover_resp node_type={parsed.node_type} snr_q={parsed.snr_quarter_db} "
                f"tag={parsed.tag.hex()} claimed_key={parsed.claimed_key.hex()}"
            )
        case UnparsedPayload():
            return f"unparsed {_ciphertext(parsed.raw)}"


def _render_advert(advert: Advert) -> str:
    verified = verify_advert(advert)
    if not isinstance(verified, VerifiedAdvert):
        return (
            f"advert key={advert.public_key.hex()} ts={advert.timestamp} "
            f"UNVERIFIED reason={verified.reason}"
        )
    appdata = verified.appdata
    node_type = (
        appdata.node_type.name
        if isinstance(appdata.node_type, NodeType)
        else f"future({int(appdata.node_type)})"
    )
    parts = [
        f"advert key={advert.public_key.hex()}",
        f"ts={advert.timestamp}",
        "sig=ok",
        f"flags=0x{appdata.flags:02x}",
        f"type={node_type}",
    ]
    if appdata.latitude is not None:
        parts.append(f"loc={appdata.latitude},{appdata.longitude}")
    if appdata.feature1 is not None:
        parts.append(f"feat1=0x{appdata.feature1:04x}")
    if appdata.feature2 is not None:
        parts.append(f"feat2=0x{appdata.feature2:04x}")
    if appdata.name is not None:
        flag = "" if appdata.name.is_valid_utf8 else " name_utf8=invalid"
        parts.append(f"name={appdata.name.text!r}{flag}")
    if appdata.trailing:
        parts.append(f"trailing={appdata.trailing.hex()}")
    return " ".join(parts)


def render_frame(frame: CorpusFrame) -> str:
    """One golden line for one corpus frame."""
    packet = decode(frame.raw)
    if isinstance(packet, DecodeFailure):
        return (
            f"{frame.capture_file}[{frame.index:03d}] "
            f"DECODE_FAILED {packet.reason} offset={packet.offset} "
            f"raw={frame.raw.hex()}"
        )
    codes = (
        "-"
        if packet.transport_codes is None
        else f"{packet.transport_codes[0]:04x},{packet.transport_codes[1]:04x}"
    )
    return (
        f"{frame.capture_file}[{frame.index:03d}] "
        f"route={packet.route_type.name} type={packet.payload_type.name} "
        f"v{packet.header.payload_version} "
        f"tc={codes} "
        f"hops={packet.hop_count}x{packet.hash_size} "
        f"path={packet.path.hex() or '-'} "
        f"| {render_payload(packet)}"
    )


def render_corpus() -> str:
    """The full golden file content."""
    lines = [render_frame(frame) for frame in load_corpus()]
    return GOLDEN_HEADER + "\n".join(lines) + "\n"
