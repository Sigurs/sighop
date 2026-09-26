# Tasks

## 1. Before anything is deleted

- [x] 1.1 Extract the real names and key prefixes to a scratchpad file (never the repo): every advert name and public key from `git show HEAD:captures/*.jsonl`, plus the peer/burned key prefixes named in `DESIGN.md`, `CORPUS.md` and `test_foreign_decrypt.py`, plus the value lines of `.env.prod` from history. Also inspect the `seed_hex` constant added in `0de8f43` and say whether it is a real key. Verify the file lists at least the 11 advert names and 12+ distinct keys the old `CORPUS.md` reports.
- [x] 1.2 Record, per test module that reads the corpus, which capture it uses and which recorded facts it pins (a name, a count, a frame with a property). Verify by running `uv run pytest -q` once on the untouched tree and saving the pass list, so later failures are attributable to this change.
- [x] 1.3 Confirm `SIGHOP_TEST_DATABASE_URL` or `DATABASE_URL` is available (`build.sh` needs one) and that `uv run --locked ruff check`, `ruff format --check` and `mypy` are green before starting.

## 2. Generator

- [x] 2.1 Write the cast in `tests/protocol/synthetic.py`: `SEED`, `GENERATOR_VERSION`, seeded identities via `LocalIdentity.from_seed`, the `syn-`-prefixed names, the Public and second-channel message lists, and the second channel's key from `channel_key_from_hashtag`. Verify with a unit test that the same seed gives the same identities and a different seed gives different ones.
- [x] 2.2 Implement the record emitters over the `src/sighop` encoders (`rx_frame`, `tx_frame`, `unparsed`, `capture_meta` with synthetic marker, version and seed), one seeded `random.Random` per file for time and signal. Verify each emitted file replays through `CaptureReplay` with no unreadable line.
- [x] 2.3 Generate `synthetic-ambient.jsonl`: adverts of every type/flag form (repeater `0x92`, chat `0x81`/`0x91`, room `0x93`/`0x83`), flood and direct routing, hops 0-5, hash sizes 1-3, TRACE of two lengths, PATH, discovery requests (6 and 10 bytes) and 38-byte responses from repeaters only, one `TRANSPORT_FLOOD`, and the multi-hop multi-byte-hash adverts. Verify every advert signature verifies and that reading a multi-byte-hash advert as 1-byte hashes yields corrupt flags or a truncated name.
- [x] 2.4 Generate `synthetic-channels.jsonl`: Public-channel group text under `PUBLIC_CHANNEL_KEY`, a second channel under its synthetic key, a few group-data frames. Verify the Public frames carry hash `0x11` and decrypt, the second channel's frames carry a different hash and do not.
- [x] 2.5 Generate `synthetic-exchange.jsonl`: DMs both ways with both ACK forms, one identical DM retransmission after >30 min, room login / post / refusal / push / ack, a REQ and RESPONSE, and sighop's own adverts as `tx_frame`. Verify the DMs decrypt with the generator's keys and every ACK matches `ack_checksum_for` of the message it answers.
- [x] 2.6 Implement the single repeat emitter that takes a `reason`, and place the declared repeats: ordinary flood repeats, exactly one late echo over a different path >60 s, exactly one retransmission (2.5). Verify repeats are ≤12% of receptions, no packet has more than 3 copies, and no other content repeats.
- [x] 2.7 Add the generator entry point (`uv run python -m tests.protocol.generate_corpus`) that writes `tests/corpus/*.jsonl` and prints the manifest (counts per file, payload type, route type, hop count, hash size, advert form). Verify running it twice produces identical bytes.

## 3. Guard and drift tests

- [x] 3.1 Add the drift test: regenerate in memory and compare with `tests/corpus/*.jsonl`, failing with the file and first differing line. Verify by hand-editing one committed byte and seeing it fail, then reverting.
- [x] 3.2 Add the no-real-data guard: every advert key and name, and every decrypted sender and text, is in the cast; and a tracked-file scan finds no private-key material outside test code that builds keys in memory. Verify by temporarily adding a non-cast name to a scratch copy and seeing the guard name the file, record and value.
- [x] 3.3 Add the repetition-budget tests (ceiling, max copies, the one late echo, the one retransmission, nothing else repeats). Verify each fails against a copy of the corpus with a duplicated frame.

## 4. Switch the harness

- [x] 4.1 In `tests/protocol/corpus.py` replace `CAPTURES_DIR` with `CORPUS_DIR` (no alias), replace `CAPTURE_FILES` / `SIDECAR_PROVENANCE_FILES` / `FIRST_TRANSMIT_FILE` and the index constants with the three role names, and re-key `EXPECTED_*` from the generator's manifest (typed in from a reviewed run). Verify `uv run pytest tests/protocol/test_corpus.py` passes after the corpus files exist.
- [x] 4.2 Update `test_corpus.py`: distribution constants, transport-frame codes, CONTROL forms, advert count and names; provenance test now requires a synthetic `capture_meta` on every file and no sidecar. Verify the module passes and that a deliberately mis-shifted payload-type mask fails the distribution test.
- [x] 4.3 Rewrite `test_foreign_decrypt.py` and `test_channel_foreign_decrypt.py` as the self-consistency tests of the `mesh-crypto` delta (DMs, ACK forms, Public `0x11`, second channel, key-slice negative, `0x17` hash negative, closed-channel negative), renamed so nothing claims a foreign implementation. Verify they pass and that each negative fails when its guard is removed.
- [x] 4.3a Delete `tests/fixtures/burned-first-transmit.json` (and the `tests/fixtures/` directory if empty). Verify `git ls-files | xargs grep -l -E '"private_key_hex": "[0-9a-f]{64,}'` finds nothing.
- [x] 4.4 Update `tests/protocol/golden.py` and `generate_golden.py` for the new files, regenerate `corpus_golden.txt`, and review the diff. Verify the golden test passes and the no-plaintext test still passes.

## 5. Move the consumers

- [x] 5.1 Update the replay-shaped consumers (`test_bus`, `test_durable_paths`, `test_dedup`, `test_paths`, `test_rx`, `test_replay`, `test_packet_log`, `test_room_exercise`, `test_corpus_pipeline`, `test_web_exercise`, `test_runtime_persistence`) to `CORPUS_DIR` and the role names. Verify each module passes; for a failure that pins a recorded fact, fix the generator or re-derive the expectation and say why in the commit, never loosen the assertion.
- [x] 5.2 Update the runtime-shaped consumers (`test_runtime`, `test_first_transmit`, `test_contacts`, `test_web_server`, `test_runtime_collect`, `test_runtime_rooms`, `test_runtime_bots`, `test_runtime_webhooks`, `test_runtime_channels`, `test_channels`) the same way. Verify each module passes.
- [x] 5.3 Re-derive literals copied from the recording: the real repeater name and the appdata bytes in `test_payloads.py`, the names in `test_adverts.py` and any hex lifted from a capture in `test_render.py`, `test_keystore.py`, `test_durable_contacts.py`, `test_web_*`, with cast values (build the bytes with the encoders instead of pasting hex where practical). Verify with `grep` of the 1.1 list that no test file contains a real name or key prefix.
- [x] 5.4 Update `test_corpus_pipeline.py`'s recorded totals (`EXPECTED_PUBLIC_DECRYPTED`, `EXPECTED_UNKNOWN_CHANNEL`, received / transmitted counts and the `ModemUnparsed` note) to the manifest. Verify the module passes.

## 6. Build, ignore, delete

- [x] 6.1 Point `build.sh`'s replay gate and its bind mount at `tests/corpus/` and update `test_the_replay_gate_compares_the_image_with_the_host_byte_for_byte`. Verify `uv run pytest tests/test_deployment_files.py` passes and `shellcheck build.sh` (if installed) is clean; if Docker is available, verify `./build.sh replay` reports the three files identical on host and image.
- [x] 6.2 Add `captures/` to `.gitignore` with a comment that live recordings are never committed and the synthetic corpus lives in `tests/corpus/`. Verify `git check-ignore captures/x.jsonl` matches.
- [x] 6.3 `git rm` everything under `captures/` (13 `.jsonl`, 8 `.log`, 2 `.meta.json`). Verify `git ls-files captures` is empty and the full suite still passes.

## 7. Documents

- [x] 7.1 Rewrite `tests/protocol/CORPUS.md` around the generator, cast, file roles, budget and what the corpus does not prove (interoperability withdrawn). Verify it names every requirement scenario in `synthetic-corpus` and `mesh-crypto` that says the documentation must say something.
- [x] 7.2 Move the recorded flood-repeat, late-echo and retransmit measurements from `CORPUS.md` into `DESIGN.md` as recorded history (they identify nobody), and record there that the first-transmit interoperability exchange existed and passed once and was withdrawn. Verify `DESIGN.md`'s milestone text no longer links to `captures/…` paths that do not exist.
- [x] 7.3 Replace real key prefixes and node names in `DESIGN.md`, `CORPUS.md` and the archived change documents found by 1.1 with `[redacted]` or a cast name. Verify with `grep -rnF -f <1.1 list>` over the tracked tree that there are no hits.
- [x] 7.4 Update `openspec/specs/protocol-corpus/spec.md`'s status blockquote (it still describes recorded files and counts) when the change is archived; note it in the change so the archive step does not skip it. Verify `openspec validate synthetic-corpus --strict` passes.

## 8. Final gates

- [x] 8.1 Run `uv run --locked ruff format --check`, `uv run --locked ruff check` and `uv run --locked mypy`; all clean.
- [x] 8.2 Run the full suite (`uv run --locked pytest -q`, with the database URL from 1.3); all green, and the count of passing tests is not lower than the 1.2 baseline except for tests intentionally removed with the recording.
- [x] 8.3 Final scan: the 1.1 list against the working tree finds nothing, no tracked file holds private-key material, and `git status` shows no file under `captures/`. Report any hit rather than fixing it silently.
- [x] 8.4 Commit the change on the working branch so the purge in section 9 rewrites the finished tree. Verify `git status` is clean and `git log -1` is the change.

## 9. Purge history (destructive; stop at 9.6 for the operator)

- [x] 9.1 Inventory: list every path ever committed on every ref (`git log --all --name-only --pretty=format: | sort -u`) and confirm the purge set is `captures/`, the burned fixture and `.env.prod`, plus anything the listing adds. Verify the inventory is written to the scratchpad and shown to the operator.
- [x] 9.2 Make a full backup outside the repo (`git bundle create <outside>/sighop-pre-purge.bundle --all`) and verify `git bundle verify` passes. Tell the operator the bundle holds the real data.
- [x] 9.3 Rewrite on a fresh clone: `git clone --no-local . <scratch>/purge`, then in it `uvx git-filter-repo --invert-paths --path captures/ --path tests/fixtures/burned-first-transmit.json --path .env.prod --replace-text <scratch>/redactions.txt`. Verify it exits zero and both branches exist.
- [x] 9.4 Verify the clone: no removed path in `git log --all --name-only`; no 1.1 string in `git grep -F -f <list> $(git rev-list --all)`; tip tree equals the pre-purge tip apart from redactions; `uv run --locked pytest -q` passes in the clone. Report any hit rather than fixing it silently.
- [x] 9.5 Show the operator the verification results and the list of what will change (all hashes, both branches).
- [x] 9.6 STOP: proceed only on the operator's explicit confirmation. Then replace the repository with the rewritten clone (keeping the original directory renamed until 9.7 passes), and verify `git status`, `git log --oneline | wc -l` and the branch list match the clone.
- [x] 9.7 `git reflog expire --expire=now --all && git gc --prune=now --aggressive`; verify `git count-objects -v` shows no unreachable garbage and that the 9.4 scans still find nothing.
- [x] 9.8 Report the follow-ups git cannot do (D12): rotate `SIGHOP_SECRET_KEY` and the database credentials from `.env.prod`; clear and reindex the CCE index (`cce index` on the host); decide about `~/.claude/projects/` transcripts and tool-result files; re-clone or purge any other clone, fork or hosting copy; delete the backup bundle and the redaction list when satisfied. Verify the operator has been shown each item.
