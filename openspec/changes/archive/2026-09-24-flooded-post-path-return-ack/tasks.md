# Tasks

## 1. Acknowledgement routing

- [x] 1.1 Extract the flooded path-return packet construction from `_send_login_reply` (`net/room.py`) into a helper that takes the inbound record, the member, and either a bundled `RESPONSE` or a bundled ACK. Login keeps its behaviour: `tests/test_room_login.py` passes unchanged.
- [x] 1.2 Change `_acknowledge_post` to send a flooded path return bundling `Acknowledgement(checksum)` when the post arrived `FLOOD`/`TRANSPORT_FLOOD`, and a bare ACK on `_route_to(member)` otherwise, at `PriorityClass.ACK` with origin `room_post_ack` (design D1–D3). Verify with a test where a flooded post from a member with a known zero-hop route yields a `PATH` packet, routed FLOOD, that the member's secret decrypts to a body with the post's path, `extra_type=ACK`, and the checksum `ack_checksum_for` computes.
- [x] 1.3 Add a test for a zero-hop flooded post: the path return carries an empty path with the inbound hash size, and no DIRECT ACK is submitted.
- [x] 1.4 Add a test for a retried flooded post: the second attempt gets its own path return, with the checksum for attempt 1, and only one row is stored.
- [x] 1.5 Keep direct-post behaviour: the existing `tests/test_room_posts.py` cases (direct posts with a known route) still get a bare ACK on that route. Add a test that a direct post with no known route gets a bare flooded ACK.

## 2. Reporting the route

- [x] 2.1 Have `_acknowledge_post` return the route label with the sent flag (`"PATH_RETURN"` or `Route.label`). Log it as `ack_route` in `room_post_stored`, and add `ack_route: str | None = None` to `PostStored`. Verify with a `RecordingLogger` assertion in `tests/test_room_posts.py`.
- [x] 2.2 Append `ack=<route>` to `render_post_stored` when acknowledged. Verify with a case in `tests/test_room_render.py`.

## 3. Documentation

- [x] 3.1 Add a note to DESIGN.md's room-server section, next to "One thing sighop does that the firmware cannot": a flooded post is ACKed by a path return, where the firmware floods a bare ACK, and why. Verify that the paragraph cites `MyMesh.cpp:494-497` and `BaseChatMesh.cpp:328-340`.

## 4. Verification

- [x] 4.1 Run `uv run --locked ruff format --check`, `uv run --locked ruff check`, `uv run --locked mypy`, and `uv run --locked --env-file .env.dev pytest -q`. All must pass.
- [ ] 4.2 Over the air, with a stock client posting flooded to a sighop room: the first post confirms without retry, `room_post_stored` shows `ack_route=PATH_RETURN`, a `path_body_learned` for the member follows, and the client's next post arrives DIRECT and is ACKed on a DIRECT route.
