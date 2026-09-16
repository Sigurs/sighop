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

from sighop.bots.base import (
    BotActed,
    BotDispatchDropped,
    BotFailed,
    BotMode,
    BotRuntimeEvent,
    BotSendResult,
    BotSuppressed,
    BotWouldAct,
)
from sighop.net.adverts import EntityStub
from sighop.net.channels import (
    ChannelConfigReadFailed,
    ChannelEvent,
    ChannelMessageReceived,
    ChannelOutcome,
    ChannelPostRefused,
    ChannelPostResolved,
    ChannelPostSubmitted,
    ChannelRepeatHeard,
    ChannelSet,
    ChannelSetChanged,
    ChannelUndecryptable,
    ChannelUnknown,
    ChannelUnsupportedText,
)
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
from sighop.net.room import (
    DeliveryAcknowledged,
    DeliverySent,
    LoginAdmitted,
    LoginRefused,
    MemberBackedOff,
    PostRefused,
    PostStored,
    RequestAnswered,
    RequestRefused,
    RetentionPruned,
    RoomEvent,
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
    WireText,
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


# Decoding holds no keys at all — direct-message keys live in `net/dm.py` and
# channel keys in `net/channels.py`, and both report their own outcome on their
# own line. A detail line that said "no key held" therefore printed a failure
# about every message this station can read: the live exercise saw
# `not decrypted (no key held)` immediately above the decrypted channel message
# it belongs to. The line now states the stage it describes.
_NOT_HERE = "not decrypted at decode (keys are tried by the layer that holds them)"


def _render_payload(payload: ParsedPayload) -> str:
    match payload:
        case DirectEnvelope(dest_hash=dest, src_hash=src, mac=mac, ciphertext=ciphertext):
            return (
                f"encrypted  dest=0x{dest:02x} src=0x{src:02x} mac={mac.hex()} "
                f"ciphertext={len(ciphertext)}B  {_NOT_HERE}"
            )
        case AnonRequestEnvelope(dest_hash=dest, mac=mac, ciphertext=ciphertext):
            return (
                f"encrypted  dest=0x{dest:02x} sender_key={payload.sender_public_key.hex()[:16]} "
                f"mac={mac.hex()} ciphertext={len(ciphertext)}B  {_NOT_HERE}"
            )
        case GroupEnvelope(channel_hash=channel, mac=mac, ciphertext=ciphertext):
            return (
                f"encrypted  channel=0x{channel:02x} mac={mac.hex()} "
                f"ciphertext={len(ciphertext)}B  {_NOT_HERE}"
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


PERSISTENCE_OFF = "off"
PERSISTENCE_ON = "on"
PERSISTENCE_DEGRADED = "degraded"
"""Three states, and the difference between the first two and the third is the
whole point (design D8): "off" is a choice an operator made and "degraded" is a
fault they have not yet noticed. A status line that rendered them alike would
make a broken database look like a deliberate configuration."""


def render_status(
    status: SchedulerStatus,
    *,
    dedup: DedupStats,
    learned_paths: int,
    active_overrides: int = 0,
    contacts: int = 0,
    persistence: str = PERSISTENCE_OFF,
    packet_log_discarded: int = 0,
    routes_discarded: int = 0,
    awaiting_backfill: int = 0,
    webhooks: str | None = None,
    channels: str | None = None,
) -> str:
    """The periodic status line. Duty cycle first — it is the limit that binds.

    The three persistence counters are always rendered, including as zeros. A
    field that disappears when it is zero is a field a reader cannot tell from a
    field nobody wrote, and the whole reason they are here is so that a gap in
    the feed reads as a gap rather than as a quiet mesh.
    """
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
        f"contacts={contacts} "
        f"persist={persistence} log_drop={packet_log_discarded} "
        f"route_drop={routes_discarded} backfill={awaiting_backfill}"
    )
    if active_overrides:
        line += f" overrides={active_overrides}"
    if webhooks is not None:
        # Only when webhooks are active: delivered/failed/dropped always
        # rendered together, zeros included, once there is a dispatcher at all.
        line += f" {webhooks}"
    if channels is not None:
        line += f" {channels}"
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


def render_persistence(
    *,
    database: str | None = None,
    schema_version: str | None = None,
    entities: int = 0,
    contacts: int = 0,
    paths: int = 0,
    conversations: int = 0,
    direct_messages: int = 0,
    channel_messages: int = 0,
    channel_posts_unknown: int = 0,
    writing: bool = True,
    not_writing_because: str = "",
) -> str:
    """Whether this run's state survives it, and what came back if it does.

    An operator must never have to infer durability. With no database the line
    says so in the words milestone 4 used, because that is still what happens;
    with one it names the database in force, the applied schema version and the
    counts restored — so "nothing was heard yet" and "nothing was restored" are
    two visibly different things before any traffic arrives.

    The restored counts are a snapshot from before the pipeline started, unlike
    the live figures in the status line: they are what persistence supplied, not
    what the radio has since added.

    `database` is expected already redacted — this function never sees a
    password, so there is no rendering path along which one could escape.
    """
    if database is None:
        return (
            "persistence: off — contacts, paths and the packet log are in memory "
            "only and do not survive the process"
        )
    state = "on" if writing else f"on, not writing ({not_writing_because})"
    # Conversations are counted, not restored into memory: there is no in-memory
    # store of them to fill. They are on this line anyway, because the question
    # a restart has to answer is "is what I said still there", and a run that
    # said nothing about it leaves "no messages" and "not looked yet" identical.
    # Channel messages are on the line for the reason the conversations are: a
    # restart's first question is whether what was said is still there, and the
    # live exercise found the count in the `persistence_restored` event and
    # nowhere an operator reads. Posts the last stop left mid-flight are named
    # when there are any, because their outcome is now `unknown` on purpose.
    unresolved = (
        f" ({channel_posts_unknown} post(s) the last stop left unresolved)"
        if channel_posts_unknown
        else ""
    )
    return (
        f"persistence: {state} — {database}  schema={schema_version or 'unknown'}\n"
        f"restored: entities={entities} contacts={contacts} paths={paths}  "
        f"held: conversations={conversations} messages={direct_messages} "
        f"channel_messages={channel_messages}{unresolved}"
    )


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


# --- Channels (change `channel-messaging`) ----------------------------------
#
# A channel sender name is plaintext anyone with the key can write. It is
# rendered only as a claim — `CLAIMED_MARK`, the word "claimed" and the name
# quoted — and never in the shape a verified advert or identity takes. A post
# is ours, so its identity is named plainly; its outcome never claims delivery.
#
# No reception line carries the reception's packet id: it is minted per
# reception, so a replay would render differently every time, and the frame line
# above each one already places it. The id is in the logged event.

CHANNELS_OFF = (
    "channels: none — channels are stored configuration, and that requires durable "
    "storage; group text is left undecrypted"
)


def render_channel_startup(channels: ChannelSet) -> str:
    """The channels a run loaded, with hashes, and any it had to skip."""
    if not channels.channels and not channels.skipped:
        return "channels: none configured; group text is left undecrypted"
    loaded = ", ".join(
        f"{channel.name}[{channel.channel_hash:02x}]"
        f"({channel.kind}{', guessable' if channel.guessable else ''})"
        for channel in channels
    )
    line = f"channels: {loaded or 'none loaded'}"
    for name in channels.skipped:
        line += (
            f"\n{FAILURE_MARK} channel {name!r} skipped: its pre-shared key does not open "
            "under SIGHOP_SECRET_KEY"
        )
    return line


def render_channel_status(
    *, decrypted: int, unknown: int, undecryptable: int, transmitted: int, repeats: int
) -> str:
    """The status line's channel segment, zeros included."""
    return (
        f"ch_rx={decrypted} ch_unknown={unknown} ch_undecryptable={undecryptable} "
        f"ch_tx={transmitted} ch_repeats={repeats}"
    )


def _actor(actor: str | None) -> str:
    return "" if actor is None else f"  by account {actor!r}"


def render_channel_message_received(event: ChannelMessageReceived) -> str:
    sender = (
        "no sender"
        if event.unverified_sender_name is None
        else f"claimed {event.unverified_sender_name!r}"
    )
    hops = "?" if event.hop_count is None else str(event.hop_count)
    return (
        f"{_INDENT}{CLAIMED_MARK} {event.channel_name} from {sender} (unverified)  "
        f"h{hops}: {event.body!r}  ts={event.wire_timestamp}"
    )


def render_channel_unknown(event: ChannelUnknown) -> str:
    return (
        f"{_INDENT}group text on unknown channel 0x{event.channel_hash:02x}"
    )


def render_channel_undecryptable(event: ChannelUndecryptable) -> str:
    return (
        f"{_INDENT}group text on channel 0x{event.channel_hash:02x} not decrypted after "
        f"{event.channels_tried} channel key(s)"
    )


def render_channel_unsupported(event: ChannelUnsupportedText) -> str:
    return (
        f"{_INDENT}{event.channel_name}: text type {event.txt_type} is not plain; "
        "counted, not shown"
    )


def render_channel_post_submitted(event: ChannelPostSubmitted) -> str:
    return (
        f"{_INDENT}-> {event.channel_name} post as {event.entity_name}: {event.text!r}  "
        f"flood class 2  {event.packet_bytes}B  ts={event.wire_timestamp}  "
        f"post={event.post_id}{_actor(event.actor)}"
    )


def render_channel_post_refused(event: ChannelPostRefused) -> str:
    channel = event.channel_name or f"channel {event.channel_id}"
    return (
        f"{_INDENT}{FAILURE_MARK} {channel} post as {event.entity_name} refused: "
        f"{event.reason}{_actor(event.actor)}"
    )


def render_channel_post_resolved(event: ChannelPostResolved) -> str:
    if event.outcome is ChannelOutcome.TRANSMITTED:
        return (
            f"{_INDENT}{event.channel_name} post as {event.entity_name} transmitted "
            f"(no acknowledgement exists for channel messages)  "
            f"air={event.airtime_ms:.0f}ms  post={event.post_id}  id={event.packet_id}"
        )
    return (
        f"{_INDENT}{FAILURE_MARK} {event.channel_name} post as {event.entity_name} not "
        f"transmitted: {event.reason}; not retried  post={event.post_id}"
    )


def render_channel_repeat_heard(event: ChannelRepeatHeard) -> str:
    hops = "?" if event.hop_count is None else str(event.hop_count)
    return (
        f"{_INDENT}<- {event.channel_name} post repeated by a repeater (heard "
        f"{event.repeats_heard}x{', duplicate' if event.duplicate else ''})  h{hops} "
        f"snr={_render_snr(event.snr_db)}  post={event.post_id}"
    )


def render_channel_set_changed(event: ChannelSetChanged) -> str:
    """A channel added or removed elsewhere, named in the run that adopted it."""
    parts = [f"+{name}" for name in event.added] + [f"-{name}" for name in event.removed]
    return (
        f"channels changed: {', '.join(parts)}  "
        f"({event.loaded} loaded; group text is trialled against all of them)"
    )


def render_channel_config_read_failed(event: ChannelConfigReadFailed) -> str:
    return (
        f"{FAILURE_MARK} channels could not be re-read ({event.error}); "
        f"keeping the {event.channels_kept} already loaded"
    )


def render_channel_event(event: ChannelEvent) -> str:
    match event:
        case ChannelMessageReceived():
            return render_channel_message_received(event)
        case ChannelUnknown():
            return render_channel_unknown(event)
        case ChannelUndecryptable():
            return render_channel_undecryptable(event)
        case ChannelUnsupportedText():
            return render_channel_unsupported(event)
        case ChannelPostSubmitted():
            return render_channel_post_submitted(event)
        case ChannelPostRefused():
            return render_channel_post_refused(event)
        case ChannelPostResolved():
            return render_channel_post_resolved(event)
        case ChannelRepeatHeard():
            return render_channel_repeat_heard(event)
        case ChannelConfigReadFailed():
            return render_channel_config_read_failed(event)
        case ChannelSetChanged():
            return render_channel_set_changed(event)
    raise AssertionError(f"unhandled channel event {event!r}")  # pragma: no cover


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


# --- Rooms (milestone 6) ----------------------------------------------------
#
# Pure formatting, and two rules that are not cosmetic. **No password and no
# password hash may appear here**, which is asserted against the actual output
# rather than trusted to review (task 11.3). And a member is identified by a key
# prefix rather than a name: a room server never learns a member's name from the
# login exchange, and inventing one from a contact would present a claim as a
# fact.

ROOMS_OFF = (
    "rooms: none — a room is bound to a stored identity, and stored identities "
    "require durable storage"
)
"""What `sighop run` says with no database configured (design D5). Stated rather
than omitted: a run that silently served no rooms would look identical to one
whose rooms failed to load."""


def render_room_startup(
    *,
    name: str,
    entity_name: str,
    node_hash: int,
    members: int,
    messages: int,
    guest_access: str,
    retention: str,
    served: bool = True,
    not_served_because: str = "",
) -> str:
    """One room, before any traffic is handled.

    `retention` is expected to read "unlimited" when no policy is set, which is
    said in words rather than shown as a blank: a bound nobody set and a bound
    nobody can see are the same thing to an operator (design D15).
    """
    head = (
        f"room {name!r} on {entity_name}[{node_hash:02x}]  "
        f"members={members} messages={messages}  "
        f"guest={guest_access}  retention={retention}"
    )
    if served:
        return head
    return f"{head}  NOT SERVED ({not_served_because})"


def render_room_status(
    *,
    name: str,
    members: int,
    messages_stored: int,
    deliveries_outstanding: int,
    members_behind: int,
    accepting_posts: bool,
    refusals: dict[str, int],
    pruned: int,
    pruned_unsynced: int,
) -> str:
    """The periodic room line.

    Every counter is rendered, zeros included, for the reason the persistence
    counters are: a field that disappears when it is zero cannot be told from a
    field nobody wrote. `refusals` is rendered whole rather than as a total —
    "six refused" says nothing an operator can act on, and "bad_password=6" says
    what to do about it.
    """
    line = (
        f"== room {name!r} members={members} stored={messages_stored} "
        f"outstanding={deliveries_outstanding} behind={members_behind} "
        f"pruned={pruned} pruned_unsynced={pruned_unsynced}"
    )
    if not accepting_posts:
        line += "  REFUSING POSTS (storage degraded)"
    reasons = ",".join(f"{reason}={count}" for reason, count in sorted(refusals.items()))
    line += f" refused={reasons or 'none'}"
    return line


def render_login_admitted(event: LoginAdmitted) -> str:
    joined = "joined" if event.new_member else "returned"
    arrival = "flooded" if event.flooded else "direct"
    return (
        f"{_INDENT}<- room {event.room_name!r}  {joined} "
        f"{CLAIMED_MARK}{event.public_key.hex()[:16]}  "
        f"as {event.permission.name.lower()}  ({arrival})  id={event.packet_id}"
    )


def render_login_refused(event: LoginRefused) -> str:
    """A refusal is silent on the air and never silent here (design D8)."""
    who = "" if event.public_key is None else f"  {CLAIMED_MARK}{event.public_key.hex()[:16]}"
    detail = f"  {event.detail}" if event.detail else ""
    return (
        f"{_INDENT}<- room {event.room_name!r}  login refused: {event.reason}{who}"
        f"{detail}  id={event.packet_id}"
    )


def render_post_stored(event: PostStored) -> str:
    marks = []
    if event.retry:
        marks.append("retry")
    if event.truncated_from is not None:
        # The author's words were dropped, so say so on the line that claims to
        # show the post rather than only in the log.
        marks.append(f"TRUNCATED from {event.truncated_from} bytes")
    if not event.acknowledged:
        marks.append("NOT ACKNOWLEDGED")
    suffix = f"  ({', '.join(marks)})" if marks else ""
    return (
        f"{_INDENT}<- room {event.room_name!r}  post @{event.post_timestamp} "
        f"from {CLAIMED_MARK}{event.author.hex()[:16]}  "
        f"{_render_wire_text(event.text)}{suffix}"
    )


def render_post_refused(event: PostRefused) -> str:
    who = "" if event.author is None else f"  {CLAIMED_MARK}{event.author.hex()[:16]}"
    detail = f"  {event.detail}" if event.detail else ""
    return (
        f"{_INDENT}<- room {event.room_name!r}  post refused: {event.reason}{who}"
        f"{detail}  id={event.packet_id}"
    )


def render_delivery_sent(event: DeliverySent) -> str:
    state = "pushed" if event.transmitted else "not sent (gate closed)"
    return (
        f"{_INDENT}-> room {event.room_name!r}  {state} @{event.post_timestamp} "
        f"to {event.member.hex()[:16]}  {event.route.label}  "
        f"expect={event.expected_ack.hex()}  id={event.packet_id}"
    )


def render_delivery_acknowledged(event: DeliveryAcknowledged) -> str:
    how = " (bundled in a path return)" if event.bundled else ""
    return (
        f"{_INDENT}<- room {event.room_name!r}  delivery acknowledged "
        f"@{event.post_timestamp} by {event.member.hex()[:16]}{how}  "
        f"id={event.packet_id}"
    )


def render_member_backed_off(event: MemberBackedOff) -> str:
    return (
        f"{_INDENT}!! room {event.room_name!r}  {event.member.hex()[:16]} backed off "
        f"after {event.failures} unacknowledged deliveries; "
        "delivery resumes when it is next heard from"
    )


def render_request_answered(event: RequestAnswered) -> str:
    name = getattr(event.request_type, "name", str(event.request_type))
    return (
        f"{_INDENT}-> room {event.room_name!r}  answered {name.lower()} "
        f"for {event.member.hex()[:16]}  {event.reply_bytes}B  id={event.packet_id}"
    )


def render_request_refused(event: RequestRefused) -> str:
    name = getattr(event.request_type, "name", str(event.request_type))
    who = "" if event.member is None else f"  {event.member.hex()[:16]}"
    return (
        f"{_INDENT}<- room {event.room_name!r}  request {name.lower()} unanswered: "
        f"{event.reason}{who}  id={event.packet_id}"
    )


def render_retention_pruned(event: RetentionPruned) -> str:
    """Design D15: what retention cost, including what it cost a member."""
    line = (
        f"{_INDENT}room {event.room_name!r}  retention removed {event.deleted} messages"
    )
    if event.deleted_unsynced:
        line += (
            f", {event.deleted_unsynced} of which a member had not yet received "
            "— those members' history has a gap"
        )
    return line


def render_room_event(event: RoomEvent) -> str:
    match event:
        case LoginAdmitted():
            return render_login_admitted(event)
        case LoginRefused():
            return render_login_refused(event)
        case PostStored():
            return render_post_stored(event)
        case PostRefused():
            return render_post_refused(event)
        case DeliverySent():
            return render_delivery_sent(event)
        case DeliveryAcknowledged():
            return render_delivery_acknowledged(event)
        case MemberBackedOff():
            return render_member_backed_off(event)
        case RequestAnswered():
            return render_request_answered(event)
        case RequestRefused():
            return render_request_refused(event)
        case RetentionPruned():
            return render_retention_pruned(event)


# --- Bots (milestone 7) -----------------------------------------------------
#
# Pure formatting, and one rule that is not cosmetic: **an observe-mode decision
# is never formatted like a transmission.** A bot in observe mode ran its whole
# decision path and put nothing on the air, and a line that looked like a send
# would make a dry run indistinguishable from a live one — which is the whole
# thing the dry run exists to establish (design D4). It gets its own verb, its
# own arrow and an explicit statement that nothing was transmitted.
#
# `bots/` never imports this module: every function here takes a typed event or
# plain values, exactly as the room renderers do (milestone 6 design D17).

WEBHOOKS_OFF_NO_DATABASE = (
    "webhooks: none — webhooks are stored configuration, and that requires durable storage"
)
WEBHOOKS_OFF_REPLAY = (
    "webhooks: none — replay; a replayed reception carries an earlier session's "
    "timestamps and would announce an old sighting as new"
)
WEBHOOKS_OFF_NO_SECRET = (
    "webhooks: none — SIGHOP_SECRET_KEY is not usable, and webhook URLs are sealed under it"
)


def render_webhook_startup(summary: str) -> str:
    return f"webhooks: {summary}"


BOTS_OFF = (
    "bots: none — a bot's decisions depend on state restored before any traffic, "
    "and that requires durable storage"
)
"""What `sighop run` says with no database configured (design D5). §7 rule 1 —
"new means never seen in the persistent contact table, not new since process
start" — is a statement about a table that does not exist without one, so a
DB-less greeter would greet the entire neighbourhood on every restart. Stated
rather than omitted, exactly as the equivalent room line is."""

OBSERVE_NOTE = "transmitted nothing (observe mode)"
"""One phrase, used everywhere an observation is rendered, so an operator learns
it once."""


def render_bot_startup(
    *,
    name: str,
    driver: str,
    node_hash: int,
    mode: str,
    limits: dict[str, object],
    served: bool = True,
    not_served_because: str = "",
) -> str:
    """One bot, before any traffic is handled.

    The mode is on the startup line rather than only in `bot show`, because
    "this bot is in observe mode" and "this bot is broken" look identical in a
    run's output otherwise.
    """
    rendered = " ".join(f"{key}={_render_limit(value)}" for key, value in limits.items())
    head = f"bot {name!r}[{node_hash:02x}]  driver={driver}  mode={mode}  {rendered}".rstrip()
    if served:
        return head
    return f"{head}  NOT RUNNING ({not_served_because})"


def render_bot_status(
    *,
    name: str,
    driver: str,
    mode: str,
    actions: int,
    observations: int,
    suppressions: dict[str, int],
    dropped: int,
    failures: int,
    pending: int = 0,
    announces: int = 0,
) -> str:
    """The periodic bot line.

    Every counter is rendered at zero too, and the suppressions are rendered
    whole rather than as a total: "eleven suppressed" says nothing an operator
    can act on, and "too_many_hops=11" says which bound to reconsider.
    """
    line = (
        f"== bot {name!r} driver={driver} mode={mode} acted={actions} "
        f"announced={announces} observed={observations} pending={pending} "
        f"dropped={dropped} failures={failures}"
    )
    reasons = ",".join(f"{reason}={count}" for reason, count in sorted(suppressions.items()))
    return f"{line} suppressed={reasons or 'none'}"


def render_bot_acted(event: BotActed) -> str:
    outcomes = {
        BotSendResult.ACKNOWLEDGED: "acknowledged",
        BotSendResult.UNACKNOWLEDGED: "NOT ACKNOWLEDGED",
        BotSendResult.REFUSED: "not sent",
        BotSendResult.OBSERVED: OBSERVE_NOTE,
    }
    outcome = outcomes[event.result]
    return (
        f"{_INDENT}-> bot {event.bot_name!r}  sent to "
        f"{CLAIMED_MARK}{event.contact.public_key.hex()[:16]}  {event.route}  "
        f"attempts={event.attempts}  {outcome}  {event.text!r}"
    )


def render_bot_would_act(event: BotWouldAct) -> str:
    """An observation. Deliberately shaped nothing like `render_bot_acted`."""
    return (
        f"{_INDENT}.. bot {event.bot_name!r}  would send to "
        f"{CLAIMED_MARK}{event.contact.public_key.hex()[:16]}  {OBSERVE_NOTE}  "
        f"{event.text!r}"
    )


def render_bot_suppressed(event: BotSuppressed) -> str:
    """A decision not to act. A greeter that is greeting nobody has to be
    distinguishable from a mesh that has gone quiet."""
    who = (
        ""
        if event.contact is None
        else f"  {CLAIMED_MARK}{event.contact.public_key.hex()[:16]}"
    )
    detail = f"  {event.detail}" if event.detail else ""
    return f"{_INDENT}.. bot {event.bot_name!r}  suppressed: {event.reason}{who}{detail}"


def render_bot_dispatch_dropped(event: BotDispatchDropped) -> str:
    return (
        f"{_INDENT}!! bot {event.bot_name!r}  dropped the oldest pending dispatch "
        f"(queue full, {event.dropped} so far); reception is unaffected"
    )


def render_bot_failed(event: BotFailed) -> str:
    return (
        f"{_INDENT}!! bot {event.bot_name!r}  driver {event.driver} raised on "
        f"{event.event}: {event.error}  (failures={event.failures}); the run continues"
    )


def render_bot_event(event: BotRuntimeEvent) -> str:
    match event:
        case BotActed():
            return render_bot_acted(event)
        case BotWouldAct():
            return render_bot_would_act(event)
        case BotSuppressed():
            return render_bot_suppressed(event)
        case BotDispatchDropped():
            return render_bot_dispatch_dropped(event)
        case BotFailed():
            return render_bot_failed(event)


def render_bot_mode_change(name: str, mode: BotMode) -> str:
    """What `sighop bot mode` prints, which is where the two gates get said.

    The mode is one of them and the run's `--enable-transmit` is the other, and
    an operator who has just made a bot active is exactly the person who needs
    to be told that the second one still applies (design D4).
    """
    if mode is BotMode.ACTIVE:
        return (
            f"bot {name!r} is now active: it may transmit. Transmission still "
            "requires the run's --enable-transmit flag and stays under the "
            "duty-cycle ceiling"
        )
    return (
        f"bot {name!r} is now in observe mode: it runs its whole decision path "
        "and transmits nothing"
    )


def _render_limit(value: object) -> str:
    return "none" if value is None else str(value)


def _render_wire_text(text: WireText) -> str:
    """Text off the wire, marked as a rendering when it is not valid UTF-8.

    §4.1's rule at the display edge: the bytes are what was stored, and what is
    shown is a rendering of them. Presenting a replacement-charactered string as
    the author's text would be presenting our guess as their content.
    """
    if text.is_valid_utf8:
        return repr(text.text)
    return f"{text.text!r} (rendering of {len(text.raw)} bytes, not valid UTF-8)"
