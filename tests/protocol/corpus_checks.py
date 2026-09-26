"""Checks over corpus text: what it names, and how it repeats.

Kept apart from the generator so a check can be run against a copy of the corpus
that has been tampered with, which is how the tests prove each one can fail
(`test_synthetic_corpus.py`). Both work on `{file name: text}` and know nothing
about where the text came from.

The no-real-data guard checks membership in the **cast table** the generator
builds, not a denylist of real names, so it needs no real data to exist anywhere
in the repository to do its job.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

from sighop.protocol.crypto import (
    PUBLIC_CHANNEL_KEY,
    calc_shared_secret,
    mac_then_decrypt,
)
from sighop.protocol.crypto import verify_advert as verify
from sighop.protocol.packet import Packet, PayloadType, RouteType, decode
from sighop.protocol.payloads import (
    Advert,
    AnonRequestEnvelope,
    DirectEnvelope,
    DiscoverResponse,
    GroupEnvelope,
    parse_group_text_body,
    parse_payload,
    parse_room_login_body,
    parse_text_message_body,
)
from sighop.protocol.payloads import parse_returned_path_body as parse_path
from sighop.protocol.result import DecodeFailure
from tests.protocol.synthetic import (
    GROUP_DATA_PAYLOADS,
    LATE_ECHO_MIN_SECONDS,
    MAX_COPIES,
    REPEAT_CEILING,
    RETRANSMIT_MIN_SECONDS,
    Cast,
    CastNode,
    cast_texts,
    club_key,
)

FRAME_KINDS = ("rx_frame", "tx_frame")


@dataclass(frozen=True, slots=True)
class Record:
    """One corpus line, located well enough to name it in a failure."""

    file: str
    line: int
    data: dict

    @property
    def location(self) -> str:
        return f"{self.file} line {self.line}"

    @property
    def is_frame(self) -> bool:
        return self.data.get("kind") in FRAME_KINDS

    @property
    def raw(self) -> bytes:
        return bytes.fromhex(self.data["raw_hex"])


def records(files: Mapping[str, str]) -> Iterator[Record]:
    for name, text in files.items():
        for number, line in enumerate(text.splitlines(), start=1):
            if line.strip():
                yield Record(name, number, json.loads(line))


# --- No real data ------------------------------------------------------------


def real_data_violations(files: Mapping[str, str], cast: Cast) -> list[str]:
    """Everything in the corpus that is not from the cast, one line each.

    Each line names the file, the record and the offending value. An empty list
    means every advert names a cast key and a cast name, every encrypted frame
    opens with a cast key, and every sender and text it opens to is written into
    the cast's message set.
    """
    texts = cast_texts()
    problems: list[str] = []

    def bad(record: Record, what: str, value: str) -> None:
        problems.append(f"{record.location}: {what} {value!r} is not from the cast")

    for record in records(files):
        data = record.data
        if data.get("kind") == "capture_meta":
            if data.get("synthetic") is not True:
                problems.append(f"{record.location}: capture_meta does not say it is synthetic")
            continue
        if not record.is_frame:
            continue
        packet = decode(record.raw)
        if isinstance(packet, DecodeFailure):
            problems.append(f"{record.location}: frame does not decode: {packet}")
            continue
        _check_frame(record, packet, cast, texts, bad, problems)
    return problems


def _check_frame(record, packet: Packet, cast: Cast, texts, bad, problems: list[str]) -> None:
    parsed = parse_payload(packet.payload_type, packet.payload)
    if isinstance(parsed, DecodeFailure):
        problems.append(f"{record.location}: payload does not parse: {parsed}")
        return
    if isinstance(parsed, Advert):
        if cast.by_key(parsed.public_key) is None:
            bad(record, "advert key", parsed.public_key.hex())
        verified = verify(parsed)
        name = getattr(getattr(getattr(verified, "appdata", None), "name", None), "text", None)
        if name is None:
            problems.append(f"{record.location}: advert does not verify or carries no name")
        elif name not in cast.names:
            bad(record, "advert name", name)
    elif isinstance(parsed, DiscoverResponse):
        if cast.by_key(parsed.claimed_key) is None:
            bad(record, "discovery response key", parsed.claimed_key.hex())
    elif isinstance(parsed, DirectEnvelope):
        _check_direct(record, parsed, cast, texts, bad, problems)
    elif isinstance(parsed, AnonRequestEnvelope):
        _check_anon(record, parsed, cast, texts, bad, problems)
    elif isinstance(parsed, GroupEnvelope):
        _check_group(record, parsed, cast, texts, bad, problems)


def _check_direct(record, envelope: DirectEnvelope, cast, texts, bad, problems) -> None:
    by_hash = {node.identity.node_hash: node for node in cast.everyone}
    source, dest = by_hash.get(envelope.src_hash), by_hash.get(envelope.dest_hash)
    if source is None or dest is None:
        bad(record, "node hash", f"{envelope.src_hash:02x}->{envelope.dest_hash:02x}")
        return
    secret = calc_shared_secret(dest.identity, source.public_key)
    match, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
    if not match.matched or plaintext is None:
        bad(record, "ciphertext between", f"{source.name}->{dest.name}")
        return
    if envelope.payload_type is PayloadType.TXT_MSG:
        body = parse_text_message_body(plaintext)
        if isinstance(body, DecodeFailure) or body.text.text not in texts:
            text = "unparsable" if isinstance(body, DecodeFailure) else body.text.text
            bad(record, "message text", text)
    elif envelope.payload_type is PayloadType.PATH:
        if isinstance(parse_path(plaintext), DecodeFailure):
            problems.append(f"{record.location}: returned path body does not parse")


def _check_anon(record, envelope: AnonRequestEnvelope, cast, texts, bad, problems) -> None:
    sender = cast.by_key(envelope.sender_public_key)
    if sender is None:
        bad(record, "anonymous-request sender key", envelope.sender_public_key.hex())
        return
    dest = next((n for n in cast.everyone if n.identity.node_hash == envelope.dest_hash), None)
    if dest is None:
        bad(record, "anonymous-request destination hash", f"{envelope.dest_hash:02x}")
        return
    secret = calc_shared_secret(dest.identity, sender.public_key)
    match, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
    if not match.matched or plaintext is None:
        bad(record, "ciphertext between", f"{sender.name}->{dest.name}")
        return
    login = parse_room_login_body(plaintext)
    if isinstance(login, DecodeFailure) or login.password.text not in texts:
        bad(record, "room password", "unparsable" if isinstance(login, DecodeFailure) else "?")


def _check_group(record, envelope: GroupEnvelope, cast, texts, bad, problems) -> None:
    for key in (PUBLIC_CHANNEL_KEY, club_key()):
        if key.channel_hash != envelope.channel_hash:
            continue
        match, plaintext = mac_then_decrypt(key.secret, envelope.mac, envelope.ciphertext)
        if not match.matched or plaintext is None:
            continue
        if envelope.payload_type is PayloadType.GRP_DATA:
            if plaintext[4:].rstrip(b"\x00") not in GROUP_DATA_PAYLOADS:
                bad(record, "group data", plaintext[4:].hex())
            return
        body = parse_group_text_body(plaintext)
        if isinstance(body, DecodeFailure):
            problems.append(f"{record.location}: group text does not parse")
            return
        if body.unverified_sender_name not in cast.names:
            bad(record, "sender name", str(body.unverified_sender_name))
        if body.body not in texts:
            bad(record, "message text", body.body)
        return
    bad(record, "channel hash", f"{envelope.channel_hash:02x}")


# --- Private-key material ----------------------------------------------------

PRIVATE_KEY_PATTERNS = (
    re.compile(r'"private_key_hex"\s*:\s*"[0-9a-fA-F]{64,}"'),
    re.compile(r'"seed_hex"\s*:\s*"[0-9a-fA-F]{64}"'),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)
"""A keyfile with a literal key in it, or a PEM private key. Test code that builds
its keys in memory holds none of these; a committed keyfile holds one."""


def tracked_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=False, text=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"`git ls-files` failed in {root}: {result.stderr.decode(errors='replace').strip()}"
        )
    return [root / name.decode() for name in result.stdout.split(b"\0") if name]


def private_key_material(paths: list[Path], *, exclude: set[Path] | None = None) -> list[str]:
    """Every tracked file that holds what looks like a private key, by path."""
    found: list[str] = []
    for path in paths:
        if exclude and path in exclude:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for pattern in PRIVATE_KEY_PATTERNS:
            if pattern.search(text):
                found.append(f"{path}: matches {pattern.pattern}")
                break
    return found


# --- Repetition --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reception:
    file: str
    line: int
    at: float
    raw: bytes
    packet: Packet
    snr: float | None
    rssi: int | None

    @property
    def content(self) -> tuple[int, bytes]:
        """The dedup key's content: payload type and payload, and nothing else."""
        return int(self.packet.payload_type), self.packet.payload


@dataclass(frozen=True, slots=True)
class Repetition:
    receptions: int
    copies: dict[tuple[int, bytes], list[Reception]]

    @property
    def repeats(self) -> int:
        return sum(len(group) - 1 for group in self.copies.values())

    @property
    def share(self) -> float:
        return self.repeats / self.receptions if self.receptions else 0.0

    @property
    def max_copies(self) -> int:
        return max((len(group) for group in self.copies.values()), default=0)

    def repeated(self) -> list[list[Reception]]:
        return [group for group in self.copies.values() if len(group) > 1]


def repetition(files: Mapping[str, str]) -> Repetition:
    """Group every reception by content, over the whole corpus.

    Only receptions count — a frame sighop transmitted is not something the mesh
    sent, and a duplicate rate over it would measure the wrong thing.
    """
    import datetime as dt

    copies: dict[tuple[int, bytes], list[Reception]] = defaultdict(list)
    receptions = 0
    for record in records(files):
        if record.data.get("kind") != "rx_frame":
            continue
        packet = decode(record.raw)
        assert not isinstance(packet, DecodeFailure), record.location
        meta = record.data.get("rx_meta") or {}
        reception = Reception(
            file=record.file,
            line=record.line,
            at=dt.datetime.fromisoformat(record.data["ts"]).timestamp(),
            raw=record.raw,
            packet=packet,
            snr=meta.get("snr_db"),
            rssi=meta.get("rssi_dbm"),
        )
        receptions += 1
        copies[reception.content].append(reception)
    return Repetition(receptions, dict(copies))


def is_flood(reception: Reception) -> bool:
    return reception.packet.route_type in (RouteType.FLOOD, RouteType.TRANSPORT_FLOOD)


def cast_lookup(cast: Cast) -> dict[bytes, CastNode]:
    return {node.public_key: node for node in cast.everyone}


FLOOD_REPEAT_MAX_SECONDS = 10.0
"""An ordinary flood repeat arrives within a few seconds of the first copy."""


def repeat_violations(files: Mapping[str, str], *, declared: int) -> list[str]:
    """Where the corpus repeats itself beyond what the generator declared.

    `declared` is the number of duplicate receptions the generator says it
    emitted on purpose. Anything above it is padding; the rest must look like what
    it was declared to be.
    """
    found = repetition(files)
    problems: list[str] = []
    if found.share > REPEAT_CEILING:
        problems.append(
            f"{found.repeats} of {found.receptions} receptions repeat a packet "
            f"({found.share:.1%}), over the ceiling of {REPEAT_CEILING:.0%}"
        )
    for name in files:
        in_file = repetition({name: files[name]})
        if in_file.share > REPEAT_CEILING:
            problems.append(f"{name}: {in_file.share:.1%} of receptions repeat a packet")
    if found.max_copies > MAX_COPIES:
        problems.append(f"a packet has {found.max_copies} copies; at most {MAX_COPIES} are allowed")
    if found.repeats != declared:
        problems.append(
            f"{found.repeats} repeated receptions, but the generator declares {declared}: "
            "something repeats that nothing accounts for"
        )

    late: list[list[Reception]] = []
    retransmitted: list[list[Reception]] = []
    ordinary: list[list[Reception]] = []
    for group in found.repeated():
        first, rest = group[0], group[1:]
        gap = rest[-1].at - first.at
        if is_flood(first) and gap > LATE_ECHO_MIN_SECONDS:
            late.append(group)
        elif first.packet.payload_type is PayloadType.TXT_MSG and any(
            copy.raw == first.raw and copy.at - first.at > RETRANSMIT_MIN_SECONDS for copy in rest
        ):
            retransmitted.append(group)
        else:
            ordinary.append(group)
    if len(late) != 1:
        problems.append(
            f"{len(late)} flood packets have a copy more than 60 s late; exactly 1 must"
        )
    elif late[0][-1].packet.path == late[0][0].packet.path:
        problems.append(
            f"{late[0][0].file} line {late[0][0].line}: the late echo took the same path"
        )
    if len(retransmitted) != 1:
        problems.append(
            f"{len(retransmitted)} direct messages are retransmitted identically after "
            "more than 30 minutes; exactly 1 must be"
        )
    for group in ordinary:
        first = group[0]
        where = f"{first.file} line {first.line}"
        if not is_flood(first):
            problems.append(f"{where}: a non-flood packet repeats, and nothing declares why")
            continue
        for copy in group[1:]:
            if copy.at - first.at > FLOOD_REPEAT_MAX_SECONDS:
                problems.append(f"{where}: a flood repeat arrives {copy.at - first.at:.1f} s late")
            if copy.packet.path == first.packet.path:
                problems.append(f"{where}: a flood repeat took the same path")
            if copy.packet.hop_count == first.packet.hop_count:
                problems.append(f"{where}: a flood repeat has the same hop count")
            if (copy.snr, copy.rssi) == (first.snr, first.rssi):
                problems.append(f"{where}: a flood repeat has the same signal readings")
    return problems
