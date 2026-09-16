## 1. DESIGN.md contract

- [x] 1.1 Update DESIGN.md per design D12: §3 channels as station state; §5 channel paragraph gains the Public key constant, "hash over real length (`0x11` vs `0x17`)" and the HMAC key-padding note; §6 tables thirteen (`channel`) and fourteen (`channel_message`) with sealing-by-kind, unencrypted-content and downgrade statements; new §7 "Channels" section (receive trial, plain-only, claimed names, composition, 160-byte limit, flood/class 2/no retry, repeat registry, refresh); §7 Bots paragraph rewritten per D11; §8 chat item 4 and "not exposed" list gain PSK reveal; §9 channel events; §11 layout gains `net/channels.py` and web additions. Verify by reading each touched section against `specs/channel-*/spec.md` and grepping DESIGN.md for "channel key store exists" / "nothing in `net/` decrypts" (no hits outside milestone history)

## 2. Protocol

- [x] 2.1 Add `PUBLIC_CHANNEL_KEY` (`ChannelKey` from `izOH6cXN6mrJ5e26oRXNcg==`, not brute-forceable-flagged but documented as public) to `protocol/crypto.py` and export it; verify a test asserts its `channel_hash == 0x11` and that `sha256(key.secret)[0] == 0x17`
- [x] 2.2 Add `build_group_text_body(timestamp, sender_name, body)` to `protocol/payloads.py` (plain, attempt 0, `name: body`, `EncodeError` on `": "` in name); verify tests for the round-trip `dev-companion` / `hello: world`, the separator refusal, and `tests/protocol/test_import_boundary.py` still passing
- [x] 2.3 Add `tests/protocol/test_channel_foreign_decrypt.py`: iterate every capture in `captures/*.jsonl`, dedupe `GRP_TXT` payloads, assert all 85 on `0x11` MAC-verify and parse as plain group text with a claimed sender under `PUBLIC_CHANNEL_KEY`, none of the 108 on `0x81` opens, and each decrypted body rebuilt with `build_group_text_body` equals the decrypted bytes minus zero padding; name the files and counts in the module docstring; verify with `uv run pytest tests/protocol/test_channel_foreign_decrypt.py`

## 3. Reception observers

- [x] 3.1 Change `IngressPipeline.observer` to `observers: list[RxObserver]` with per-observer isolation and `rx_observer_error` reporting; make `Runtime.watch_traffic(on_reception=…)` append; verify new `tests/test_bus.py` tests for two observers on a duplicate and a raising first observer, and `tests/test_web_feed.py` passing unchanged

## 4. Channel messaging core (`net/channels.py`)

- [x] 4.1 Add `LoadedChannel`, immutable `ChannelSet` (`by_hash`, `by_id`, atomic replace) and the typed events (`ChannelMessageReceived`, `ChannelUnknown`, `ChannelUndecryptable`, `ChannelUnsupportedText`, `ChannelPostSubmitted`, `ChannelPostRefused`, `ChannelPostResolved`, `ChannelRepeatHeard`) and `ChannelMessageRecord` + `ChannelMessageSink` protocol (never awaits, never raises); verify unit tests for `by_hash` with two channels sharing a hash
- [x] 4.2 Implement `ChannelMessenger.handle` as a bus subscriber for `GRP_TXT`: trial all channels under the hash, `mac_then_decrypt`, `parse_group_text_body`, require `TextType.PLAIN`, emit events, offer an inbound record to every sink (design D3); verify `tests/test_channels.py` tests for: corpus Public frame decrypts; shared-hash second channel wins; unknown hash; MAC failure; non-plain type counted and not recorded; `GRP_DATA` ignored
- [x] 4.3 Implement `ChannelMessenger.post(channel_id, identity, text)` with refusals in D5 order (channel loaded, identity loaded, `": "` in name, 160-byte limit, gate closed) raising typed errors, station-wide `max(now, last+1)` timestamp, one flood class-2 submission, record at submission and on resolution (`awaiting` → `transmitted` | `not_transmitted`); verify tests: each refusal records nothing and submits nothing; two identical posts in one second have distinct timestamps; submitted packet decrypts under the channel key to `dev-companion: hello`; a budget drop resolves `not_transmitted` with the scheduler's reason and is not retried
- [x] 4.4 Implement the repeat registry (256 entries, 1 h, keyed by dedup key, filled on transmitted outcome) with an observer that counts every copy including duplicates and the bus handler skipping keys it holds (design D6); verify tests: three copies (one non-duplicate, two duplicates) → `repeats_heard == 3`, no inbound record, no double count of the first copy; a received message claiming a local identity's name that is not in the registry is recorded as inbound; registry evicts past capacity and age
- [x] 4.5 Add `monitor/render.py` lines for decrypted messages (claimed name with the existing unverified marking), posts and outcomes (with `actor` when from the web), repeats, unknown/undecryptable counts; verify `tests/test_render.py` string tests including a claimed name never rendered with the verified marking

## 5. Persistence

- [x] 5.1 Add models `Channel` and `ChannelMessage` and migration `alembic/versions/0007_channels.py` per design D2/D7 (checks on kind/required columns, FK cascade, unique `(channel_id, ref)`, handled-time index, Public seed row, docstring stating unencrypted text and downgrade loss); verify a `tests/test_channel_schema.py` upgrade/downgrade test against the test database fixture showing exactly one `public` row after upgrade and both tables gone after downgrade, and `sighop db current` expecting `0007`
- [x] 5.2 Add `ChannelRepository` (`add_public`, `add_hashtag`, `add_psk` sealing with value seal v2, `list`, `get`, `remove` returning deleted message count, `message_count`, `load_keys` returning loaded channels plus names skipped as unsealable) with duplicate-key and duplicate-name refusals and base64/length validation; verify `tests/test_channel_repository.py`: wrong length names length, bad base64, same key under another name refused naming the existing channel, same hash different key accepted, sealed column contains no key bytes, unsealable row skipped by name, removal cascades and reports the count
- [x] 5.3 Add `ChannelMessageRepository` (`upsert` on `(channel_id, ref)`, `recent(channel_id, limit)` newest-first by `handled_at`, startup rewrite of `awaiting` → `unknown`) and a refusing `WriteBehind` lane in `db/persistence.py` with counted refusals; verify repository tests for in-place update, ordering against a past wire timestamp, and the startup rewrite, plus a persistence test that a full lane refuses and counts

## 6. Runtime wiring

- [x] 6.1 Wire `ChannelSet` loading at startup (after persistence restore), `ChannelMessenger` subscription, durable sink, observer registration, startup report (loaded names/hashes, skipped names, "no database" line), status-line counters, `reload_channels()` and a 60 s refresh task that keeps the last set on failure with `channel_config_read_failed`; verify `tests/test_runtime_channels.py`: no-database run reports no channels; a channel added to the repository appears after `reload_channels()`; a failing loader keeps the previous set
- [x] 6.2 Extend `tests/test_corpus_pipeline.py` with a replay that loads Public: considered, duplicate, contact and path counts identical to the no-channel replay, 85 decrypted Public messages, and zero submissions; verify with `uv run pytest tests/test_corpus_pipeline.py`
- [x] 6.3 Confirm replay recording follows `writes_enabled` (no channel rows without `--persist-replay`); verify a runtime test with a replay source and a database fixture

## 7. CLI

- [x] 7.1 Add the `sighop channel` noun per design D9 (`add --public|--hashtag|--psk-stdin|--generate`, `list`, `show`, `remove [--delete-history]`, `key`, `history [--limit]`), guessable-key statements, "applies to a running run within 60 s" line, no-database refusal; verify `tests/test_channel_cli.py` covering each spec scenario in `specs/runtime-cli/spec.md`, including that `--psk` does not exist and `remove` refuses non-interactively without the flag

## 8. Web

- [x] 8.1 Add `ChannelLog` (bounded per channel, unread counts) in `web/`, register it as a messenger sink, and expose channels, `post` and `reload_channels` through `web/state.py` protocols; verify unit tests for offer-by-ref update-in-place and the bound
- [x] 8.2 Chat: channel list on the index, `GET /chat/channel/{id}`, `GET …/messages` poll, `POST /chat/channel/{id}` with identity field; claimed-name presentation separate from `_identity.html`, the standing "names are not authenticated" sentence, repeats wording, Public audience line, refusal re-render with preserved text, degraded-database line; remove `channels_absent` and its template blocks; verify `tests/test_web_chat.py` tests for every scenario in `specs/web-chat/spec.md`, including a claimed name equal to a verified contact rendering without the verified class or a contact link, and opening a channel submitting nothing
- [x] 8.3 Admin: `/admin/channels` list (name, kind, hash, guessable marking, message count), add hashtag / PSK (password field, never re-filled) / re-add Public, remove via confirm-and-nonce stating the count, `reload_channels()` after each change, "not offered here" entry naming `sighop channel key`, no-database page; verify `tests/test_web_admin.py` tests including page source containing no PSK in base64 or hex after an add and after a refused add, and a route-table walk still proving every new route requires a session
- [x] 8.4 Emit run output for a post from the web naming the account; verify a web route test capturing run output

## 9. Bots docstring

- [x] 9.1 Rewrite the `on_channel_message` explanation in `src/sighop/bots/base.py` (module docstring and `Bot` docstring) per design D11, no behaviour change; verify `uv run pytest tests/test_bot_runtime.py` passes and grep finds no "nothing decrypts `GRP_TXT`" in `src/`

## 10. Checks and live exercise

- [x] 10.1 Run `uv run ruff check src tests`, `uv run mypy src`, `uv run pytest` and `./build.sh` and verify all gates pass (including `replay` identical on host and image)
- [x] 10.2 Live exercise against the V4 and the stock test peer on a hashtag channel `#dev-sighop` (never Public): peer posts → message appears in the browser with its claimed name marked unverified; `dev-` identity posts from the browser → peer shows `dev-<name>: <text>`; repeats heard shown if a repeater is in range; restart → both messages in history. Record findings under DESIGN.md §12 and verify the session's airtime stayed under the ceiling in the run's status line
