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
- `tx.py:516` `_airtime_ms` asks the same question again at dequeue and drops with
  `DropReason.NO_RADIO`. Not a fifth site: after this change a submission only happens once the
  readback exists, and a readback lost between submission and dequeue is a genuine mid-run loss the
  scheduler is right to drop and count.
- `dm.py:1086` `_handle_text_message` already awaits `_acknowledge` and carries its result into the
  durable record, the log line and `MessageReceived.acknowledged`, which `render.py:538` prints as
  `acked` / `NOT acked`. The report is therefore already sequenced behind the acknowledgement — see
  D5, which turns on exactly this.

## Goals / Non-Goals

**Goals:**

- A peer that messages a sighop node during its first second is answered.
- One mechanism for all four sites, so the next reactive path added inherits it.
- The refusal survives for a board that answers nothing, with a reason that says which case it is.

**Non-Goals:**

- Relaxing §4.1. Nothing is ever priced against configured values; this only changes *when*
  `require_params` is asked.
- Holding the RX pipeline. Receptions are the one thing a run must never be late to record, and they
  need no airtime figure. The single exception is the report of the message *being acknowledged*,
  which follows its own acknowledgement because the standing ordering rule requires it (D5) — one
  subscriber's own sequencing, not the pipeline.
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

### D5 — The ordering rule needs no defence, because the report already waits for the acknowledgement

`direct-messaging` requires the acknowledgement to be submitted before the report reaches any
consumer, "so that no consumer can delay or prevent it". The wait does not disturb that rule.
`_handle_text_message` already awaits `_acknowledge` and carries its result into the report; the
wait extends an await that is already there rather than reordering anything around it. Nothing new
is needed to preserve the guarantee — the code is already in the shape the guarantee describes.

So `acknowledged`, in the log line and in `MessageReceived`, keeps meaning exactly what it means
today: the acknowledgement reached the scheduler. It is never emitted before that is settled, and is
never `true` for a transmission that did not happen.

The cost is stated rather than hidden: in the startup window, and only there, the report of an
inbound message is delayed by however long the board takes, bounded by D3. That is the same delay
the acknowledgement itself takes, for the one message the run is in the middle of answering.

*Alternatives considered:* emit the report as soon as the acknowledgement is **committed** — route
resolved, packet built, outcome beyond any consumer's reach — and let only the submission complete
after the wait. Rejected in both forms it can take. Making `acknowledged` tri-state (submitted /
awaiting the board / refused) is honest, but widens the change into `MessageReceived` and every
consumer of it, including `render.py:538`, for a state that exists only in a run's first second.
Redefining `acknowledged` to mean *committed* keeps the diff small but lets the monitor print `acked`
on one line and `ack_not_routed` on the next. A field that reads true for a transmission that never
happened is a worse defect than a report that is a second late, and it is the kind that outlives the
bug it was introduced for.

## Risks / Trade-offs

- **A wait that never ends.** → D3's bounded budget; a regression test asserts the refusal still
  arrives when no readback ever does.
- **A wait that blocks the pipeline.** Checked rather than assumed: inbound direct messages are
  handled as a **bus subscriber**, and `bus.py::subscribe` gives each subscriber its own task and
  bounded queue. An inline `await` in `_acknowledge` therefore stalls only that subscriber — decode,
  dedup, path learning and every other subscriber continue. The same holds for rooms.
  → What it *can* do is let the direct-message subscriber's queue back up for the length of the
  budget — and, by D5, delay that subscriber's own reports for the same length — with a full queue
  dropping records. With a budget of seconds against a queue sized for normal traffic this is not
  reachable in practice, but a test asserts the rest of the pipeline keeps flowing during a wait, and
  the queue-overflow path already reports what it drops.
- **A reply that arrives after it was worth sending.** A very slow readback could see an ack
  submitted after the sender has already retried. → The budget sits inside the ack window by design,
  and a late acknowledgement is still better than none — the sender's retry is answered by the
  firmware's own de-duplication.
- **Four sites, one habit.** The next reactive path can forget to wait. → The mechanism is one
  helper, and the spec states the rule for the class rather than for the three instances.
