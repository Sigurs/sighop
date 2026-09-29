# Tasks

## 1. Store the paths

- [x] 1.1 Add `paths: tuple[bytes, ...] | None = None` and `path_hash_size: int | None = None` to `ChannelMessageRecord` (`src/sighop/net/channels.py`) and set them from the reception's packet on the inbound record (`(packet.path,)`, `packet.hash_size`); verify with a channels unit test that a received 3-hop message's record carries exactly that one path and hash size, and a post's record carries `None`
- [x] 1.2 Remember inbound records in a bounded received registry by content key (design D2) and, in `_observe`, append a duplicate copy's path (cap `MAX_PATHS = 32`) and re-record; verify unit tests: a duplicate over another route yields two paths in arrival order with hop count/SNR/RSSI unchanged, a zero-hop duplicate appends `b""`, the 33rd copy is ignored, a non-duplicate observer call adds nothing, a copy after TTL expiry adds nothing, and own-post repeat counting is unchanged
- [x] 1.3 Add nullable `paths` (`ARRAY(LargeBinary)`) and `path_hash_size` (`SmallInteger`) to `ChannelMessage` in `src/sighop/db/models.py` and Alembic migration `alembic/versions/0015_channel_message_paths.py` (upgrade adds, downgrade drops); verify `alembic upgrade head`, `alembic downgrade -1`, `alembic upgrade head` succeed against the dev database
- [x] 1.4 Write and read both columns in `ChannelMessageRepository` (`src/sighop/db/repositories.py`: `_channel_message_values` and `_channel_message`); verify a DB test that an inbound message recorded, then re-recorded with a second path, reads back as one row with both paths in order, and a row with `NULL` columns reads back as `None`

## 2. Show and copy them

- [x] 2.1 Add `_path_lines(paths, hash_size)` in `src/sighop/web/routes/chat.py` per design D3; verify unit tests for `a3f1->28c0->9e4b` (2-byte, 3 hops), `0a->ff` (1-byte), `direct` for `b""`, a non-dividing element skipped, and `()` for `None`
- [x] 2.2 Add `"path_copy"` to `_channel_message_view` (`\n`-joined lines, `None` for outbound, `NULL` paths or all-`direct`) and pass the lines into `_channel_state` so the received explanation adds copies heard and the paths (design D4); verify a test that a 2-hop message heard twice has hover text stating 2 hops, 2 copies and both paths, with figures still only the hop count
- [x] 2.3 Add `copy_path(text)` macro to `src/sighop/web/templates/_display.html` (design D5), render it in the state cell of `src/sighop/web/templates/chat/_channel_messages.html` when `message.path_copy` is set, add `white-space: pre` for `.path-copy .key-full` in the panel stylesheet, and update `display.js`'s header comment; verify a route test that `/chat/channel/<id>/messages` includes `data-copy` holding `a3->28->9e`, `direct`, `a3->5d` separated by newlines (escaped as the template renders them) for a multi-copy message, and no `copy paths` control for a post, a direct-only message or a path-less row

## 3. Verify

- [x] 3.1 Run lint and the full test suite (`uv run --locked --env-file .env.dev` ruff/pyright/pytest as the project does) and confirm all pass
- [ ] 3.2 Open a channel in the running panel, receive (or replay) a message heard over at least two routes, click the copy control over plain HTTP and confirm the pasted text is one `->`-joined path per line in arrival order, and that a further copy appears after the 3 s refresh
