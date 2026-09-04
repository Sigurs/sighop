"""The modem transmit path (milestone 3, `modem-tx`, design D8).

Nothing here reaches a radio: the transport is the same queue-driven fake the
request tests use, so a `TxDone` can be placed at an exact point in the frame
stream. What it establishes is that the one-in-flight invariant belongs to the
modem rather than to the scheduler, and that awaiting a transmission never
stalls reception.

Milestone 4 is what confirms any of this against a board. This file is what
makes milestone 4 a flag flip rather than a debugging session on the air.
"""

from __future__ import annotations

import asyncio

import pytest

from sighop.radio.kiss import KissFrame, Reconnected
from sighop.radio.modem import (
    ERROR_TX_BUSY,
    MAX_PACKET_BYTES,
    SUB_ERROR,
    SUB_GET_VERSION,
    SUB_RESP_VERSION,
    SUB_TX_DONE,
    TYPE_DATA,
    TYPE_SET_HARDWARE,
    Modem,
    PacketTooLarge,
    RequestOk,
    RxEvent,
    TransmitBusy,
    TransmitDone,
    TransmitFailed,
    TransmitTimedOut,
    UnparsedEvent,
)
from tests.test_modem_request import (
    FAST_TIMEOUT,
    QueueTransport,
    data_frame,
    running,
    rx_meta_frame,
    settle,
    sub_frame,
)

PACKET = bytes.fromhex("0401" + "ab" * 30)


def tx_done_frame(success: bool = True) -> KissFrame:
    return sub_frame(SUB_TX_DONE, bytes((0x01 if success else 0x00,)))


def data_sends(transport: QueueTransport) -> list[bytes]:
    return [data for type_byte, data in transport.sent if type_byte == TYPE_DATA]


# --- Resolution ------------------------------------------------------------


async def test_a_transmission_resolves_when_the_modem_reports_success() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        pending = asyncio.create_task(modem.send_packet(PACKET, timeout=1))
        await settle()
        transport.push(tx_done_frame(success=True))
        result = await pending

    assert result == TransmitDone(success=True)
    assert data_sends(transport) == [PACKET]


async def test_a_failed_txdone_is_distinct_from_a_timeout() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        pending = asyncio.create_task(modem.send_packet(PACKET, timeout=1))
        await settle()
        transport.push(tx_done_frame(success=False))
        result = await pending

    assert result == TransmitDone(success=False)
    assert not isinstance(result, TransmitTimedOut)


async def test_a_busy_error_resolves_as_busy_not_as_a_failure() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        pending = asyncio.create_task(modem.send_packet(PACKET, timeout=1))
        await settle()
        transport.push(sub_frame(SUB_ERROR, bytes((ERROR_TX_BUSY,))))
        result = await pending

    assert result == TransmitBusy()


async def test_a_silent_modem_times_out_and_releases_the_invariant() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        first = await modem.send_packet(PACKET, timeout=0.05)
        assert first == TransmitTimedOut()

        # The next submission is accepted rather than blocked behind the last.
        second = asyncio.create_task(modem.send_packet(PACKET, timeout=1))
        await settle()
        transport.push(tx_done_frame())
        assert await second == TransmitDone(success=True)

    assert len(data_sends(transport)) == 2


# --- Size ------------------------------------------------------------------


async def test_an_oversized_packet_never_reaches_the_transport() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        with pytest.raises(PacketTooLarge, match="255"):
            await modem.send_packet(b"\x00" * (MAX_PACKET_BYTES + 1))

    assert data_sends(transport) == []


async def test_a_maximum_size_packet_is_accepted() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    packet = b"\x04" * MAX_PACKET_BYTES
    async with running(modem):
        pending = asyncio.create_task(modem.send_packet(packet, timeout=1))
        await settle()
        transport.push(tx_done_frame())
        await pending

    assert data_sends(transport) == [packet]


# --- One in flight ---------------------------------------------------------


async def test_concurrent_submissions_serialize_at_the_modem() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        first = asyncio.create_task(modem.send_packet(PACKET, timeout=1))
        second = asyncio.create_task(modem.send_packet(PACKET + b"\x01", timeout=1))
        await settle()

        # Only the first packet is on the wire while the first is unresolved.
        assert len(data_sends(transport)) == 1

        transport.push(tx_done_frame())
        await first
        await settle()
        assert len(data_sends(transport)) == 2

        transport.push(tx_done_frame())
        await second

    assert data_sends(transport) == [PACKET, PACKET + b"\x01"]


async def test_an_unsolicited_txdone_is_reported_as_an_unparsed_frame() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem) as events:
        transport.push(tx_done_frame())
        await settle()

    unparsed = [e for e in events if isinstance(e, UnparsedEvent)]
    assert len(unparsed) == 1
    assert unparsed[0].raw == bytes((TYPE_SET_HARDWARE, SUB_TX_DONE, 0x01))


# --- Reception continues ---------------------------------------------------


async def test_a_frame_received_mid_transmission_keeps_its_rx_meta() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem) as events:
        pending = asyncio.create_task(modem.send_packet(PACKET, timeout=1))
        await settle()

        transport.push(data_frame(b"\x04\x11\x22"), rx_meta_frame(8, -95))
        await settle()

        transport.push(tx_done_frame())
        assert await pending == TransmitDone(success=True)

    received = [e for e in events if isinstance(e, RxEvent)]
    assert len(received) == 1
    assert received[0].packet == b"\x04\x11\x22"
    assert received[0].rx_meta is not None
    assert received[0].rx_meta.snr_db == 2.0
    assert received[0].rx_meta.rssi_dbm == -95


async def test_a_txdone_between_a_data_frame_and_its_rx_meta_preserves_correlation() -> None:
    """The design D3 hazard, in its transmit form."""
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem) as events:
        pending = asyncio.create_task(modem.send_packet(PACKET, timeout=1))
        await settle()

        transport.push(data_frame(b"\x04\x33"))
        await settle()
        transport.push(tx_done_frame())
        await pending
        transport.push(rx_meta_frame(-4, -110))
        await settle()

    received = [e for e in events if isinstance(e, RxEvent)]
    assert len(received) == 1
    assert received[0].rx_meta is not None
    assert received[0].rx_meta.snr_db == -1.0


async def test_a_probe_response_still_resolves_while_a_transmission_is_outstanding() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        sending = asyncio.create_task(modem.send_packet(PACKET, timeout=1))
        await settle()

        asking = asyncio.create_task(modem.request(SUB_GET_VERSION, timeout=1))
        await settle()
        transport.push(sub_frame(SUB_RESP_VERSION, b"\x01\x00"))
        answer = await asking

        transport.push(tx_done_frame())
        assert await sending == TransmitDone(success=True)

    assert isinstance(answer, RequestOk)


# --- Link loss -------------------------------------------------------------


async def test_a_reconnect_resolves_the_outstanding_transmission_as_failed() -> None:
    transport = QueueTransport()
    modem = Modem(transport, request_timeout=FAST_TIMEOUT)
    async with running(modem):
        pending = asyncio.create_task(modem.send_packet(PACKET, timeout=5))
        await settle()

        transport.push(Reconnected(attempts=1))
        result = await asyncio.wait_for(pending, 1)

    assert isinstance(result, TransmitFailed)
    assert "reconnect" in result.reason
