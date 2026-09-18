"""The conversations the panel shows, durable or not (`web-chat`).

A conversation has two possible homes and the panel reads both:

* the `direct_message` table, when this run has a database — which is what makes
  a conversation survive a restart;
* this module's bounded in-memory log, always — which is what makes chat work
  on a run with **no** database at all, and during an outage of one.

The second is not a cache of the first. It is what the `dm-history` spec means
by "sending and receiving continue" while nothing is being recorded: the
messages of this session are shown, and the interface says plainly that they are
not being kept.

The log is offered records on the same contract the durable sink has — never
awaits, never raises — because it is offered them from the same place, which is
the message path.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from sighop.net.channels import ChannelMessageRecord
from sighop.net.dm import DirectMessageRecord

DEFAULT_PER_CONVERSATION = 200
"""How much of one conversation this run keeps in memory. Bounded: a session
left running for a week is not a growing list of everything anybody said."""

type ConversationKey = tuple[bytes, bytes]


@dataclass(slots=True)
class ConversationLog:
    """This run's own view of its conversations. A `DirectMessageSink`.

    Keyed by `(local identity, peer)` — never by the peer alone. Two identities
    talking to one contact are two conversations, and merging them would
    attribute one identity's words to the other (`web-chat`).
    """

    capacity: int = DEFAULT_PER_CONVERSATION
    _messages: dict[ConversationKey, dict[str, DirectMessageRecord]] = field(default_factory=dict)
    _order: dict[ConversationKey, deque[str]] = field(default_factory=dict)
    unread: dict[ConversationKey, int] = field(default_factory=dict)
    offered: int = 0

    def offer(self, record: DirectMessageRecord) -> bool:
        """Take one record. Never awaits, never raises.

        Keyed on `ref`, so a send offered at submission and again when it
        resolves updates one entry rather than appearing twice — the same rule
        the durable store follows, for the same reason.
        """
        key = (record.entity_public_key, record.peer_public_key)
        held = self._messages.setdefault(key, {})
        order = self._order.setdefault(key, deque())
        if record.ref not in held:
            order.append(record.ref)
            while len(order) > self.capacity:
                held.pop(order.popleft(), None)
            if record.inbound:
                # Something arrived that the operator has not looked at. Cleared
                # when the conversation is opened, not when it is rendered
                # somewhere else.
                self.unread[key] = self.unread.get(key, 0) + 1
        held[record.ref] = record
        self.offered += 1
        return True

    # --- Reading ------------------------------------------------------------

    def conversation(
        self, entity_public_key: bytes, peer_public_key: bytes
    ) -> list[DirectMessageRecord]:
        """This session's messages for one conversation, newest first."""
        key = (entity_public_key, peer_public_key)
        held = self._messages.get(key, {})
        order = self._order.get(key, deque())
        return [held[ref] for ref in reversed(order) if ref in held]

    def conversation_keys(self) -> list[ConversationKey]:
        return list(self._messages)

    def latest(self, key: ConversationKey) -> DirectMessageRecord | None:
        messages = self.conversation(*key)
        return messages[0] if messages else None

    def new_for(self, key: ConversationKey) -> int:
        return self.unread.get(key, 0)

    def opened(self, entity_public_key: bytes, peer_public_key: bytes) -> None:
        """Mark a conversation as looked at."""
        self.unread.pop((entity_public_key, peer_public_key), None)

    def as_json(self) -> dict[str, object]:
        return {
            "conversations": len(self._messages),
            "messages_held": sum(len(held) for held in self._messages.values()),
            "records_offered": self.offered,
        }


# --- Channels (change `channel-messaging`, design D7) ------------------------

DEFAULT_PER_CHANNEL = 200
"""How much of one channel this run keeps in memory — the direct conversation
bound. Whether a busy Public channel makes it too small for a useful outage view
is an open question the design leaves to a busy band."""


@dataclass(slots=True)
class ChannelLog:
    """This run's own view of its channels. A `ChannelMessageSink`.

    The second sink beside the durable one, so channel chat keeps working while
    the database is degraded, and says so. Keyed on `(channel_id, ref)` like the
    table, so a post offered at submission, on resolution and on each repeat
    heard is one entry updated in place.
    """

    capacity: int = DEFAULT_PER_CHANNEL
    _messages: dict[int, dict[str, ChannelMessageRecord]] = field(default_factory=dict)
    _order: dict[int, deque[str]] = field(default_factory=dict)
    unread: dict[int, int] = field(default_factory=dict)
    offered: int = 0

    def offer(self, record: ChannelMessageRecord) -> bool:
        """Take one record. Never awaits, never raises."""
        held = self._messages.setdefault(record.channel_id, {})
        order = self._order.setdefault(record.channel_id, deque())
        if record.ref not in held:
            order.append(record.ref)
            while len(order) > self.capacity:
                held.pop(order.popleft(), None)
            if record.inbound:
                self.unread[record.channel_id] = self.unread.get(record.channel_id, 0) + 1
        held[record.ref] = record
        self.offered += 1
        return True

    def messages(self, channel_id: int) -> list[ChannelMessageRecord]:
        """This session's messages in one channel, newest first."""
        held = self._messages.get(channel_id, {})
        order = self._order.get(channel_id, deque())
        return [held[ref] for ref in reversed(order) if ref in held]

    def new_for(self, channel_id: int) -> int:
        return self.unread.get(channel_id, 0)

    def opened(self, channel_id: int) -> None:
        """Mark a channel as looked at."""
        self.unread.pop(channel_id, None)

    def as_json(self) -> dict[str, object]:
        return {
            "channels": len(self._messages),
            "channel_messages_held": sum(len(held) for held in self._messages.values()),
            "channel_records_offered": self.offered,
        }
