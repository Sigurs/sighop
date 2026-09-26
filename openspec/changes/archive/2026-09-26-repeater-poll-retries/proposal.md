# Proposal

## Why

Production polls over the 18 hours to 2026-09-25 21:00 UTC show that one lost packet costs a whole
sample. Two selected repeaters ([redacted], [redacted]) never answered at all and are taken as
password-protected or down. Of the 144 polls of the other eight, only 67 succeeded:

- 55 were login unanswered.
- 13 were status unanswered right after an answered login.
- 9 were neighbours incomplete, all at offset 0, right after an answered status.

In each of those 22 mid-poll failures the packet log shows the first relay forwarding our last
request. The route was working seconds earlier, so the request or its answer was lost further along,
and one resend would most likely have recovered it. Answers that do arrive come 1–2.5 s after the
request, well inside the step timeout, so the timeout is not cutting them off.

The collector sends every step exactly once and ends the poll at the first silence. It also always
uses the most recently learned direct route, even after many failed polls. For example:

- One repeater was polled 18 times along one route and failed 15 of them at login.
- Another repeater failed 14 logins. In 9 of those, the first relay never forwarded the request.

Stock clients handle this differently: they resend, and they fall back to a flood when a direct
route stops working.

## What Changes

- **Each step is retried.** A login, status request or neighbour page that goes unanswered within
  its timeout is sent again with a fresh timestamp, up to a fixed limit, before the poll ends. The
  answer to an earlier attempt of the same step is still accepted if it arrives late.
- **Login falls back to a flood.** When a repeater that has answered a login within the recency
  window does not answer any direct login attempt, the last login attempt is flooded. Its answer comes back with a path
  return, which gives us a fresh route. The existing flooded-answer handling then applies: we wait
  for the re-floods to settle and send a path return.
- **Silent repeaters are not retried.** A repeater that has not answered a login within the
  recency window gets one direct login attempt per cycle, as today, and no flood. This covers the
  password-protected or down repeaters, and it bounds flooding at a repeater that gains a guest
  password later.
- **Retries are recorded.** Each poll stores how many requests were sent again. The route label
  shows the flood fallback, for example `DIRECT h1 → FLOOD`. Collector counters report retries and
  flood fallbacks.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `repeater-metrics`:
  - The poll requirement gains per-step retries and the login flood fallback.
  - The answer-matching requirement changes: an unanswered attempt is retried instead of ending
    the poll, and a late answer to an earlier attempt of the same step is accepted.
  - The recording requirement gains the retry count.

## Impact

- `src/sighop/net/collect.py`: step retry loop, per-step tag set, flood fallback for login,
  recently-answered map, new counters.
- `src/sighop/db/repositories.py`:
  - `PollRecord.retries`.
  - `RepeaterPollRepository.record` stores the count.
  - New `last_answered()` read.
- `src/sighop/db/models.py`, `alembic/versions/0013_*`: nullable `repeater_poll.retries` column.
  This builds on `0012` from the in-flight `repeater-metrics-history` change.
- Tests: `tests/test_collect.py`, `tests/test_repeater_repositories.py`, `tests/test_db_schema.py`.
- A worst-case poll of an answering-but-unreachable repeater grows from about 12 s to about 50 s.
  Cycles stay well inside the 60-minute default interval. There is at most one flooded login per
  such repeater per cycle, at the lowest priority.
- No new dependency. No protocol change. No web change beyond what the new column needs.
