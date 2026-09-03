"""MeshCore protocol layer: pure functions over bytes.

Per DESIGN.md §11 this layer depends on nothing else in sighop — no radio, no
database, no network, no event loop — which `tests/protocol/test_import_boundary.py`
enforces against the source.

The decode path runs in three stages with different trust levels (design D2):

    packet.decode(raw)                  -> Packet | DecodeFailure
    payloads.parse_payload(type, bytes) -> envelope | DecodeFailure
    crypto.mac_then_decrypt(...)        -> body bytes, only with the right key

Decoding returns failures rather than raising (design D3); encoding raises
`EncodeError`, because its inputs are ours.
"""

from sighop.protocol.crypto import (
    AdvertVerificationFailure,
    ChannelKey,
    MacCandidateMatch,
    SharedSecretCache,
    VerifiedAdvert,
    ack_checksum,
    ack_checksum_for,
    calc_shared_secret,
    channel_key_from_hashtag,
    compute_mac,
    decrypt,
    encrypt,
    encrypt_then_mac,
    mac_then_decrypt,
    shared_secret_from_scalar,
    sign_advert,
    verify_advert,
    verify_mac,
)
from sighop.protocol.identity import (
    Identity,
    IdentityGenerationError,
    LocalIdentity,
    generate_identity,
)
from sighop.protocol.packet import (
    MAX_PACKET_PAYLOAD,
    MAX_PATH_SIZE,
    MAX_TRANS_UNIT,
    Packet,
    PacketHeader,
    PayloadType,
    RouteType,
)
from sighop.protocol.packet import decode as decode_packet
from sighop.protocol.packet import encode as encode_packet
from sighop.protocol.payloads import (
    Acknowledgement,
    Advert,
    AdvertAppData,
    AnonRequestEnvelope,
    DirectEnvelope,
    GroupEnvelope,
    GroupTextBody,
    NodeType,
    ParsedPayload,
    ReturnedPathBody,
    RoomLoginBody,
    TextMessageBody,
    TextType,
    TracePayload,
    UnparsedPayload,
    WireText,
    build_payload,
    parse_appdata,
    parse_group_text_body,
    parse_payload,
    parse_returned_path_body,
    parse_room_login_body,
    parse_text_message_body,
)
from sighop.protocol.result import (
    DecodeFailure,
    DecodeResult,
    EncodeError,
    FailureReason,
)

__all__ = [
    "MAX_PACKET_PAYLOAD",
    "MAX_PATH_SIZE",
    "MAX_TRANS_UNIT",
    "Acknowledgement",
    "Advert",
    "AdvertAppData",
    "AdvertVerificationFailure",
    "AnonRequestEnvelope",
    "ChannelKey",
    "DecodeFailure",
    "DecodeResult",
    "DirectEnvelope",
    "EncodeError",
    "FailureReason",
    "GroupEnvelope",
    "GroupTextBody",
    "Identity",
    "IdentityGenerationError",
    "LocalIdentity",
    "MacCandidateMatch",
    "NodeType",
    "Packet",
    "PacketHeader",
    "ParsedPayload",
    "PayloadType",
    "ReturnedPathBody",
    "RoomLoginBody",
    "RouteType",
    "SharedSecretCache",
    "TextMessageBody",
    "TextType",
    "TracePayload",
    "UnparsedPayload",
    "VerifiedAdvert",
    "WireText",
    "ack_checksum",
    "ack_checksum_for",
    "build_payload",
    "calc_shared_secret",
    "channel_key_from_hashtag",
    "compute_mac",
    "decode_packet",
    "decrypt",
    "encode_packet",
    "encrypt",
    "encrypt_then_mac",
    "generate_identity",
    "mac_then_decrypt",
    "parse_appdata",
    "parse_group_text_body",
    "parse_payload",
    "parse_returned_path_body",
    "parse_room_login_body",
    "parse_text_message_body",
    "shared_secret_from_scalar",
    "sign_advert",
    "verify_advert",
    "verify_mac",
]
