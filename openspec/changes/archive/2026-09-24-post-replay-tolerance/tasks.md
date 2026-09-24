# Tasks

## 1. Post replay tolerance

- [x] 1.1 Add `POST_CLOCK_SKEW_TOLERANCE_S = 300` to `net/room.py` with a docstring citing the phone/radio clock split (`companion_radio/MyMesh.cpp:1089-1107`, `BaseChatMesh.cpp:577`). Change `_handle_text` to refuse as a replay only when `body.timestamp < member.last_timestamp - POST_CLOCK_SKEW_TOLERANCE_S`. The refusal detail states the gap in seconds and the tolerance (design D5).
- [x] 1.2 Have `_store_post` report whether it found an existing row, drop its unused `retry` parameter, and have `_store_then_acknowledge` log and emit that as `retry` (design D3). Remove the timestamp-equality `retry` from `_handle_text`.
- [x] 1.3 Tests in `tests/test_room_posts.py`: a post 15 s below the recorded timestamp is stored and acknowledged, and the recorded timestamp is not lowered; a post exactly 300 s below is accepted; a retry of that post is acknowledged again, reported as `retry=True`, and stored once. Move the existing `test_a_post_below_the_recorded_timestamp_is_refused_as_a_replay` gap beyond the tolerance (for example 301 s), and assert the detail names the gap.

## 2. Blank-password login keeps the guard

- [x] 2.1 Pass an `empty_password` flag into `_admit` from the blank-password branch of `_handle_login`. On that path keep `existing.last_timestamp`; otherwise keep `max(...)` (design D4). Log `empty_password` on `room_login_admitted`.
- [x] 2.2 Tests in `tests/test_room_login.py`: an existing member's blank-password login with a timestamp above the recorded one leaves the recorded timestamp unchanged, in memory and in the stored row; a password login raises it; `room_login_admitted` carries `empty_password` for both. `test_an_existing_member_with_an_empty_password_is_answered` passes unchanged.
- [x] 2.3 Test the incident end to end in `tests/test_room_posts.py`: a member re-logs in with a blank password stamped 15 s ahead, then posts with a timestamp 9 s older than that login, and the post is stored and acknowledged.

## 3. Documentation

- [x] 3.1 DESIGN.md room-server section: a paragraph on the post clock-skew tolerance and the blank-password match. It cites `MyMesh.cpp:335-376`, `:448` and `companion_radio/MyMesh.cpp:1103`, and says why requests are not covered.

## 4. Verification

- [x] 4.1 Run `uv run --locked ruff format --check`, `uv run --locked ruff check`, `uv run --locked mypy`, and `uv run --locked --env-file .env.dev pytest -q`. All must pass.
- [ ] 4.2 Over the air, with the stock client that failed in `issue-room-post-2`: after a re-login, a post is stored and confirmed without "failed", and `room_login_admitted` shows `empty_password`.
