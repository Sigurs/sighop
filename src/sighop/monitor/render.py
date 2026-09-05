"""Rendering decoded receptions as fixed-width lines. Pure functions only.

The one rule that is not cosmetic: **unverified content is never rendered in
the shape reserved for verified content**. A signature-verified advert shows a
mark and its name; an advert whose signature failed shows the failure and its
public key, and no name at all — not the name with a caveat beside it.
DESIGN.md §5 makes discarding badly-signed adverts the rule and §8 makes
"never present unverified data as verified" a hard one; a name rendered next to
a warning is how that rule dies three milestones later when someone reuses the
formatter (design D8).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sighop.net.adverts import EntityStub
from sighop.net.dedup import DedupStats
from sighop.net.dm import (
    AckMatched,
    AckUnmatched,
    DirectMessageEvent,
    MessageReceived,
    MessageSent,
    MessageUndecryptable,
    MessageUnparsable,
    SendResolved,
)
from sighop.net.rx import (
    AdvertOutcome,
    ModemUnparsed,
    Payload,
    PayloadFailure,
    RxRecord,
    StructuralFailure,
    Uninterpreted,
)
from sighop.net.tx import SchedulerStatus
from sighop.protocol.crypto import AdvertVerification, VerifiedAdvert
from sighop.protocol.packet import RouteType
from sighop.protocol.payloads import (
    Acknowledgement,
    AnonRequestEnvelope,
    DirectEnvelope,
    GroupEnvelope,
    NodeType,
    ParsedPayload,
    TracePayload,
)
from sighop.radio.modem import DEVICE_REBOOT_REASON, RadioParams
from sighop.radio.probe import Absent, FirmwareVersion, Probed, ProbeResult

VERIFIED_MARK = "✓"
UNVERIFIED_MARK = "✗"
FAILURE_MARK = "!"

_ROUTE_LABELS = {
    RouteType.FLOOD: "FLOOD",
    RouteType.DIRECT: "DIRECT",
    RouteType.TRANSPORT_FLOOD: "T-FLOOD",
    RouteType.TRANSPORT_DIRECT: "T-DIRECT",
}

_PATH_WIDTH = 24
_INDENT = " " * 10


def render_frame_line(record: RxRecord) -> str:
    """The per-reception line: when, what, how it got here, how well."""
    time = record.received_at.strftime("%H:%M:%S")
    payload = record.payload_type.name if record.payload_type is not None else "-"
    route = _ROUTE_LABELS.get(record.route_type, "-") if record.route_type else "-"
    hops = f"h{record.hop_count}" if record.hop_count is not None else "h-"
    return (
        f"{time}  {payload:<11} {route:<8} {hops:<3} "
        f"{_render_path(record):<{_PATH_WIDTH}} "
        f"snr={_render_snr(record.snr_db)} rssi={_render_rssi(record.rssi_dbm)} "
        f"{record.size_bytes:>3}B"
    )


def render_detail_line(record: RxRecord) -> str:
    """The second line: what the payload actually was."""
    match record.outcome:
        case AdvertOutcome(verification=verification):
            return _INDENT + _render_advert(verification)
        case Payload(payload=payload):
            return _INDENT + _render_payload(payload)
        case Uninterpreted(payload_type=payload_type, raw=raw):
            return (
                f"{_INDENT}{payload_type.name} not interpreted, "
                f"{len(raw)}B: {_truncate_hex(raw)}"
            )
        case StructuralFailure(failure=failure) | PayloadFailure(failure=failure):
            return (
                f"{_INDENT}{FAILURE_MARK} decode failed: {failure.reason} at offset "
                f"{failure.offset}"
                + (f" — {failure.detail}" if failure.detail else "")
                + f"  raw={_truncate_hex(failure.raw)}"
            )
        case ModemUnparsed(reason=reason) if reason.startswith(DEVICE_REBOOT_REASON):
            # Worth its own shape: this is the board restarting, which costs it
            # the radio configuration and is why reception stops dead.
            return (
                f"{_INDENT}*** MODEM REBOOTED ({reason.removeprefix(DEVICE_REBOOT_REASON + ': ')})"
                " — radio configuration lost, re-applying SetRadio ***"
            )
        case ModemUnparsed(reason=reason):
            return (
                f"{_INDENT}{FAILURE_MARK} unparsed frame: {reason}  "
                f"raw={_truncate_hex(record.raw)}"
            )
    raise AssertionError(f"unhandled outcome {record.outcome!r}")  # pragma: no cover


def render_record(record: RxRecord) -> str:
    return f"{render_frame_line(record)}\n{render_detail_line(record)}"


# --- Payload details -------------------------------------------------------


def _render_advert(verification: AdvertVerification) -> str:
    if isinstance(verification, VerifiedAdvert):
        appdata = verification.appdata
        node_type = (
            appdata.node_type.name
            if isinstance(appdata.node_type, NodeType)
            else f"type_{appdata.node_type}"
        )
        name = appdata.name.text if appdata.name is not None else "(unnamed)"
        line = (
            f"{VERIFIED_MARK} advert  {name!r}  {node_type}  flags=0x{appdata.flags:02x}  "
            f"key={verification.public_key.hex()[:16]}"
        )
        if appdata.latitude_degrees is not None:
            line += f"  at {appdata.latitude_degrees:.5f},{appdata.longitude_degrees:.5f}"
        return line
    # No name, no node type, no location: an unverified advert's content is
    # attacker-chosen, and this is the one place that could leak it.
    return (
        f"{UNVERIFIED_MARK} advert  UNVERIFIED ({verification.reason})  "
        f"node_hash=0x{verification.node_hash:02x}  content withheld"
    )


def _render_payload(payload: ParsedPayload) -> str:
    match payload:
        case DirectEnvelope(dest_hash=dest, src_hash=src, mac=mac, ciphertext=ciphertext):
            return (
                f"encrypted  dest=0x{dest:02x} src=0x{src:02x} mac={mac.hex()} "
                f"ciphertext={len(ciphertext)}B  not decrypted (no key held)"
            )
        case AnonRequestEnvelope(dest_hash=dest, mac=mac, ciphertext=ciphertext):
            return (
                f"encrypted  dest=0x{dest:02x} sender_key={payload.sender_public_key.hex()[:16]} "
                f"mac={mac.hex()} ciphertext={len(ciphertext)}B  not decrypted (no key held)"
            )
        case GroupEnvelope(channel_hash=channel, mac=mac, ciphertext=ciphertext):
            return (
                f"encrypted  channel=0x{channel:02x} mac={mac.hex()} "
                f"ciphertext={len(ciphertext)}B  not decrypted (no key held)"
            )
        case Acknowledgement(checksum=checksum, tail=tail):
            suffix = f" tail={tail.hex()}" if tail else ""
            return f"ack  checksum={checksum.hex()}{suffix}"
        case TracePayload(raw=raw):
            return f"trace  {len(raw)}B: {_truncate_hex(raw)}"
    raise AssertionError(f"unhandled payload {payload!r}")  # pragma: no cover


# --- Startup and summary ---------------------------------------------------


def render_startup(probe_result: ProbeResult | None) -> str:
    """What the board says it is. Every unanswered field says so."""
    if probe_result is None:
        return "modem: probe result unavailable — the link never reached the ready state"

    lines = [
        "modem: "
        + f"{_probed(probe_result.device_name)}  "
        + f"firmware={_render_firmware(probe_result.firmware_version)}  "
        + f"tx_power={_probed(probe_result.tx_power_dbm, lambda v: f'{v} dBm')}",
        f"radio: {_render_radio(probe_result)}",
    ]
    if probe_result.radio_matches_configured is False:
        lines.append(
            "  *** RADIO READBACK MISMATCH: configured "
            f"{_radio_text(probe_result.configured_radio)}, board reports "
            f"{_radio_text(probe_result.observed_radio)} ***"
        )
    lines.append(
        "telemetry: "
        + f"battery={_probed(probe_result.battery_mv, lambda v: f'{v} mV')}  "
        + f"mcu_temp={_probed(probe_result.mcu_temp_tenths_c, lambda v: f'{v / 10:.1f} °C')}  "
        + f"sensors={_probed(probe_result.sensors_raw, lambda v: f'{len(v)}B CayenneLPP')}"
    )
    return "\n".join(lines)


def render_replay_startup(provenance: dict | None, source: str) -> str:
    """The startup line for a replay: the file's own provenance, or its absence."""
    if provenance is None:
        return (
            f"replaying {source}\n"
            "modem: no capture_meta header in this file — provenance absent "
            "(see its .meta.json sidecar, if any)"
        )
    device = _provenance_field(provenance, "device_name")
    radio = provenance.get("radio", {}).get("value")
    firmware = _provenance_field(provenance, "firmware_version")
    sighop = provenance.get("sighop", {})
    return (
        f"replaying {source}\n"
        f"modem: {device}  firmware={firmware}  "
        f"radio={_radio_text_from_json(radio)}\n"
        f"recorded by sighop {sighop.get('version', '?')} "
        f"({sighop.get('commit_hash', '?')})"
    )


@dataclass(frozen=True, slots=True)
class Summary:
    """The counts a monitor session reports. Raw receptions, not deduplicated
    ones: milestone 3's dedup needs an unfiltered baseline (design D9).
    """

    frames: int = 0
    decode_failures: int = 0
    adverts_verified: int = 0
    adverts_failed: int = 0
    node_hashes: int = 0
    reconnects: int = 0
    reboots: int = 0


def render_summary(summary: Summary) -> str:
    return (
        f"-- frames={summary.frames} failed={summary.decode_failures} "
        f"adverts={summary.adverts_verified} advert_failures={summary.adverts_failed} "
        f"nodes_heard={summary.node_hashes} reconnects={summary.reconnects} "
        f"reboots={summary.reboots}"
    )


# --- The runtime's status line (milestone 3) -------------------------------


def render_gate(transmit_enabled: bool) -> str:
    """The one field an operator must never be uncertain about."""
    return "TX=ENABLED" if transmit_enabled else "TX=disabled"


def render_run_startup(
    source_text: str,
    *,
    transmit_enabled: bool,
    ceiling_pct: float,
    above_regulatory_default: bool,
    entities: Sequence[EntityStub] = (),
) -> str:
    """The banner. With the gate open it says what that now means.

    Milestone 3's `--enable-transmit` reached a hand-off that dropped the
    packet; milestone 4's keys a transmitter. The banner has to say the second
    thing, name every identity that will originate traffic — a public key on the
    air is what another node stores — and state the ceiling in force.
    """
    lines = [source_text]
    if transmit_enabled:
        lines.append(
            f"** TRANSMIT ENABLED ** packets WILL be transmitted on air. "
            f"duty-cycle ceiling {ceiling_pct:.1f}% ({ceiling_pct * 36:.0f} s/hour)"
        )
        if entities:
            lines.append("originating identities:")
            lines.extend(
                f"  {entity.name}  key={entity.identity.public_key.hex()}  "
                f"node_hash=0x{entity.node_hash:02x}  "
                f"{'persistent' if entity.persistent else 'ephemeral'}"
                for entity in entities
            )
        else:
            lines.append(
                "originating identities: none — nothing will originate traffic, "
                "though replies still can"
            )
    else:
        lines.append(
            f"transmit disabled — nothing will be sent; "
            f"duty-cycle ceiling {ceiling_pct:.1f}% would apply"
        )
    if above_regulatory_default:
        lines.append(
            "!! duty-cycle ceiling is configured above the EU 868 10% limit; "
            "sighop will permit more airtime than the sub-band allows"
        )
    return "\n".join(lines)


def render_status(
    status: SchedulerStatus,
    *,
    dedup: DedupStats,
    learned_paths: int,
    active_overrides: int = 0,
    contacts: int = 0,
) -> str:
    """The periodic status line. Duty cycle first — it is the limit that binds."""
    queues = "/".join(str(status.queue_depths.get(index, 0)) for index in range(4))
    budget = (
        f"duty={status.duty_cycle_pct:5.1f}% "
        f"({status.duty_cycle_used_ms / 1000:.0f}/{status.duty_cycle_ceiling_ms / 1000:.0f}s)"
    )
    if status.reserve_reached:
        budget += " RESERVE(classes 2-3 stalled)"
    if status.above_regulatory_default:
        budget += " ABOVE-10%"
    line = (
        f"== {render_gate(status.transmit_enabled)} {budget} "
        f"q={queues} tx={status.stats.transmitted} sup={status.stats.suppressed} "
        f"drop={status.stats.dropped} fail={status.stats.failed} "
        f"dup={dedup.hit_rate * 100:.1f}% "
        f"cache={dedup.entries}/{dedup.max_entries} paths={learned_paths} "
        f"contacts={contacts}"
    )
    if active_overrides:
        line += f" overrides={active_overrides}"
    return line


def render_stubs(stubs: Sequence[EntityStub]) -> str:
    """The entity listing, marking which keys outlive the process.

    An ephemeral key is never read as an identity another node can keep; a
    persistent one is exactly that, and the two must not look alike.
    """
    if not stubs:
        return "stubs: none"
    rendered = ", ".join(
        f"{stub.name}[{stub.node_hash:02x}]"
        f"({'persistent' if stub.persistent else 'ephemeral'}, "
        f"advert {stub.flood_interval_seconds / 3600:.0f}h)"
        for stub in stubs
    )
    return f"stubs: {rendered}"


def render_contacts() -> str:
    """Contacts are in memory only for this milestone, and the run says so.

    Deliberately carries no count. The banner is printed once the board's probe
    has answered, which is seconds after the radio started delivering frames, so
    a count here is not "what we started with" — it is whatever had arrived by
    then, and reads as state that survived the restart. The live count belongs
    in the status line, where a moving number is what a reader expects.
    """
    return "contacts: in memory only — they do not survive the process"


# --- Direct messages (milestone 4) -----------------------------------------
#
# Pure formatting, and one rule carried over from adverts: a decrypted direct
# message's sender is a *claimed* contact. A 2-byte MAC match selects a key; it
# is not a signature, and nothing here may render it in the shape reserved for
# verified content (design D7).

CLAIMED_MARK = UNVERIFIED_MARK


def render_message_sent(event: MessageSent) -> str:
    state = "sent" if event.transmitted else "not sent (gate closed)"
    return (
        f"{_INDENT}-> dm {state}  to {event.contact.display_name!r}  "
        f"attempt {event.attempt}  {event.route.label}  {event.size_bytes}B  "
        f"air={event.airtime_ms:.0f}ms  ack_by={event.ack_timeout_ms:.0f}ms  "
        f"expect={event.expected_ack.hex()}  id={event.packet_id}"
    )


def render_ack_matched(event: AckMatched) -> str:
    return (
        f"{_INDENT}<- ack {event.checksum.hex()} ({event.payload_bytes}B) matched "
        f"attempt {event.attempt} from {event.contact.display_name!r} after "
        f"{event.latency_ms:.0f}ms  id={event.packet_id}"
    )


def render_ack_unmatched(event: AckUnmatched) -> str:
    return (
        f"{_INDENT}<- ack {event.checksum.hex()} matched none of "
        f"{event.outstanding} outstanding sends  id={event.packet_id}"
    )


def render_send_resolved(event: SendResolved) -> str:
    outcome = event.outcome
    if outcome.acknowledged:
        latency = "" if outcome.ack_latency_ms is None else f" in {outcome.ack_latency_ms:.0f}ms"
        return (
            f"{_INDENT}dm delivered to {event.contact.display_name!r} after "
            f"{outcome.attempts} attempt(s){latency}  id={outcome.message_id}"
        )
    return (
        f"{_INDENT}{FAILURE_MARK} dm {outcome.result} to "
        f"{event.contact.display_name!r} after {outcome.attempts} attempt(s)"
        + (f": {outcome.reason}" if outcome.reason else "")
        + f"  id={outcome.message_id}"
    )


def render_message_received(event: MessageReceived) -> str:
    """A decrypted message. The sender is claimed, and the line says so."""
    acked = "acked" if event.acknowledged else "NOT acked"
    return (
        f"{_INDENT}{CLAIMED_MARK} dm from claimed {event.contact.display_name!r} "
        f"(key={event.contact.public_key.hex()[:16]}) to {event.entity_name}: "
        f"{event.body.text.text!r}  ts={event.body.timestamp} "
        f"attempt={event.body.attempt} tried={event.candidates_tried} {acked}  "
        f"id={event.packet_id}"
    )


def render_message_undecryptable(event: MessageUndecryptable) -> str:
    return (
        f"{_INDENT}encrypted dm  dest=0x{event.dest_hash:02x} "
        f"src=0x{event.src_hash:02x}  not decrypted after "
        f"{event.candidates_tried} candidate key(s)  id={event.packet_id}"
    )


def render_message_unparsable(event: MessageUnparsable) -> str:
    return (
        f"{_INDENT}{FAILURE_MARK} dm decrypted for {event.entity_name} but did not "
        f"parse ({event.reason}); not acknowledged  id={event.packet_id}"
    )


def render_dm_event(event: DirectMessageEvent) -> str:
    match event:
        case MessageSent():
            return render_message_sent(event)
        case AckMatched():
            return render_ack_matched(event)
        case AckUnmatched():
            return render_ack_unmatched(event)
        case SendResolved():
            return render_send_resolved(event)
        case MessageReceived():
            return render_message_received(event)
        case MessageUndecryptable():
            return render_message_undecryptable(event)
        case MessageUnparsable():
            return render_message_unparsable(event)
    raise AssertionError(f"unhandled direct message event {event!r}")  # pragma: no cover


# --- Helpers ---------------------------------------------------------------


def _render_path(record: RxRecord) -> str:
    if record.packet is None or not record.packet.path:
        return "path=-"
    text = "path=" + ",".join(hop.hex() for hop in record.packet.hops)
    if len(text) > _PATH_WIDTH:
        return text[: _PATH_WIDTH - 1] + "…"
    return text


def _render_snr(snr_db: float | None) -> str:
    return "   --" if snr_db is None else f"{snr_db:+5.2f}"


def _render_rssi(rssi_dbm: int | None) -> str:
    return "  --" if rssi_dbm is None else f"{rssi_dbm:>4}"


def _truncate_hex(raw: bytes, limit: int = 32) -> str:
    text = raw.hex()
    return text if len(text) <= limit else text[:limit] + "…"


def _probed[T](value: Probed[T], render: Callable[[T], str] = str) -> str:
    """A probed value, or an explicit `unknown` naming why it is unknown."""
    if isinstance(value, Absent):
        return f"unknown({value.reason})"
    return render(value)


def _render_firmware(value: Probed[FirmwareVersion]) -> str:
    return _probed(value, lambda v: f"v{v.version}")


def _radio_text(radio: RadioParams | None) -> str:
    if radio is None:
        return "unknown"
    return (
        f"{radio.freq_hz / 1_000_000:.3f} MHz  BW {radio.bw_hz / 1000:g} kHz  "
        f"SF{radio.sf}  CR{radio.cr}"
    )


def _radio_text_from_json(radio: dict | None) -> str:
    if not radio:
        return "unknown"
    return (
        f"{radio['freq_hz'] / 1_000_000:.3f} MHz  BW {radio['bw_hz'] / 1000:g} kHz  "
        f"SF{radio['sf']}  CR{radio['cr']}"
    )


def _render_radio(probe_result: ProbeResult) -> str:
    if probe_result.observed_radio is None:
        assert isinstance(probe_result.radio, Absent)
        return (
            f"unknown({probe_result.radio.reason}) — configured "
            f"{_radio_text(probe_result.configured_radio)}, not confirmed by the board"
        )
    return f"{_radio_text(probe_result.observed_radio)} (confirmed by the board)"


def _provenance_field(provenance: dict, key: str) -> str:
    field = provenance.get(key) or {}
    value = field.get("value")
    if value is None:
        return f"unknown({field.get('reason', 'absent')})"
    if key == "firmware_version" and isinstance(value, dict):
        return f"v{value['version']}"
    return str(value)
