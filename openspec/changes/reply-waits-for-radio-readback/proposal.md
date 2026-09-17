## Why

A stock MeshCore companion sent a direct message to sighop during the `create-entity-with-known-key`
live exercise. The message decrypted and was reported. It was never acknowledged:

```
ack_not_routed  packet_id=6fffd87eb8c24ae9
  reason="no GetRadio readback available; refusing to compute airtime from configured values"
direct_message_received  claimed_sender=[redacted]…  acknowledged=false
```

The refusal itself is right — §4.1 forbids pricing a transmission against configuration the board
may not be running. The *ordering* is wrong, and this is the second time it has been wrong in the
same way. `Runtime._send_once` already carries the scar:

> Both wait for startup, because startup is what adopts the board's radio readback — and without
> one the scheduler refuses to compute airtime and drops the packet. Observed doing exactly that on
> the first attempt at the one-shot advert: correct refusal, wrong ordering.

That fix gated the *one-shot* paths on `Runtime._ready`. Nothing gated the **reactive** ones. The
RX pipeline (`_consume`) is started in the same breath as `_print_startup`, which is what awaits the
probe and calls `set_radio`, so every frame the modem buffered while the database opened arrives
before the board's parameters do. A reply composed from one of those frames is dropped.

It is not a narrow window. The exercise's inbound message, its two copies and the peer's advert were
all delivered in the same millisecond, ahead of the startup banner — the modem's buffer flushing at
once. Any peer that messages a sighop node during its first second goes unanswered, and retries into
silence.

## What Changes

- A reactive transmission composed before the board has answered its readback **waits** for that
  readback, bounded, instead of being dropped. The readback lands milliseconds after the probe and
  an acknowledgement window is seconds wide, so in practice nothing is late.
- The wait applies to every path with this defect, not only the observed one:
  - an inbound direct message's **acknowledgement** (`dm.py::_acknowledge`) — **observed live**
  - a room server's **reply** to a request (`room.py::_submit_reply`)
  - a room server's **push** to a member (`room.py::_send_push`)
- The refusal survives for the case it was written for: a board that answers *nothing*. On timeout
  the transmission is still refused and still reported, with a reason that says the wait expired
  rather than implying the readback was asked for and absent.
- `Runtime.set_radio` gains the signal these waits watch: adopted when a readback arrives, withdrawn
  when one is lost, so a reply composed during a reconnect waits for the new parameters instead of
  being priced against the old ones or dropped.
- The refusal counters an operator sees keep counting what they counted; a wait that succeeds is not
  a refusal and is not reported as one.

## Capabilities

### New Capabilities

None. This corrects the ordering of behaviour three existing capabilities already specify.

### Modified Capabilities

- `airtime`: the refusal to compute time on air without a readback is specified as applying to a
  board that has not answered, distinguished from one that has not answered *yet*.
- `direct-messaging`: an inbound message that arrives before the readback is still acknowledged.
- `room-server`: a reply and a push composed before the readback are still sent.

## Impact

- `src/sighop/runtime.py` — `set_radio` sets and clears the readiness signal; the existing `_ready`
  event and its single consumer stay as they are.
- `src/sighop/net/dm.py` — `_acknowledge` (the observed defect) and the outbound send path at
  `dm.py:859` wait before refusing.
- `src/sighop/net/room.py` — `_submit_reply` and `_send_push` wait before refusing.
  `_post_ack_window` is **not** in scope: it already degrades to a 100 s window rather than dropping,
  which is a different decision and a correct one.
- `src/sighop/net/airtime.py` — unchanged. `require_params` keeps refusing; what changes is when it
  is called.
- Tests: `tests/test_dm.py`, `tests/test_room_requests.py`, `tests/test_runtime.py`, and a regression
  test in the shape of the one at `tests/test_first_transmit.py:664`, which already pins the
  one-shot half of this bug.
- No wire change, no schema change, no configuration change. A run whose board answers promptly
  behaves exactly as it does today.
