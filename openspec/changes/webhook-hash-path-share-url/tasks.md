## 1. Contact prefix lookup

- [x] 1.1 Add `ContactStore.by_prefix(prefix: bytes) -> frozenset[Contact]` using the first-byte index and `startswith` (empty prefix → empty set) (design D3); verify with new tests in `tests/test_contacts.py` covering 1-, 2- and 3-byte prefixes, a colliding first byte, and no match

## 2. Event model

- [x] 2.1 Add frozen `PathHop(hash, name, matches, key_prefix)` with a `label` property (`name` / key prefix / `<unknown>` / `<ambiguous>`) and a `HopLookup` protocol in `src/sighop/webhooks/events.py` (design D1, D2); verify with unit tests in `tests/test_webhooks_render.py` for each label case
- [x] 2.2 Add `hash_size: int` and `path: tuple[PathHop, ...]` to `WebhookEvent`, plus a `sized_hash` property (design D1); verify with tests for 1-, 2- and 3-byte hashes of the same key
- [x] 2.3 Extend `event_from_observation` with an optional `contacts: HopLookup | None` that sets `hash_size = record.hash_size or 1` and resolves `record.packet.hops`, excluding the advertising contact (design D2, D4); verify with tests: 2-hop path with one known named hop and one unknown, a colliding 1-byte hop → `<ambiguous>`, a single unnamed match → key prefix, a flood advert with 0 hops and hash size 2 keeps size 2, no lookup → all hops `<unknown>`
- [x] 2.4 Extend `advert_record` in `tests/botfixtures.py` with optional `hash_size` and `path` parameters (defaults preserving current behaviour); verify existing bot and webhook tests still pass with `uv run pytest tests/test_webhooks_render.py tests/test_webhooks_dispatcher.py tests/test_greeter.py`
- [x] 2.5 Update `sample_event` to `hash_size=2`, `hop_count=2` and a path of `c3d4` named `dev-hop` (1 match) then `e5f6` (0 matches) (design D7); verify with an updated `test_a_sample_event_is_marked_as_a_test`
- [ ] 2.6 Give `sample_event` a sample `Position` (design D7) so a test message carries a location link; verify with an updated `test_a_sample_event_is_marked_as_a_test` asserting the sample position

## 3. Rendering

- [x] 3.1 JSON: add `node.hash`, `node.hash_size` and `reception.path` (`hash`, `name`, `matches`) while leaving `node_hash` as the first byte; update the module docstring's schema rule (design D6); verify by updating the golden `test_the_json_body_is_the_documented_schema_1_event`, adding a test that a 2-byte event keeps `node_hash` at two hex digits, and a zero-hop test with `reception.path == []`
- [x] 3.2 Discord: `Node hash` shows `sized_hash`; `Public key` becomes a non-inline field with the full 64-hex key in inline code, and the unnamed-node description uses `sized_hash` and the full key (drop `KEY_PREFIX_LENGTH`); add non-inline `Path` field (hashes in inline code, names via `escape_discord`, fixed labels unescaped, `heard directly` when empty, cut at 1024 chars on a hop boundary ending `…`) (design D5); verify by updating the golden field test and `test_an_unnamed_node_is_identified_by_hash_and_key_prefix` (full key, no `…`), and adding tests for a markdown contact name in the path shown literally, `@everyone` in a path name notifying nobody, an empty path, and a path that would exceed 1024 chars
- [x] 3.3 Verify a sample event renders a `[test]` Discord message with the sample path, and JSON `test: true` with the sample path, in `test_a_sample_is_marked_as_a_test_in_both_formats`
- [ ] 3.4 Add `MAPS_URL`/`COORDINATE_DECIMALS` and a `maps_url(position) -> str` helper in `src/sighop/webhooks/render.py` building `https://www.google.com/maps/search/?api=1&query={lat:.6f},{lon:.6f}` (design D8); verify with unit tests for a positive pair, a negative pair, and a whole-degree value keeping six decimals
- [ ] 3.5 Discord: drop the `SNR` field and add an inline `Location` field after `Hops`, value `[{lat}, {lon}](<maps url>)` as a masked link or `not advertised` when `event.position is None`, coordinates unescaped because sighop formats them (design D5, D8); verify by updating the golden field test (field order `Name`, `Type`, `Node hash`, `Hops`, `Location`, `Public key`, `Path`; no `SNR` field), and adding tests for a located node's masked link, an unlocated node's `not advertised`, and a negative-coordinate node
- [ ] 3.6 JSON: add `node.position.map_url` alongside `latitude`/`longitude`, leaving `position: null` and `reception.snr_db` as they are (design D6); verify by updating the golden `test_the_json_body_is_the_documented_schema_1_event` and adding a test that an unlocated event has `node.position` null and still carries `snr_db`
- [ ] 3.7 Update the `render.py` module docstring to say the Discord message carries the location link and no SNR; verify by reading it against `specs/webhooks/spec.md`

## 4. Wiring

- [x] 4.1 Add `contacts: HopLookup | None = None` to `WebhookDispatcher.__init__` and pass it to `event_from_observation` in `on_observation`; verify with a test in `tests/test_webhooks_dispatcher.py` where a store holding a named repeater hears a companion through that repeater's hop and the delivered JSON body's `reception.path[0].name` is the repeater's name
- [x] 4.2 Pass `contacts=self.contacts` in `Runtime._build_webhooks` (`src/sighop/runtime.py`); verify the runtime/webhook wiring tests still pass and `self.contacts` is constructed before `_build_webhooks` is called

## 5. Documentation and checks

- [x] 5.1 Update DESIGN.md §Webhooks: list the new JSON fields, the Discord full public key and path fields, and change the schema rule to "fields may be added within a schema version; never removed or repurposed"; verify by reading the section against `specs/webhooks/spec.md`
- [ ] 5.2 Extend the same DESIGN.md §Webhooks pass with `node.position.map_url`, the Discord `Location` field and the removed Discord `SNR` field (noting SNR stays in `json`); verify by reading the section against `specs/webhooks/spec.md`
- [ ] 5.3 Run `uv run ruff check src tests`, `uv run mypy src` and `uv run pytest` and verify all pass
- [ ] 5.4 Send `sighop webhook test <name> --trigger new_repeater` to a real Discord webhook and verify the message shows the 2-byte sample hash, the `dev-hop` → `<unknown>` path, the full 64-hex public key, a `Location` link that opens the sample coordinates in Google Maps, and no `SNR` field
