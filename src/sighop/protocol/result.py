"""The shared decode-result type for the protocol layer.

Per design D3: decoding *returns* failures rather than raising them. The RX
pipeline (milestone 3) must log every malformed frame as a wide event and carry
on — DESIGN.md §4.1 is explicit that a silently dropped frame is invisible
forever — and the corpus harness needs to report *which* frame failed, not just
that something did.

Encoding is the other direction: its inputs are ours, so an over-limit outbound
packet is a bug in our code and raises `EncodeError`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FailureReason(StrEnum):
    """Why a decode failed, as a stable value tests and logs can match on.

    Named per the limit or field violated so a failure says which rule the
    bytes broke, not merely that they broke one.
    """

    TRUNCATED = "truncated"
    UNSUPPORTED_PAYLOAD_VERSION = "unsupported_payload_version"
    RESERVED_HASH_SIZE = "reserved_hash_size"
    PATH_SIZE_LIMIT = "path_size_limit"
    PAYLOAD_SIZE_LIMIT = "payload_size_limit"
    TOTAL_SIZE_LIMIT = "total_size_limit"
    CIPHERTEXT_NOT_BLOCK_ALIGNED = "ciphertext_not_block_aligned"
    BAD_PAYLOAD_LENGTH = "bad_payload_length"
    PAYLOAD_TYPE_MISMATCH = "payload_type_mismatch"


@dataclass(frozen=True, slots=True)
class DecodeFailure:
    """A structured decode failure: which rule broke, where, and over what bytes.

    `offset` is the byte position within `raw` at which the problem was
    detected — for a truncation, the first byte that was needed and absent.
    """

    reason: FailureReason
    offset: int
    raw: bytes
    detail: str = ""

    def __str__(self) -> str:
        suffix = f": {self.detail}" if self.detail else ""
        return f"{self.reason} at offset {self.offset} of {len(self.raw)} bytes{suffix}"


type DecodeResult[T] = T | DecodeFailure
"""Either a decoded value or a `DecodeFailure`; discriminate with `isinstance`."""


class EncodeError(ValueError):
    """An outbound structure violates a wire limit — i.e. a bug in our code."""
