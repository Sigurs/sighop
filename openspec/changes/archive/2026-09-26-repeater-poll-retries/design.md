# Design

## Context

`RepeaterCollector.poll` (`src/sighop/net/collect.py`) calls `_step` once per step, and the first
`None` (silence) ends the poll. `_step` builds one packet, registers one `_Outstanding` with a
single echo `tag`, submits it, and waits `ack_timeout_ms(...) + 300 ms`. For a one-hop direct
route that is about 12 s. `_route` always takes `PathStore.lookup_public_key`, which is the most
recently confirmed path. Paths are re-confirmed by every advert heard, so the route to a repeater
flips with whichever advert arrived last. It carries no memory of whether that path ever delivered
a poll.

The production evidence behind the choices below:

- **Losses are drops, not late answers.** Across the 22 mid-poll failures in the last 18 h, every
  answer that did arrive came 1–2.5 s after the request, and the first relay's forward of our
  request was heard in nearly all of them.
- **One-hop and multi-hop routes both drop.** They fail at similar rates.
- **The zero-hop repeater ([redacted]) never failed.**

See proposal.md for the full numbers.

## Goals / Non-Goals

**Goals:**
- Recover single lost packets within a poll.
- Recover a repeater whose learned direct route has gone bad, without waiting for a new advert to
  replace that route.
- Make the effect measurable afterwards by SQL, the way this proposal was measured.

**Non-Goals:**
- Route quality scoring in `PathStore`. It is shared with direct messages and room delivery, so it
  is a separate change.
- Changing step timeouts. The data shows answers come early, not late.
- A second pass over failed repeaters at the end of a cycle.
- Any change for the repeaters that never answer ([redacted], [redacted]).
- Web display of the retry count. The column is stored, and showing it can follow with the
  metrics-history poll detail page.

## Decisions

### D1. The retry loop lives in `_step`, with one `_Outstanding` per step

`_step` makes up to `MAX_STEP_ATTEMPTS = 3` attempts. Each attempt:

1. takes a fresh `_timestamp`;
2. rebuilds the packet (the neighbour request keeps its offset and gets a new random blob);
3. re-resolves `_route`, so a route adopted from a path return mid-poll is used at once;
4. adds its tag to the step's tag set;
5. submits and waits its own timeout.

`_Outstanding.tag` becomes `tags: set[int]`, and `_accept` checks membership. The `_Outstanding`
and its future stay registered for the whole step, so a late answer to attempt 1 that lands while
attempt 2 is pending completes the step. Once the future is done, any further answer is already
counted as unmatched ("answer already received for this step").

A `_NotSent` result returns at once without a retry. The scheduler's refusal (transmit gate,
airtime ceiling, deadline) is not radio loss, and resending would only repeat the refusal.

*Alternative:* retry the whole poll from login. This re-sends packets that already worked, and it
loses the status that was already received before a neighbour-page loss. Rejected.

*Alternative:* resend the same bytes. Firmware drops a repeated timestamp as a replay, so the resend
must be a new request. Rejected.

### D2. Three attempts, and only the last login attempt floods

Three attempts matches stock client behaviour for direct messages. With per-step independent loss
of about 15–30% (the observed mid-poll rate), three attempts bring a step's loss under 3%.

Status and neighbour requests never flood. The login answer was received moments before, so the
route is proven. The flooded login is the route-repair tool. The repeater answers a flooded
`ANON_REQ` with a path return bundling the login answer (`on_bundled_response`). `PathBodyReader`
adopts that route before the callback runs. `_after_flooded_answer` then settles and sends our path
return as it does today. The status and neighbour requests that follow use the fresh route.

`_step(..., flood=True)` builds `Route(flood=True, hash_size=self.path_hash_size)` instead of
calling `_route`. `routes` already appends a label whenever it changes, so the stored route becomes
e.g. `DIRECT h1 → FLOOD → DIRECT h2` with no extra code.

When no route is known, `_route` already floods. That login gets one attempt: a flood is heard
mesh-wide, and repeating it would triple the cost for a repeater that is probably out of reach.

*Alternative:* flood the status or neighbour request too. That costs a mesh-wide flood for a route
that just worked. Rejected.

*Alternative:* flood after N failed polls, not within the poll. That leaves every poll in between
lost. Rejected.

### D3. "Answered recently" is an in-memory map seeded from the database

The collector keeps `_answered: dict[bytes, dt.datetime]`, public key → last answered login time.

- **Seeding.** It is seeded once, on the first cycle, from a new
  `RepeaterPollRepository.last_answered() -> Outcome[dict[bytes, dt.datetime]]`, which runs
  `SELECT public_key, max(started_at) ... WHERE outcome IN ('succeeded', 'status_unanswered',
  'neighbours_incomplete') GROUP BY public_key`. These are exactly the outcomes that imply an
  answered login.
- **Failed seed read.** If that read fails, the map stays empty and seeding is tried again next
  cycle. This errs toward fewer floods.
- **Updating.** Each accepted login answer updates the map with `self.clock.now()`.
- **The test.** A repeater counts as answered recently when
  `now - _answered[key] <= recent_days`, the same window used for eligibility.

Bounding by the recency window means a repeater that later gains a guest password stops being
flooded after `recent_days`, even though it keeps advertising.

*Alternative:* query per poll. That adds a database round-trip per repeater per cycle for a value
the collector itself produces. Rejected.

*Alternative:* bound by retention. With "keep forever" from the in-flight change, that means
forever. Rejected.

### D4. `retries` column: nullable int, counting resends across the poll

The poll counts `attempts - 1` summed over its steps, and passes the total as
`PollRecord.retries: int | None = None`. Migration `0013` adds a nullable `repeater_poll.retries`
column (down revision `0012`). Old rows stay NULL, which is distinguishable from "no retry needed".
`RepeaterPollRepository.record` writes it.

*Alternative:* put it in `reason`. `reason` is NULL on success, and it is prose, not queryable.
Rejected.

### D5. Counters and logs

- **`as_json` counters.** It gains `repeater_retries`, one per resent request, and
  `repeater_login_floods`, one per flood-fallback login sent.
- **Resend log line.** `repeater_request_sent` gains `attempt=n`.
- **Fallback log line.** A `repeater_login_flood_fallback` info line is logged when the fallback
  fires.

## Risks / Trade-offs

- **Floods cost mesh-wide airtime.** At most one flooded login per recently-answering, currently
  unreachable repeater per cycle. At `ADVERT` priority it yields to everything, and it is subject
  to the airtime ceiling. Of today's eight answering repeaters, typically two or three fail login
  per cycle.
- **Longer worst-case polls and cycles.** An answering-but-unreachable repeater takes about
  3 × 12–20 s. A cycle of eight could reach about 6 min in the worst case, well inside a 60-minute
  interval. The loop checks enable and transmit state between repeaters, but not between attempts.
  Each attempt still passes the transmit gate at submission, and a gated attempt ends the poll as
  not sent (D1).
- **A lost answer means the repeater answers twice.** If the repeater answered attempt 1 but the
  answer was lost, attempt 2 makes it answer again. That is harmless: the answer carries the new
  timestamp, and the firmware accepts a newer timestamp from a guest client.
- **A flooded answer arrives after the flood attempt's timeout.** It is counted as unmatched, and
  the poll ends as login unanswered, as today. A path return that arrives late still teaches the
  route through `PathBodyReader`, so the next cycle benefits.
- **A 1-byte-hash route is ambiguous.** One repeater's learned route `be` could match more than one
  repeater. This design does not address that. The flood fallback repairs the route when it
  fails.

## Migration Plan

1. The in-flight `repeater-metrics-history` change (migration `0012`) lands first. `0013` chains
   on it.
2. Deploy. The migration adds a nullable column and needs no backfill.
3. Verify against prod, read-only, after about 24 h:
   - success rate per repeater compared with the baseline in proposal.md;
   - the distribution of `retries`;
   - how often the route contains `FLOOD`, and how often those polls succeed.
4. **Rollback.** Revert the code. The `0013` downgrade drops the column. Older code ignores the
   column, so a code-only rollback is also safe.
