## Context

See proposal.md — Why, for the observation and the log line.

What the code holds today:

- `Runtime.run` starts `_consume()` (the RX pipeline) and `_print_startup()` as sibling tasks.
  `_print_startup` awaits `self.startup()`, which for a live run is `cli.py::_live_startup` —
  the function that waits on `modem.probe_ready` and calls `runtime.set_radio(observed)`. Until it
  returns, `radio` is `None` everywhere.
- `Runtime.set_radio` fans the parameters out: `scheduler`, `pipeline`, `messenger`, and each room.
  It is called at startup **and after every reconnect**, and may legitimately be called with `None`.
- `Runtime._ready` is an `asyncio.Event` set at the very end of `_print_startup`, with the comment
  "only now may anything be queued". Exactly one place waits on it: `_send_once`, the one-shot
  `--send` / `--advert-zero-hop` path, fixed after the same bug was observed there.
- The reactive paths call `require_params(self.radio)`, which raises `NoRadioReadback`, and each
  turns that into a refusal and a `return False`:

  | site | what is lost |
  |---|---|
  | `dm.py:1167` `_acknowledge` | an inbound DM's acknowledgement — **the observation** |
  | `room.py:1753` `_submit_reply` | a room reply (login result, post ack, status) |
  | `room.py:1481` `_send_push` | a room push to a member |
  | `dm.py:858` send attempt | an outbound DM, resolved as `DROPPED` |

- `room.py:1102` `_post_ack_window` also catches `NoRadioReadback`, but degrades to a 100 s window
  instead of dropping. That is a different and correct decision, and it is out of scope.

## Goals / Non-Goals

**Goals:**

- A peer that messages a sighop node during its first second is answered.
- One mechanism for all four sites, so the next reactive path added inherits it.
- The refusal survives for a board that answers nothing, with a reason that says which case it is.

**Non-Goals:**

- Relaxing §4.1. Nothing is ever priced against configured values; this only changes *when*
  `require_params` is asked.
- Holding the RX pipeline. Receptions are the one thing a run must never be late to record, and they
  need no airtime figure.
- `_post_ack_window`'s fallback, which is a degraded *estimate* of someone else's window rather than
  a price for our own transmission.
- Changing `Runtime._ready` or its one-shot consumer. That gate is about *startup completion*; this
  is about *radio availability*, and after a reconnect the second can lapse while the first stands.

## Decisions

### D1 — A radio-readiness signal on `Runtime`, set and cleared by `set_radio`

`set_radio` is already the single funnel every readback passes through, including reconnects. It
gains an `asyncio.Event` that it sets when handed parameters and **clears** when handed `None`.

Distinct from `_ready`, which means "startup finished". A reconnect that loses the board leaves
`_ready` set and this one clear, which is the state a reply must wait on — waiting on `_ready` would
sail straight through into the same refusal.

*Alternative considered:* reuse `_ready`. Rejected for that reconnect case, and because `_ready`
carries a second meaning (the capture writer has started, the banner has printed) that a reply has
no business waiting for.

### D2 — The wait lives with the waiter, not in `airtime.py`

`require_params` stays a pure, synchronous check. The waiting is done by the caller, which is the
only place that knows whether it is in a context that *may* wait — `_acknowledge` and
`_submit_reply` are already `async`, and `airtime.py` is a pure module the import-boundary test
keeps that way.

Each site becomes: await the signal within a budget, then call `require_params` exactly as now. On
timeout the existing refusal runs unchanged apart from its reason.

`DirectMessenger` and `RoomServer` receive the signal the same way they receive `radio` — pushed in
by `Runtime.set_radio` — so neither learns that `Runtime` exists. Where no signal has been supplied
(every unit test constructing a messenger directly, and the replay path, which has parameters from
the capture's provenance before it starts), the wait is a no-op and behaviour is exactly today's.

### D3 — One budget, named for what it is waiting for

A single constant, in the region of a few seconds: long enough to cover a probe that retries
(`SetRadio` is already retried 5 s × 3 after the CP2102-reset discovery), short enough to sit inside
an acknowledgement window so a late ack is still a useful one.

It is not derived from the ack timeout, because a room push has no ack timeout and the two would
then disagree about how long the board gets.

*Deliberately not infinite.* A board that never answers must still produce the refusal §4.1 asks
for, and a wait that cannot end would convert a loud refusal into a silent hang — the worse failure.

### D4 — The refusal says which case it is

Today every one of these reads `no GetRadio readback available; refusing to compute airtime from
configured values`, which is accurate but reads as "the board was asked and said nothing". After a
wait expires it should say that a wait expired. The existing `NoRadioReadback` message stays for the
genuinely-absent case; the timeout path names the budget it waited out.

This matters because the two call for different actions: one is a board or firmware problem, the
other is a board that is slow or gone mid-run.

### D5 — The acknowledgement's ordering guarantee is preserved by commitment, not by submission

`direct-messaging` requires the acknowledgement to be submitted before the report reaches any
consumer, "so that no consumer can delay or prevent it". A wait would reorder that if taken
literally.

The guarantee is about *consumers*, and is kept by committing the acknowledgement — taking its
decision, and putting it beyond any consumer's reach — before the report is emitted, with only the
scheduler submission completing after the wait. The report is not held behind the board.

*Alternative considered:* hold the report until the acknowledgement is submitted. Rejected: it makes
a slow board delay the operator's view of an inbound message, which is the opposite of what the
receive path is for.

## Risks / Trade-offs

- **A wait that never ends.** → D3's bounded budget; a regression test asserts the refusal still
  arrives when no readback ever does.
- **A wait that blocks the pipeline.** Checked rather than assumed: inbound direct messages are
  handled as a **bus subscriber**, and `bus.py::subscribe` gives each subscriber its own task and
  bounded queue. An inline `await` in `_acknowledge` therefore stalls only that subscriber — decode,
  dedup, path learning and every other subscriber continue. The same holds for rooms.
  → What it *can* do is let the direct-message subscriber's queue back up for the length of the
  budget, and a full queue drops records. With a budget of seconds against a queue sized for normal
  traffic this is not reachable in practice, but a test asserts receptions keep flowing during a
  wait, and the queue-overflow path already reports what it drops.
- **A reply that arrives after it was worth sending.** A very slow readback could see an ack
  submitted after the sender has already retried. → The budget sits inside the ack window by design,
  and a late acknowledgement is still better than none — the sender's retry is answered by the
  firmware's own de-duplication.
- **Four sites, one habit.** The next reactive path can forget to wait. → The mechanism is one
  helper, and the spec states the rule for the class rather than for the three instances.
