"""Payload codec tests: envelopes, adverts and the decrypted bodies.

The advert cases use appdata bytes lifted from real corpus frames (`0x92`
repeater and `0x93` room server) alongside synthetic fixtures for the flag
combinations the corpus never contains — feature 1, feature 2, and adverts with
no name.
"""

from __future__ import annotations

import pytest

from sighop.protocol.packet import PayloadType
from sighop.protocol.payloads import (
    ADVERT_FIXED_SIZE,
    MAX_ADVERT_DATA_SIZE,
    Acknowledgement,
    Advert,
    AdvertAppData,
    AnonRequestEnvelope,
    DirectEnvelope,
    DiscoverRequest,
    DiscoverResponse,
    GroupEnvelope,
    GroupTextBody,
    NodeType,
    ReturnedPathBody,
    RoomLoginBody,
    TextMessageBody,
    TextType,
    TracePayload,
    UnparsedPayload,
    WireText,
    build_ack,
    build_advert,
    build_appdata,
    build_discover_request,
    build_discover_response,
    build_group_text_body,
    build_payload,
    build_returned_path_body,
    build_room_login_body,
    build_text_message_body,
    parse_advert,
    parse_appdata,
    parse_group_text_body,
    parse_payload,
    parse_returned_path_body,
    parse_room_login_body,
    parse_text_message_body,
)
from sighop.protocol.result import DecodeFailure, EncodeError, FailureReason

BLOCK = bytes(range(16))


def ok[T](result: T | DecodeFailure) -> T:
    assert not isinstance(result, DecodeFailure), result
    return result


def bad(result: object) -> DecodeFailure:
    assert isinstance(result, DecodeFailure), result
    return result


# --- Envelopes -------------------------------------------------------------


@pytest.mark.parametrize(
    "payload_type",
    [PayloadType.REQ, PayloadType.RESPONSE, PayloadType.TXT_MSG, PayloadType.PATH],
)
def test_direct_envelope_round_trips(payload_type: PayloadType) -> None:
    raw = b"\xab\xcd" + b"\x12\x34" + BLOCK * 2
    envelope = ok(parse_payload(payload_type, raw))
    assert isinstance(envelope, DirectEnvelope)
    assert envelope.dest_hash == 0xAB
    assert envelope.src_hash == 0xCD
    assert envelope.mac == b"\x12\x34"
    assert envelope.ciphertext == BLOCK * 2
    assert build_payload(envelope) == raw


def test_direct_envelope_rejects_unaligned_ciphertext() -> None:
    failure = bad(parse_payload(PayloadType.TXT_MSG, b"\xab\xcd\x12\x34" + bytes(20)))
    assert failure.reason is FailureReason.CIPHERTEXT_NOT_BLOCK_ALIGNED
    assert "20 ciphertext bytes" in failure.detail


def test_direct_envelope_rejects_empty_ciphertext() -> None:
    failure = bad(parse_payload(PayloadType.TXT_MSG, b"\xab\xcd\x12\x34"))
    assert failure.reason is FailureReason.CIPHERTEXT_NOT_BLOCK_ALIGNED


def test_direct_envelope_rejects_truncation() -> None:
    assert bad(parse_payload(PayloadType.TXT_MSG, b"\xab\xcd")).reason is (FailureReason.TRUNCATED)


def test_anon_request_round_trips() -> None:
    pubkey = bytes(range(32))
    raw = b"\xab" + pubkey + b"\x12\x34" + BLOCK
    envelope = ok(parse_payload(PayloadType.ANON_REQ, raw))
    assert isinstance(envelope, AnonRequestEnvelope)
    assert envelope.dest_hash == 0xAB
    assert envelope.sender_public_key == pubkey
    assert envelope.sender_hash == pubkey[0]
    assert build_payload(envelope) == raw


def test_anon_request_rejects_truncation() -> None:
    assert bad(parse_payload(PayloadType.ANON_REQ, b"\xab" + bytes(20))).reason is (
        FailureReason.TRUNCATED
    )


@pytest.mark.parametrize("payload_type", [PayloadType.GRP_TXT, PayloadType.GRP_DATA])
def test_group_envelope_round_trips(payload_type: PayloadType) -> None:
    raw = b"\x7f" + b"\x12\x34" + BLOCK
    envelope = ok(parse_payload(payload_type, raw))
    assert isinstance(envelope, GroupEnvelope)
    assert envelope.channel_hash == 0x7F
    assert envelope.mac == b"\x12\x34"
    assert envelope.ciphertext == BLOCK
    assert build_payload(envelope) == raw


def test_ack_four_byte_form() -> None:
    ack = ok(parse_payload(PayloadType.ACK, b"\xde\xad\xbe\xef"))
    assert isinstance(ack, Acknowledgement)
    assert ack.checksum == b"\xde\xad\xbe\xef"
    assert ack.tail == b""
    assert build_payload(ack) == b"\xde\xad\xbe\xef"


def test_ack_six_byte_form_preserves_the_tail() -> None:
    """Current firmware appends an extended attempt byte and a random byte."""
    raw = b"\xde\xad\xbe\xef\x01\x99"
    ack = ok(parse_payload(PayloadType.ACK, raw))
    assert isinstance(ack, Acknowledgement)
    assert ack.checksum == b"\xde\xad\xbe\xef"
    assert ack.tail == b"\x01\x99"
    assert build_payload(ack) == raw


@pytest.mark.parametrize("length", [0, 3, 5, 7, 8])
def test_ack_of_any_other_length_is_rejected(length: int) -> None:
    failure = bad(parse_payload(PayloadType.ACK, bytes(length)))
    assert failure.reason is FailureReason.BAD_PAYLOAD_LENGTH


def test_build_ack_rejects_a_bad_tail() -> None:
    with pytest.raises(EncodeError, match="tail"):
        build_ack(Acknowledgement(checksum=b"\x00\x01\x02\x03", tail=b"\x01"))


def test_trace_payload_is_preserved_without_interpretation() -> None:
    trace = ok(parse_payload(PayloadType.TRACE, bytes(range(20))))
    assert isinstance(trace, TracePayload)
    assert trace.raw == bytes(range(20))
    assert build_payload(trace) == bytes(range(20))


@pytest.mark.parametrize(
    "payload_type",
    [
        PayloadType.MULTIPART,
        PayloadType.CONTROL,
        PayloadType.RAW_CUSTOM,
        PayloadType.RESERVED_0C,
        PayloadType.RESERVED_0D,
        PayloadType.RESERVED_0E,
    ],
)
def test_unsupported_payload_types_are_preserved_not_dropped(
    payload_type: PayloadType,
) -> None:
    raw = bytes(range(12))
    parsed = ok(parse_payload(payload_type, raw))
    assert isinstance(parsed, UnparsedPayload)
    assert parsed.payload_type is payload_type
    assert parsed.raw == raw
    assert build_payload(parsed) == raw


# --- CONTROL: node discovery ------------------------------------------------

# A 38-byte response lifted from the corpus (2026-09-05): a repeater answering
# the request above it, tag 9a7d3916, reporting it heard the request at +11.75 dB.
CORPUS_DISCOVER_RESP = bytes.fromhex(
    "922f9a7d3916[redacted]"
)


def test_discover_request_without_since() -> None:
    raw = bytes.fromhex("80049a7d3916")
    request = ok(parse_payload(PayloadType.CONTROL, raw))
    assert isinstance(request, DiscoverRequest)
    assert request.prefix_only is False
    assert request.selected_node_types == (NodeType.REPEATER,)
    assert request.tag == bytes.fromhex("9a7d3916")
    assert request.since is None
    assert build_payload(request) == raw


def test_discover_request_with_since() -> None:
    raw = bytes.fromhex("8006129320690a000000")
    request = ok(parse_payload(PayloadType.CONTROL, raw))
    assert isinstance(request, DiscoverRequest)
    assert request.selected_node_types == (NodeType.CHAT, NodeType.REPEATER)
    assert request.tag == bytes.fromhex("12932069")
    assert request.since == 10
    assert build_payload(request) == raw


def test_discover_request_since_zero_is_not_absent() -> None:
    """The repeater firmware itself sends `since=0` in the 10-byte form."""
    with_zero = build_discover_request(DiscoverRequest(0, 0x04, b"\x01\x02\x03\x04", since=0))
    without = build_discover_request(DiscoverRequest(0, 0x04, b"\x01\x02\x03\x04"))
    assert len(with_zero) == 10
    assert len(without) == 6


def test_discover_request_preserves_every_flag_bit() -> None:
    raw = bytes.fromhex("8f10deadbeef")
    request = ok(parse_payload(PayloadType.CONTROL, raw))
    assert isinstance(request, DiscoverRequest)
    assert request.prefix_only is True
    assert request.flags == 0x0F
    assert request.selected_node_types == (NodeType.SENSOR,)
    assert build_payload(request) == raw


def test_discover_response_with_full_key() -> None:
    response = ok(parse_payload(PayloadType.CONTROL, CORPUS_DISCOVER_RESP))
    assert isinstance(response, DiscoverResponse)
    assert response.node_type_name is NodeType.REPEATER
    assert response.snr_db == 11.75
    assert response.tag == bytes.fromhex("9a7d3916")
    assert response.claimed_key == CORPUS_DISCOVER_RESP[6:]
    assert response.key_is_prefix is False
    assert build_payload(response) == CORPUS_DISCOVER_RESP


def test_discover_response_with_key_prefix_and_negative_snr() -> None:
    raw = bytes([0x92, 0xF6]) + b"\x01\x02\x03\x04" + bytes(range(8))
    response = ok(parse_payload(PayloadType.CONTROL, raw))
    assert isinstance(response, DiscoverResponse)
    assert response.key_is_prefix is True
    assert response.claimed_key == bytes(range(8))
    assert response.snr_db == -2.5
    assert build_payload(response) == raw


def test_discover_response_with_an_unnamed_node_type_still_parses() -> None:
    raw = bytes([0x9C, 0x00]) + bytes(4) + bytes(8)
    response = ok(parse_payload(PayloadType.CONTROL, raw))
    assert isinstance(response, DiscoverResponse)
    assert response.node_type_name == 0x0C
    assert build_payload(response) == raw


@pytest.mark.parametrize(
    ("raw", "valid"),
    [
        (bytes([0x80]) + bytes(6), "6 or 10"),
        (bytes([0x80]) + bytes(4), "6 or 10"),
        (bytes([0x90]) + bytes(19), "14 or 38"),
        (bytes([0x90]) + bytes(38), "14 or 38"),
    ],
)
def test_discover_payload_of_an_unsent_length_is_rejected(raw: bytes, valid: str) -> None:
    failure = bad(parse_payload(PayloadType.CONTROL, raw))
    assert failure.reason is FailureReason.BAD_PAYLOAD_LENGTH
    assert valid in failure.detail


@pytest.mark.parametrize("raw", [b"", b"\xde\xad", bytes([0x00]) + bytes(37), bytes([0xA0, 1])])
def test_non_discovery_control_is_preserved_uninterpreted(raw: bytes) -> None:
    parsed = ok(parse_payload(PayloadType.CONTROL, raw))
    assert isinstance(parsed, UnparsedPayload)
    assert parsed.payload_type is PayloadType.CONTROL
    assert build_payload(parsed) == raw


def test_build_discover_request_rejects_a_bad_tag() -> None:
    with pytest.raises(EncodeError, match="tag"):
        build_discover_request(DiscoverRequest(0, 0x04, b"\x01\x02\x03"))


@pytest.mark.parametrize("key_size", [0, 7, 16, 33])
def test_build_discover_response_rejects_a_bad_key(key_size: int) -> None:
    with pytest.raises(EncodeError, match="claimed key"):
        build_discover_response(DiscoverResponse(2, 0, bytes(4), bytes(key_size)))


# --- Adverts ---------------------------------------------------------------

# Appdata bytes as they appear on the corpus mesh: flags, lat, lon, name.
CORPUS_REPEATER_APPDATA = (
    bytes([0x92])
    + (59_123_456).to_bytes(4, "little", signed=True)
    + (17_654_321).to_bytes(4, "little", signed=True)
    + b"[redacted]"
)
CORPUS_ROOM_APPDATA = (
    bytes([0x93])
    + (-33_865_143).to_bytes(4, "little", signed=True)
    + (151_209_900).to_bytes(4, "little", signed=True)
    + b"Room 1"
)


def make_advert_payload(appdata: bytes) -> bytes:
    return bytes(32) + (1_756_000_000).to_bytes(4, "little") + bytes(64) + appdata


def test_advert_parses_its_fixed_fields() -> None:
    advert = ok(parse_advert(make_advert_payload(CORPUS_REPEATER_APPDATA)))
    assert isinstance(advert, Advert)
    assert len(advert.public_key) == 32
    assert len(advert.signature) == 64
    assert advert.timestamp == 1_756_000_000
    assert advert.appdata == CORPUS_REPEATER_APPDATA
    assert build_advert(advert) == make_advert_payload(CORPUS_REPEATER_APPDATA)


def test_advert_signed_message_is_pubkey_timestamp_appdata() -> None:
    advert = ok(parse_advert(make_advert_payload(b"\x01")))
    assert advert.signed_message == (
        advert.public_key + (1_756_000_000).to_bytes(4, "little") + b"\x01"
    )


@pytest.mark.parametrize("length", [0, 80, ADVERT_FIXED_SIZE])
def test_advert_shorter_than_101_bytes_is_rejected(length: int) -> None:
    failure = bad(parse_advert(bytes(length)))
    assert failure.reason is FailureReason.TRUNCATED


def test_located_named_repeater_appdata() -> None:
    appdata = ok(parse_appdata(CORPUS_REPEATER_APPDATA))
    assert isinstance(appdata, AdvertAppData)
    assert appdata.node_type is NodeType.REPEATER
    assert appdata.latitude == 59_123_456
    assert appdata.latitude_degrees == pytest.approx(59.123456)
    assert appdata.longitude == 17_654_321
    assert appdata.name is not None
    assert appdata.name.text == "[redacted]"


def test_room_server_flags_are_an_enum_not_a_bit_combination() -> None:
    """`0x93` low nibble `0x03` is ROOM_SERVER, not CHAT|REPEATER."""
    appdata = ok(parse_appdata(CORPUS_ROOM_APPDATA))
    assert appdata.node_type is NodeType.ROOM_SERVER
    assert appdata.latitude == -33_865_143
    assert appdata.name is not None
    assert appdata.name.text == "Room 1"


def test_negative_coordinates_decode_as_signed() -> None:
    appdata = ok(parse_appdata(CORPUS_ROOM_APPDATA))
    assert appdata.latitude_degrees is not None
    assert appdata.latitude_degrees < 0


def test_advert_with_no_name_flag_preserves_trailing_bytes() -> None:
    raw = bytes([0x02]) + b"\xde\xad"
    appdata = ok(parse_appdata(raw))
    assert appdata.node_type is NodeType.REPEATER
    assert appdata.name is None
    assert appdata.trailing == b"\xde\xad"


def test_feature_fields_decode_in_wire_order() -> None:
    """No corpus advert sets 0x20 or 0x40, so this combination is synthetic."""
    raw = build_appdata(
        NodeType.SENSOR,
        latitude=1_000_000,
        longitude=-2_000_000,
        feature1=0x1234,
        feature2=0xABCD,
        name="s",
    )
    assert raw[0] == 0x04 | 0x10 | 0x20 | 0x40 | 0x80
    appdata = ok(parse_appdata(raw))
    assert appdata.node_type is NodeType.SENSOR
    assert (appdata.latitude, appdata.longitude) == (1_000_000, -2_000_000)
    assert (appdata.feature1, appdata.feature2) == (0x1234, 0xABCD)
    assert appdata.name is not None and appdata.name.text == "s"


def test_future_node_type_value_is_kept_as_a_number() -> None:
    appdata = ok(parse_appdata(bytes([0x0F])))
    assert appdata.node_type == 0x0F
    assert not isinstance(appdata.node_type, NodeType)


def test_name_is_not_nul_terminated_on_the_wire() -> None:
    appdata = ok(parse_appdata(bytes([0x81]) + b"no terminator"))
    assert appdata.name is not None
    assert appdata.name.text == "no terminator"


def test_name_that_is_not_valid_utf8_is_flagged_not_discarded() -> None:
    appdata = ok(parse_appdata(bytes([0x81]) + b"caf\xe9"))
    assert appdata.name is not None
    assert not appdata.name.is_valid_utf8
    assert appdata.name.raw == b"caf\xe9"
    assert "�" in appdata.name.text


def test_appdata_truncated_before_a_declared_location() -> None:
    failure = bad(parse_appdata(bytes([0x91]) + bytes(4)))
    assert failure.reason is FailureReason.TRUNCATED
    assert "location" in failure.detail


def test_appdata_truncated_before_a_declared_feature() -> None:
    failure = bad(parse_appdata(bytes([0x21]) + b"\x01"))
    assert failure.reason is FailureReason.TRUNCATED
    assert "feature 1" in failure.detail


def test_empty_appdata_is_rejected() -> None:
    assert bad(parse_appdata(b"")).reason is FailureReason.TRUNCATED


def test_build_appdata_round_trips_a_corpus_shape() -> None:
    raw = build_appdata(
        NodeType.REPEATER,
        latitude=59_123_456,
        longitude=17_654_321,
        name="[redacted]",
    )
    assert raw == CORPUS_REPEATER_APPDATA


def test_build_appdata_enforces_max_advert_data_size() -> None:
    with pytest.raises(EncodeError, match="MAX_ADVERT_DATA_SIZE"):
        build_appdata(NodeType.CHAT, name="n" * MAX_ADVERT_DATA_SIZE)


def test_build_appdata_requires_both_coordinates() -> None:
    with pytest.raises(EncodeError, match="together"):
        build_appdata(NodeType.CHAT, latitude=1)


def test_build_advert_enforces_max_advert_data_size() -> None:
    advert = Advert(
        public_key=bytes(32),
        timestamp=0,
        signature=bytes(64),
        appdata=bytes(MAX_ADVERT_DATA_SIZE + 1),
    )
    with pytest.raises(EncodeError, match="MAX_ADVERT_DATA_SIZE"):
        build_advert(advert)


# --- Text message bodies ---------------------------------------------------


def make_text_body(timestamp: int, txt_type: int, attempt: int, text: bytes) -> bytes:
    return timestamp.to_bytes(4, "little") + bytes([txt_type << 2 | attempt]) + text


def test_plain_text_message_body() -> None:
    body = ok(parse_text_message_body(make_text_body(1_700_000_000, 0, 2, b"hi")))
    assert isinstance(body, TextMessageBody)
    assert body.timestamp == 1_700_000_000
    assert body.txt_type is TextType.PLAIN
    assert body.attempt == 2
    assert body.text.text == "hi"
    assert body.sender_key_prefix is None


def test_cli_command_body() -> None:
    body = ok(parse_text_message_body(make_text_body(1, 1, 0, b"advert")))
    assert body.txt_type is TextType.CLI_DATA
    assert body.text.text == "advert"


def test_signed_plain_text_body_splits_off_the_key_prefix() -> None:
    body = ok(parse_text_message_body(make_text_body(1, 2, 0, b"\xaa\xbb\xcc\xdd" + b"yo")))
    assert body.txt_type is TextType.SIGNED_PLAIN
    assert body.sender_key_prefix == b"\xaa\xbb\xcc\xdd"
    assert body.text.text == "yo"


def test_signed_plain_text_body_truncated_before_its_prefix() -> None:
    failure = bad(parse_text_message_body(make_text_body(1, 2, 0, b"\xaa\xbb")))
    assert failure.reason is FailureReason.TRUNCATED


def test_zero_padding_is_stripped_from_the_text() -> None:
    padded = make_text_body(1, 0, 0, b"hello" + bytes(6))
    assert len(padded) % 16 == 0
    assert ok(parse_text_message_body(padded)).text.raw == b"hello"


def test_text_message_body_round_trips() -> None:
    raw = make_text_body(1_700_000_000, 0, 1, b"round trip")
    assert build_text_message_body(ok(parse_text_message_body(raw))) == raw


def test_signed_text_message_body_round_trips() -> None:
    raw = make_text_body(9, 2, 0, b"\x01\x02\x03\x04" + b"signed")
    assert build_text_message_body(ok(parse_text_message_body(raw))) == raw


def test_text_message_body_too_short() -> None:
    assert bad(parse_text_message_body(bytes(4))).reason is FailureReason.TRUNCATED


def test_build_text_message_body_rejects_an_out_of_range_attempt() -> None:
    message = TextMessageBody(
        timestamp=0, txt_type=TextType.PLAIN, attempt=4, text=WireText.from_bytes(b"x")
    )
    with pytest.raises(EncodeError, match="two bits"):
        build_text_message_body(message)


def test_build_text_message_body_rejects_a_stray_key_prefix() -> None:
    message = TextMessageBody(
        timestamp=0,
        txt_type=TextType.PLAIN,
        attempt=0,
        text=WireText.from_bytes(b"x"),
        sender_key_prefix=b"\x01\x02\x03\x04",
    )
    with pytest.raises(EncodeError, match="SIGNED_PLAIN"):
        build_text_message_body(message)


# --- Group text bodies -----------------------------------------------------


def test_group_message_with_a_sender_name_prefix() -> None:
    body = ok(parse_group_text_body(make_text_body(1, 0, 0, b"user123: I'm on my way")))
    assert isinstance(body, GroupTextBody)
    assert body.unverified_sender_name == "user123"
    assert body.body == "I'm on my way"


def test_group_message_with_no_name_separator_is_not_split() -> None:
    body = ok(parse_group_text_body(make_text_body(1, 0, 0, b"just a message")))
    assert body.unverified_sender_name is None
    assert body.body == "just a message"


def test_group_message_splits_only_on_the_first_separator() -> None:
    body = ok(parse_group_text_body(make_text_body(1, 0, 0, b"a: b: c")))
    assert body.unverified_sender_name == "a"
    assert body.body == "b: c"


def test_group_text_body_build_round_trips_a_parse() -> None:
    body = ok(
        parse_group_text_body(build_group_text_body(1_757_000_000, "dev-companion", "hello: world"))
    )
    assert body.unverified_sender_name == "dev-companion"
    assert body.body == "hello: world"
    assert body.message.txt_type is TextType.PLAIN
    assert body.message.attempt == 0
    assert body.message.timestamp == 1_757_000_000


def test_group_text_body_refuses_a_sender_name_containing_the_separator() -> None:
    with pytest.raises(EncodeError, match="': '"):
        build_group_text_body(1, "a: b", "hello")


# --- Returned path bodies --------------------------------------------------


def test_returned_path_with_no_bundled_extra() -> None:
    body = ok(parse_returned_path_body(bytes([0x03]) + b"\xaa\xbb\xcc"))
    assert isinstance(body, ReturnedPathBody)
    assert body.hop_count == 3
    assert body.hash_size == 1
    assert body.hops == (b"\xaa", b"\xbb", b"\xcc")
    assert body.extra_type is None
    assert build_returned_path_body(body) == bytes([0x03]) + b"\xaa\xbb\xcc"


def test_returned_path_uses_the_packed_hash_size_encoding() -> None:
    raw = bytes([0x43]) + bytes(range(6))
    body = ok(parse_returned_path_body(raw))
    assert (body.hop_count, body.hash_size) == (3, 2)
    assert body.hops == (b"\x00\x01", b"\x02\x03", b"\x04\x05")
    assert build_returned_path_body(body) == raw


def test_returned_path_bundling_an_acknowledgement() -> None:
    raw = bytes([0x02]) + b"\xaa\xbb" + bytes([PayloadType.ACK]) + b"\x01\x02\x03\x04"
    body = ok(parse_returned_path_body(raw))
    assert body.extra_type is PayloadType.ACK
    assert body.extra_ack is not None
    assert body.extra_ack.checksum == b"\x01\x02\x03\x04"
    assert build_returned_path_body(body) == raw


def test_returned_path_preserves_a_padded_extra_verbatim() -> None:
    """`Mesh.cpp:172`: the extra "may be padded with zeroes"; only the first
    four bytes of a bundled ACK are meaningful.
    """
    extra = b"\x01\x02\x03\x04\x07\x00" + bytes(4)
    raw = bytes([0x01]) + b"\xaa" + bytes([PayloadType.ACK]) + extra
    body = ok(parse_returned_path_body(raw))
    assert body.extra_ack is not None
    assert body.extra_ack.checksum == b"\x01\x02\x03\x04"
    assert body.extra_raw == extra
    assert build_returned_path_body(body) == raw


def test_returned_path_rejects_the_reserved_hash_size() -> None:
    assert bad(parse_returned_path_body(bytes([0xC1]) + bytes(4))).reason is (
        FailureReason.RESERVED_HASH_SIZE
    )


def test_returned_path_rejects_truncation() -> None:
    failure = bad(parse_returned_path_body(bytes([0x05]) + b"\xaa"))
    assert failure.reason is FailureReason.TRUNCATED


def test_returned_path_rejects_an_over_limit_path() -> None:
    assert bad(parse_returned_path_body(bytes([0x80 | 23]) + bytes(69))).reason is (
        FailureReason.PATH_SIZE_LIMIT
    )


def test_empty_returned_path_body_is_rejected() -> None:
    assert bad(parse_returned_path_body(b"")).reason is FailureReason.TRUNCATED


# --- Room login bodies -----------------------------------------------------


def test_room_login_body() -> None:
    raw = (1).to_bytes(4, "little") + (2).to_bytes(4, "little") + b"hunter2" + bytes(1)
    body = ok(parse_room_login_body(raw))
    assert isinstance(body, RoomLoginBody)
    assert (body.timestamp, body.sync_timestamp) == (1, 2)
    assert body.password.text == "hunter2"


def test_room_login_body_with_an_empty_password() -> None:
    raw = (1).to_bytes(4, "little") + (2).to_bytes(4, "little") + bytes(8)
    body = ok(parse_room_login_body(raw))
    assert body.password.raw == b""


def test_room_login_body_round_trips_without_padding() -> None:
    raw = (7).to_bytes(4, "little") + (8).to_bytes(4, "little") + b"pw"
    assert build_room_login_body(ok(parse_room_login_body(raw))) == raw


def test_room_login_body_too_short() -> None:
    assert bad(parse_room_login_body(bytes(7))).reason is FailureReason.TRUNCATED
