"""The synthetic corpus: generated, deterministic, from the cast, and bounded.

These tests hold the generator to `synthetic-corpus`: the committed files are what
the generator writes (drift), nothing in them is real (the guard), every shape the
protocol layer must handle is present (variety), and the corpus repeats itself only
where a test needs it to (the budget). Each check is also run against a tampered
copy, so a check that could not fail cannot pass unnoticed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sighop.protocol.crypto import (
    PUBLIC_CHANNEL_KEY,
    VerifiedAdvert,
    ack_checksum_for,
    calc_shared_secret,
    mac_then_decrypt,
    sign_advert,
)
from sighop.protocol.crypto import verify_advert as verify
from sighop.protocol.packet import PayloadType, RouteType, decode
from sighop.protocol.payloads import (
    Advert,
    DirectEnvelope,
    DiscoverRequest,
    DiscoverResponse,
    GroupEnvelope,
    NodeType,
    parse_advert,
    parse_appdata,
    parse_group_text_body,
    parse_payload,
    parse_text_message_body,
)
from sighop.protocol.result import DecodeFailure
from sighop.radio.capture import CAPTURE_META_KIND
from sighop.radio.replay import CaptureReplay
from tests.protocol import generate_corpus
from tests.protocol.corpus import (
    AMBIENT,
    CHANNELS,
    CORPUS_DIR,
    CORPUS_FILES,
    EXCHANGE,
)
from tests.protocol.corpus_checks import (
    private_key_material,
    real_data_violations,
    records,
    repeat_violations,
    repetition,
    tracked_files,
)
from tests.protocol.synthetic import (
    GENERATOR_VERSION,
    MAX_COPIES,
    REPEAT_CEILING,
    SEED,
    build_cast,
    club_key,
    declared_repeats,
    generate,
    manifest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
"""The repository root, for the tracked-file scan."""


@pytest.fixture(scope="module")
def cast():
    return build_cast()


@pytest.fixture(scope="module")
def generated() -> dict[str, str]:
    return generate()


@pytest.fixture(scope="module")
def committed() -> dict[str, str]:
    return {name: (CORPUS_DIR / name).read_text(encoding="utf-8") for name in CORPUS_FILES}


def _frames(files: dict[str, str], name: str) -> list:
    """Each frame of a file, decoded, with its record."""
    out = []
    for record in records({name: files[name]}):
        if record.is_frame:
            packet = decode(record.raw)
            assert not isinstance(packet, DecodeFailure), record.location
            out.append((record, packet))
    return out


# --- The cast and determinism (2.1) -------------------------------------------


def test_the_same_seed_gives_the_same_identities(cast) -> None:
    again = build_cast(SEED)
    assert [n.public_key for n in cast.everyone] == [n.public_key for n in again.everyone]
    assert [n.name for n in cast.everyone] == [n.name for n in again.everyone]


def test_a_different_seed_gives_different_identities(cast) -> None:
    other = build_cast(SEED + 1)
    assert not cast.public_keys & other.public_keys, "an identity does not depend on the seed"
    assert cast.names == other.names, "names are the cast's, keys are the seed's"


def test_a_different_seed_gives_a_different_corpus(generated) -> None:
    other = generate(SEED + 1)
    for name in CORPUS_FILES:
        assert other[name] != generated[name], f"{name} does not depend on the seed"


def test_every_cast_name_is_marked_fictional_and_every_node_hash_is_its_own(cast) -> None:
    assert all(name.startswith("syn-") for name in cast.names)
    hashes = [node.identity.node_hash for node in cast.everyone]
    assert len(set(hashes)) == len(hashes), "two cast nodes share a 1-byte path hash"
    assert not {0x00, 0xFF} & set(hashes)


def test_the_cast_is_deliberately_varied(cast) -> None:
    assert len(cast.repeaters) == 12
    assert len(cast.rooms) == 3
    assert len(cast.chat) == 8
    assert any(node.location is None for node in cast.chat)
    assert any(node.location is not None for node in cast.chat)
    assert cast.lounge.location is None, "the hosted room advertises with no location"


def test_generation_is_identical_in_another_process(generated) -> None:
    """Different process, different hash seed, different time: the same bytes."""
    code = (
        "import json; from tests.protocol.synthetic import generate; "
        "print(json.dumps(generate(), sort_keys=True))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": "random", "PYTHONPATH": f"{REPO_ROOT}:{REPO_ROOT / 'src'}"},
    )
    assert json.loads(result.stdout) == generated


# --- The records are the capture writer's (2.2) -------------------------------


def test_every_file_replays_through_the_capture_reader(generated, tmp_path) -> None:
    for name in CORPUS_FILES:
        replay = CaptureReplay.open(CORPUS_DIR / name)
        events = list(replay.read())
        rx = sum(1 for r in records({name: generated[name]}) if r.data["kind"] == "rx_frame")
        tx = sum(1 for r in records({name: generated[name]}) if r.data["kind"] == "tx_frame")
        unparsed = sum(1 for r in records({name: generated[name]}) if r.data["kind"] == "unparsed")
        assert replay.unreadable == [], name
        assert len(events) == rx + unparsed, name
        assert replay.transmitted_skipped == tx, name
        assert replay.provenance is not None and replay.provenance["kind"] == CAPTURE_META_KIND


def test_records_have_the_shapes_the_capture_writer_writes(generated) -> None:
    for name in CORPUS_FILES:
        rows = list(records({name: generated[name]}))
        assert rows[0].data["kind"] == CAPTURE_META_KIND, "the header is the first line"
        assert sum(1 for r in rows if r.data["kind"] == CAPTURE_META_KIND) == 1
        for row in rows[1:]:
            kind = row.data["kind"]
            keys = set(row.data)
            if kind == "rx_frame":
                assert keys == {"ts", "kind", "raw_hex", "rx_meta"}, row.location
                assert set(row.data["rx_meta"]) == {"snr_db", "rssi_dbm"}, row.location
            elif kind == "tx_frame":
                assert keys == {"ts", "kind", "raw_hex", "packet_id", "airtime_ms"}, row.location
            else:
                assert kind == "unparsed", row.location
                assert keys == {"ts", "kind", "raw_hex", "reason"}, row.location


def test_the_header_states_it_is_synthetic_and_describes_no_real_device(generated) -> None:
    for name in CORPUS_FILES:
        header = next(iter(records({name: generated[name]}))).data
        assert header["synthetic"] is True
        assert header["generator"]["version"] == GENERATOR_VERSION
        assert header["generator"]["seed"] == SEED
        assert header["device_name"]["value"] == "synthetic (no device)"
        assert "firmware_version" not in header
        assert header["sighop"] == {"version": "synthetic", "commit_hash": "synthetic"}


# --- The ambient file (2.3) ---------------------------------------------------


def _adverts(files, name):
    out = []
    for record, packet in _frames(files, name):
        if packet.payload_type is PayloadType.ADVERT:
            advert = parse_payload(packet.payload_type, packet.payload)
            assert isinstance(advert, Advert)
            verified = verify(advert)
            assert isinstance(verified, VerifiedAdvert), f"{record.location} does not verify"
            out.append((record, packet, advert, verified))
    return out


def test_every_ambient_advert_verifies_and_every_form_is_present(generated) -> None:
    forms = set()
    for _record, _packet, _advert, verified in _adverts(generated, AMBIENT):
        forms.add(verified.appdata.flags)
    assert forms == {0x92, 0x81, 0x91, 0x93, 0x83}, [hex(f) for f in sorted(forms)]


def test_routing_hops_and_hash_sizes_span_what_the_codec_supports(generated) -> None:
    frames = [packet for _, packet in _frames(generated, AMBIENT)]
    assert {RouteType.FLOOD, RouteType.DIRECT, RouteType.TRANSPORT_FLOOD} == {
        p.route_type for p in frames
    }
    assert {p.hop_count for p in frames} == {0, 1, 2, 3, 4, 5}
    assert {p.hash_size for p in frames} == {1, 2, 3}
    transport = [p for p in frames if p.transport_codes is not None]
    assert len(transport) == 1
    assert transport[0].payload_type is PayloadType.ADVERT


def test_ambient_holds_trace_path_and_third_party_traffic(generated) -> None:
    by_type: dict[PayloadType, list] = {}
    for _record, packet in _frames(generated, AMBIENT):
        by_type.setdefault(packet.payload_type, []).append(packet)
    assert {len(p.payload) for p in by_type[PayloadType.TRACE]} == {10, 13}
    for expected in (
        PayloadType.PATH,
        PayloadType.TXT_MSG,
        PayloadType.ACK,
        PayloadType.ANON_REQ,
        PayloadType.REQ,
        PayloadType.RESPONSE,
    ):
        assert by_type.get(expected), f"no {expected.name} in the ambient file"
    assert {len(p.payload) for p in by_type[PayloadType.ACK]} == {4, 6}


def test_discovery_appears_in_every_form_and_only_repeaters_answer(generated, cast) -> None:
    requests: set[int] = set()
    responses = 0
    repeater_keys = {node.public_key for node in cast.repeaters}
    for record, packet in _frames(generated, AMBIENT):
        if packet.payload_type is not PayloadType.CONTROL:
            continue
        parsed = parse_payload(packet.payload_type, packet.payload)
        if isinstance(parsed, DiscoverRequest):
            requests.add(len(packet.payload))
        else:
            assert isinstance(parsed, DiscoverResponse), record.location
            assert len(packet.payload) == 38
            assert parsed.node_type_name is NodeType.REPEATER, record.location
            assert parsed.claimed_key in repeater_keys, record.location
            responses += 1
    assert requests == {6, 10}
    assert responses > 0


def _read_as_one_byte_hashes(raw: bytes):
    """The advert as a reader that ignores the encoded hash size would parse it."""
    offset = 1 + (4 if RouteType(raw[0] & 0x03).has_transport_codes else 0)
    hops = raw[offset] & 0x3F
    payload = raw[offset + 1 + hops :]
    advert = parse_advert(payload)
    if isinstance(advert, DecodeFailure):
        return None
    return parse_appdata(advert.appdata)


def test_multi_hop_adverts_depend_on_the_encoded_hash_size(generated) -> None:
    """Read with 1-byte hashes, a multi-byte-hash advert yields corrupt flags or a
    truncated name: the corpus still discriminates the two readings."""
    checked = 0
    for record, packet, _advert, verified in _adverts(generated, AMBIENT):
        if packet.hash_size == 1 or packet.hop_count < 2:
            continue
        checked += 1
        true_appdata = verified.appdata
        wrong = _read_as_one_byte_hashes(record.raw)
        misread = wrong is None or isinstance(wrong, DecodeFailure)
        if not misread:
            misread = wrong.flags != true_appdata.flags or (
                (wrong.name.text if wrong.name else None) != true_appdata.name.text
            )
        assert misread, f"{record.location} reads the same as 1-byte hashes"
    assert checked >= 4, "the corpus needs multi-hop multi-byte-hash adverts"


# --- The channels file (2.4) --------------------------------------------------


def test_public_frames_carry_hash_0x11_and_decrypt_and_the_second_channel_does_not(
    generated, cast
) -> None:
    club = club_key()
    assert club.channel_hash not in (0x11, 0x17)
    public = other = data = 0
    for record, packet in _frames(generated, CHANNELS):
        envelope = parse_payload(packet.payload_type, packet.payload)
        assert isinstance(envelope, GroupEnvelope), record.location
        under_public, _ = mac_then_decrypt(
            PUBLIC_CHANNEL_KEY.secret, envelope.mac, envelope.ciphertext
        )
        under_club, plaintext = mac_then_decrypt(club.secret, envelope.mac, envelope.ciphertext)
        if envelope.channel_hash == 0x11:
            public += 1
            assert under_public.matched and not under_club.matched, record.location
        else:
            other += 1
            assert envelope.channel_hash == club.channel_hash, record.location
            assert under_club.matched and not under_public.matched, record.location
            if packet.payload_type is PayloadType.GRP_DATA:
                data += 1
            else:
                assert plaintext is not None
                body = parse_group_text_body(plaintext)
                assert not isinstance(body, DecodeFailure)
                assert body.unverified_sender_name in cast.names
    assert public > 0 and other > 0 and data > 0


# --- The exchange file (2.5) --------------------------------------------------


def test_exchange_direct_messages_decrypt_and_every_ack_matches_its_message(
    generated, cast
) -> None:
    by_hash = {node.identity.node_hash: node for node in cast.everyone}
    expected_acks: set[bytes] = set()
    messages = 0
    ack_lengths: set[int] = set()
    for record, packet in _frames(generated, EXCHANGE):
        if packet.payload_type is PayloadType.TXT_MSG:
            envelope = parse_payload(packet.payload_type, packet.payload)
            assert isinstance(envelope, DirectEnvelope)
            source, dest = by_hash[envelope.src_hash], by_hash[envelope.dest_hash]
            secret = calc_shared_secret(dest.identity, source.public_key)
            match, plaintext = mac_then_decrypt(secret, envelope.mac, envelope.ciphertext)
            assert match.matched and plaintext is not None, record.location
            body = parse_text_message_body(plaintext)
            assert not isinstance(body, DecodeFailure), record.location
            expected_acks.add(ack_checksum_for(body, source.public_key))
            messages += 1
    for record, packet in _frames(generated, EXCHANGE):
        if packet.payload_type is PayloadType.ACK:
            ack_lengths.add(len(packet.payload))
            assert packet.payload[:4] in expected_acks, f"{record.location}: no message it answers"
    assert messages >= 8
    assert ack_lengths == {4, 6}, "both acknowledgement forms"


def test_the_exchange_holds_both_directions_and_sighops_own_adverts(generated) -> None:
    rows = [r for r in records({EXCHANGE: generated[EXCHANGE]}) if r.is_frame]
    tx = [r for r in rows if r.data["kind"] == "tx_frame"]
    assert tx and len(tx) < len(rows)
    adverts_sent = [r for r in tx if decode(r.raw).payload_type is PayloadType.ADVERT]
    assert len(adverts_sent) == 2
    assert {r.data["kind"] for r in records({EXCHANGE: generated[EXCHANGE]})} >= {
        "capture_meta",
        "rx_frame",
        "tx_frame",
        "unparsed",
    }


# --- Drift (3.1) --------------------------------------------------------------


def first_difference(name: str, expected: str, actual: str) -> str | None:
    """Where two versions of a corpus file first differ, or None when they do not."""
    if expected == actual:
        return None
    left, right = expected.splitlines(), actual.splitlines()
    for number, (a, b) in enumerate(zip(left, right, strict=False), start=1):
        if a != b:
            return f"{name} differs at line {number}: generated {a[:120]!r}, committed {b[:120]!r}"
    return f"{name} differs in length: generated {len(left)} lines, committed {len(right)}"


def test_the_committed_corpus_has_not_drifted(generated, committed) -> None:
    assert set(committed) == set(generated)
    problems = [
        first_difference(name, generated[name], committed[name])
        for name in CORPUS_FILES
        if generated[name] != committed[name]
    ]
    assert not problems, (
        "the committed corpus differs from what the generator writes; run "
        "`uv run python -m tests.protocol.generate_corpus`, review the diff, and update "
        "the recorded expectations from the manifest: " + "; ".join(p for p in problems if p)
    )


def test_a_single_changed_byte_is_reported_by_file_and_line(committed) -> None:
    text = committed[AMBIENT]
    lines = text.splitlines(keepends=True)
    original = lines[7]
    flipped = original[:-5] + ("0" if original[-5] != "0" else "1") + original[-4:]
    lines[7] = flipped
    message = first_difference(AMBIENT, text, "".join(lines))
    assert message is not None
    assert message.startswith(f"{AMBIENT} differs at line 8")


def test_the_entry_point_writes_the_same_bytes_every_time(
    tmp_path, monkeypatch, capsys, generated
) -> None:
    monkeypatch.setattr(generate_corpus, "CORPUS_DIR", tmp_path)
    generate_corpus.main()
    first = {name: (tmp_path / name).read_bytes() for name in CORPUS_FILES}
    generate_corpus.main()
    second = {name: (tmp_path / name).read_bytes() for name in CORPUS_FILES}
    assert first == second
    assert {name: data.decode() for name, data in first.items()} == generated
    output = capsys.readouterr().out
    assert manifest(generated) in output


# --- No real data (3.2) -------------------------------------------------------


def test_the_committed_corpus_names_nothing_outside_the_cast(committed, cast) -> None:
    assert real_data_violations(committed, cast) == []


def test_a_node_outside_the_cast_is_refused_naming_file_record_and_value(committed, cast) -> None:
    """An advert that verifies, from a key and under a name that are not the cast's."""
    from sighop.protocol.identity import LocalIdentity
    from sighop.protocol.packet import PAYLOAD_VERSION_1, Packet, PacketHeader, encode
    from sighop.protocol.payloads import build_advert, build_appdata

    stranger = LocalIdentity.from_seed(bytes(range(32)))
    appdata = build_appdata(NodeType.CHAT, name="not-from-the-cast")
    raw = encode(
        Packet(
            header=PacketHeader(RouteType.FLOOD, PayloadType.ADVERT, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=build_advert(sign_advert(stranger, 1_788_000_000, appdata)),
        )
    )
    record = json.dumps(
        {
            "ts": "2026-09-05T18:00:00+00:00",
            "kind": "rx_frame",
            "raw_hex": raw.hex(),
            "rx_meta": {"snr_db": 1.0, "rssi_dbm": -90},
        }
    )
    scratch = dict(committed)
    scratch[AMBIENT] = committed[AMBIENT] + record + "\n"
    line = len(scratch[AMBIENT].splitlines())

    problems = real_data_violations(scratch, cast)

    assert any(
        f"{AMBIENT} line {line}" in p and stranger.public_key.hex() in p for p in problems
    ), problems
    assert any(f"{AMBIENT} line {line}" in p and "not-from-the-cast" in p for p in problems)


def test_a_message_outside_the_cast_is_refused(committed, cast) -> None:
    from sighop.protocol.packet import PAYLOAD_VERSION_1, Packet, PacketHeader, encode
    from tests.protocol.synthetic import group_text_payload

    raw = encode(
        Packet(
            header=PacketHeader(RouteType.FLOOD, PayloadType.GRP_TXT, PAYLOAD_VERSION_1),
            transport_codes=None,
            hop_count=0,
            hash_size=1,
            path=b"",
            payload=group_text_payload(
                PUBLIC_CHANNEL_KEY, 1_788_000_000, "somebody-real", "a line the cast did not write"
            ),
        )
    )
    row = json.dumps(
        {
            "ts": "2026-09-05T19:31:00+00:00",
            "kind": "rx_frame",
            "raw_hex": raw.hex(),
            "rx_meta": {"snr_db": 1.0, "rssi_dbm": -90},
        }
    )
    scratch = dict(committed)
    scratch[CHANNELS] = committed[CHANNELS] + row + "\n"
    problems = real_data_violations(scratch, cast)
    assert any("somebody-real" in p for p in problems), problems
    assert any("a line the cast did not write" in p for p in problems), problems


def test_a_header_that_does_not_say_synthetic_is_refused(committed, cast) -> None:
    lines = committed[AMBIENT].splitlines(keepends=True)
    header = json.loads(lines[0])
    header["synthetic"] = False
    lines[0] = json.dumps(header) + "\n"
    scratch = dict(committed)
    scratch[AMBIENT] = "".join(lines)
    assert any("does not say it is synthetic" in p for p in real_data_violations(scratch, cast))


def test_no_tracked_file_holds_private_key_material() -> None:
    """A committed keyfile is what this guard exists for; test code that builds its
    keys in memory holds no literal and is not flagged."""
    here = {Path(__file__).resolve(), (Path(__file__).parent / "corpus_checks.py").resolve()}
    found = private_key_material(tracked_files(REPO_ROOT), exclude=here)
    assert found == [], "private-key material is tracked:\n" + "\n".join(found)


def test_the_private_key_scan_finds_a_keyfile_and_ignores_code_that_builds_keys(tmp_path) -> None:
    keyfile = tmp_path / "leaked.json"
    keyfile.write_text('{"private_key_hex": "' + "ab" * 64 + '"}')
    seedfile = tmp_path / "seed.json"
    seedfile.write_text('{"seed_hex": "' + "cd" * 32 + '"}')
    code = tmp_path / "builds_in_memory.py"
    code.write_text(
        "identity = LocalIdentity.from_seed(bytes(32))\n"
        'assert document["private_key_hex"] == identity.private_key.hex()\n'
    )
    found = private_key_material([keyfile, seedfile, code])
    assert [Path(line.split(":")[0]).name for line in found] == ["leaked.json", "seed.json"]


# --- Repetition (3.3) ---------------------------------------------------------


def test_repetition_is_bounded_and_every_repeat_is_declared(committed) -> None:
    declared = declared_repeats()
    assert all(d.reason.strip() for d in declared), "a repeat with no reason"
    assert repeat_violations(committed, declared=len(declared)) == []


def test_the_repeat_share_and_copy_count_are_within_the_declared_budget(committed) -> None:
    found = repetition(committed)
    assert found.share <= REPEAT_CEILING, f"{found.share:.1%}"
    assert found.max_copies <= MAX_COPIES
    assert found.repeats > 0, "the dedup tests need real repeats"


def test_exactly_one_late_echo_and_one_retransmission(committed) -> None:
    found = repetition(committed)
    late = [
        g
        for g in found.repeated()
        if g[0].packet.route_type is RouteType.FLOOD and g[-1].at - g[0].at > 60
    ]
    assert len(late) == 1
    assert late[0][0].packet.payload_type is PayloadType.ANON_REQ
    assert late[0][-1].packet.path != late[0][0].packet.path
    dms = [
        r
        for group in found.copies.values()
        for r in group
        if r.packet.payload_type is PayloadType.TXT_MSG
    ]
    raws: dict[bytes, list] = {}
    for reception in dms:
        raws.setdefault(reception.raw, []).append(reception)
    identical = [group for group in raws.values() if len(group) > 1]
    assert len(identical) == 1
    assert identical[0][-1].at - identical[0][0].at > 30 * 60
    assert identical[0][0].file == EXCHANGE


def _with_extra(
    committed: dict[str, str], name: str, line: int, delta_ts: str | None = None
) -> dict[str, str]:
    """A copy of the corpus with one frame line duplicated at the end of its file."""
    lines = committed[name].splitlines()
    row = json.loads(lines[line])
    if delta_ts:
        row["ts"] = delta_ts
    scratch = dict(committed)
    scratch[name] = committed[name] + json.dumps(row) + "\n"
    return scratch


def _first_frame_line(committed: dict[str, str], name: str, payload_type: PayloadType) -> int:
    for record in records({name: committed[name]}):
        if (
            record.data.get("kind") == "rx_frame"
            and decode(record.raw).payload_type is payload_type
        ):
            return record.line - 1
    raise AssertionError(f"no {payload_type.name} in {name}")


def test_a_duplicated_frame_fails_the_budget(committed) -> None:
    declared = len(declared_repeats())
    line = _first_frame_line(committed, AMBIENT, PayloadType.ADVERT)
    scratch = _with_extra(committed, AMBIENT, line, "2026-09-05T20:59:00+00:00")
    problems = repeat_violations(scratch, declared=declared)
    assert any("nothing accounts for" in p for p in problems), problems


def test_too_many_copies_of_one_packet_fail_the_budget(committed) -> None:
    declared = len(declared_repeats())
    line = _first_frame_line(committed, AMBIENT, PayloadType.ADVERT)
    scratch = committed
    for _ in range(MAX_COPIES):
        scratch = _with_extra(scratch, AMBIENT, line, "2026-09-05T20:59:00+00:00")
    problems = repeat_violations(scratch, declared=declared + MAX_COPIES)
    assert any("copies" in p and "at most" in p for p in problems), problems


def test_a_second_late_echo_fails_the_budget(committed) -> None:
    declared = len(declared_repeats())
    line = _first_frame_line(committed, CHANNELS, PayloadType.GRP_TXT)
    scratch = _with_extra(committed, CHANNELS, line, "2026-09-05T21:00:00+00:00")
    problems = repeat_violations(scratch, declared=declared + 1)
    assert any("more than 60 s late" in p for p in problems), problems


def test_a_second_retransmission_fails_the_budget(committed) -> None:
    declared = len(declared_repeats())
    line = _first_frame_line(committed, EXCHANGE, PayloadType.TXT_MSG)
    scratch = _with_extra(committed, EXCHANGE, line, "2026-09-05T23:59:00+00:00")
    problems = repeat_violations(scratch, declared=declared + 1)
    assert any("retransmitted identically" in p for p in problems), problems


def test_a_corpus_padded_past_the_ceiling_fails_the_budget(committed) -> None:
    declared = len(declared_repeats())
    scratch = committed
    added = 0
    for record in records({AMBIENT: committed[AMBIENT]}):
        if record.is_frame and decode(record.raw).payload_type is PayloadType.ADVERT:
            scratch = _with_extra(scratch, AMBIENT, record.line - 1, "2026-09-05T20:59:00+00:00")
            added += 1
        if added == 30:
            break
    problems = repeat_violations(scratch, declared=declared + added)
    assert any("ceiling" in p for p in problems), problems
