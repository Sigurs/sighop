## 1. The failing test first

- [ ] 1.1 Add a regression test in the shape of `tests/test_first_transmit.py:664` — a runtime whose startup resolves only when released, an inbound direct message delivered before that — and verify it fails today with the acknowledgement refused as `ack_not_routed`, which is the observation from the live exercise reproduced without hardware
- [ ] 1.2 Add the same for a room reply and a room push in `tests/test_room_requests.py`, and verify both fail today with `room_reply_not_sent` and `room_push_not_sent`

## 2. The readiness signal

- [ ] 2.1 Add the radio-readiness event to `Runtime`, set by `set_radio` when handed parameters and cleared when handed `None` (design D1), and verify a unit test covers set → clear → set across a simulated reconnect
- [ ] 2.2 Verify the new signal is independent of `_ready`: a test in which startup has completed and a reconnect has since cleared the radio finds `_ready` set and the radio signal clear
- [ ] 2.3 Pass the signal to `DirectMessenger` and each `RoomServer` through `set_radio`'s existing fan-out, and verify a messenger constructed without one — as every unit test and the replay path does — behaves exactly as today
- [ ] 2.4 Add the waiting helper and its budget constant (design D3), and verify unit tests for: the signal already set (returns immediately, no sleep), set during the wait, and never set (raises or returns the timeout case within the budget)

## 3. The four sites

- [ ] 3.1 Make `dm.py::_acknowledge` wait before `require_params` and verify 1.1 now passes — the acknowledgement is submitted, routed and reported as sent
- [ ] 3.2 Make `room.py::_submit_reply` wait, and verify the room reply half of 1.2 passes
- [ ] 3.3 Make `room.py::_send_push` wait, and verify the room push half of 1.2 passes, including that the member's unsynced state advances as it would have
- [ ] 3.4 Make the outbound send path at `dm.py:858` wait, and verify a send composed before the readback resolves as sent rather than `DROPPED`
- [ ] 3.5 Verify `room.py::_post_ack_window` is untouched and still degrades to its 100 s window, with a test asserting it does not wait — it estimates someone else's window rather than pricing our transmission

## 4. The refusal that remains

- [ ] 4.1 Give the timeout path its own reason naming the expired wait (design D4), distinct from `NoRadioReadback`'s "no GetRadio readback available", and verify a test asserts the two messages differ and that each names its own case
- [ ] 4.2 Verify every site still refuses when no readback ever arrives: the acknowledgement, the reply, the push and the send each report a refusal within the budget and the run keeps receiving
- [ ] 4.3 Verify the refusal counters and `RefusalReason.NO_RADIO_READBACK` still count what they counted, and that a wait which succeeds increments nothing

## 5. Not stalling the receive path

- [ ] 5.1 Verify receptions continue while a reply waits: with the readback withheld, publish further packets and assert they are decoded, reported and recorded — the `airtime` spec's requirement, and the reason the wait is per-transmission rather than a gate on `_consume`
- [ ] 5.2 Verify the direct-message report is not held behind the wait: the message is reported to consumers while its acknowledgement is still waiting (design D5)
- [ ] 5.3 Verify a slow or raising consumer cannot affect an acknowledgement that is waiting, preserving what the ordering rule in `direct-messaging` exists to guarantee

## 6. Verification

- [ ] 6.1 Verify the full suite passes — `pytest` — with attention to `tests/test_dm.py`, `tests/test_room_requests.py`, `tests/test_room_sync.py` and `tests/test_runtime.py`
- [ ] 6.2 Verify against the recorded case: replay a capture through `sighop run --replay` and confirm nothing waits, because the replay path has the radio from the capture's provenance before it starts
- [ ] 6.3 Verify live against the hardware that found this — KISS modem and a stock companion — by having the companion message sighop within its first second, and confirm the acknowledgement arrives and the companion reports the message as delivered rather than retrying
- [ ] 6.4 Record the ordering rule where the next reactive path will meet it: a note in `DESIGN.md` §4 that anything composed in reaction to a reception waits for the readback, alongside the existing one-shot note
