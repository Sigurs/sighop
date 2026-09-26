# Tasks

## 1. Storage

- [x] 1.1 Add `alembic/versions/0013_repeater_poll_retries.py` (down revision `0012`): nullable `repeater_poll.retries` Integer; downgrade drops it. Add `retries: Mapped[int | None]` to `RepeaterPollRow` in `src/sighop/db/models.py`. Verify `tests/test_db_schema.py` (model/migration parity) passes.
- [x] 1.2 Add `retries: int | None = None` to `PollRecord` and write it in `RepeaterPollRepository.record` (`src/sighop/db/repositories.py`). Verify with a test in `tests/test_repeater_repositories.py` that a recorded count reads back, and that an older row reads back NULL.
- [x] 1.3 Add `RepeaterPollRepository.last_answered() -> Outcome[dict[bytes, dt.datetime]]`: latest `started_at` per key among `succeeded`, `status_unanswered` and `neighbours_incomplete` polls (design D3). Verify with a repository test covering mixed outcomes: `login_unanswered`/`not_sent`-only keys are absent, and the latest answered time wins.

## 2. Collector fakes

- [x] 2.1 Extend `tests/collectfixtures.py`:
  - the fake poll sink implements `last_answered`, can be pre-seeded, and can be made to fail;
  - `FakeRepeater` can drop the first N requests of a given step, or answer only by flood, or answer an earlier attempt late.

  Verify the existing `tests/test_collect.py` still passes unchanged.

## 3. Step retries (design D1)

- [x] 3.1 In `src/sighop/net/collect.py`:
  - add `MAX_STEP_ATTEMPTS = 3`;
  - change `_Outstanding.tag` to a `tags` set and match `_accept` against it;
  - loop attempts inside `_step`, with a fresh timestamp, packet and `_route` per attempt, keeping one `_Outstanding` registered for the whole step;
  - return a `_NotSent` at once without retrying.

  Verify these new tests pass:
  - `test_a_status_request_lost_once_is_resent_and_the_poll_succeeds`
  - `test_a_neighbour_page_lost_once_is_resent_and_paging_continues`
  - `test_an_unanswered_status_ends_the_poll_before_neighbours` (existing, now asserts three status attempts)
  - `test_a_late_answer_to_the_first_attempt_completes_the_step`
  - the late-answer test also asserts the resend's own answer, arriving after, is unmatched
  - `test_a_not_sent_attempt_is_not_retried`
- [x] 3.2 Update existing tests that assumed a single attempt (`test_an_unanswered_status_ends_the_poll_before_neighbours`, `test_neighbours_cut_short_keep_the_status_and_the_first_page`, and any packet-count assertions) to the three-attempt behaviour. Verify with `uv run pytest tests/test_collect.py`.

## 4. Login policy and flood fallback (design D2, D3)

- [x] 4.1 Add a `_answered` map to the collector:
  - seed it on the first cycle from `polls.last_answered()`, retrying the seed next cycle on failure;
  - update it on every accepted login;
  - add `_answered_recently(key, settings, now)` using `recent_days`.

  Verify these tests pass:
  - `test_a_repeater_never_answered_gets_one_login_and_no_flood`
  - `test_a_repeater_that_stopped_answering_beyond_the_window_gets_one_login`
  - `test_a_login_answered_in_this_run_enables_the_fallback_next_cycle`
  - `test_an_unreadable_answered_seed_sends_no_flood_and_is_retried`
- [x] 4.2 In `poll`, pick the login attempts:
  - **recently answered, direct route known:** direct, direct, then flooded (`_step(..., flood=True)`);
  - **not answered recently:** one attempt;
  - **no route known:** one flood.

  Verify these tests pass:
  - `test_a_stale_route_falls_back_to_a_flooded_login_and_uses_the_returned_route`, which asserts the route label `DIRECT h1 → FLOOD → DIRECT ...`
  - `test_an_unreachable_repeater_that_answered_before_gets_two_direct_and_one_flooded_login`
  - `test_no_route_known_sends_one_flooded_login`
- [x] 4.3 Count resends per poll into `PollRecord.retries`. Add the `repeater_retries` and `repeater_login_floods` counters to `as_json`, `step_attempt=` to `repeater_request_sent` (`attempt` would clash with the scheduler outcome's own `attempts` fields), and the `repeater_login_flood_fallback` log line. Verify with these tests:
  - `test_a_poll_that_needed_resends_records_the_count`
  - `test_a_first_time_poll_records_zero_retries`
  - a counters assertion

## 5. Wiring and checks

- [x] 5.1 Confirm `RepeaterPollRepository` still satisfies the collector's `PollSink` protocol, now including `last_answered`. Verify `tests/test_runtime_collect.py` passes and `uv run pyright` (or the project type checker) reports no new errors.
- [x] 5.2 Update the module docstring of `src/sighop/net/collect.py` for retries and the flood fallback. Run the project lint and the full test suite. Verify both are clean.
- [ ] 5.3 After deploy, rerun the read-only prod queries from this proposal over about 24 h and record the result in the change:
  - success rate per repeater;
  - `retries` distribution;
  - share and success rate of polls whose route contains `FLOOD`.
