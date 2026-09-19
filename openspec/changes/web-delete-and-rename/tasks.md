# Tasks

## 1. Name validation, shared by create and rename

- [x] 1.1 Add `parse_entity_name` to `src/sighop/db/repositories.py` in the shape of the existing `parse_channel_name`, refusing empty, over-length and control-character names, stripping surrounding whitespace, and additionally refusing the group-name separator `check_sender_name` refuses (design D4); verify with unit tests in `tests/test_entity_store.py` that each refusal names its reason
- [x] 1.2 Add `parse_room_name` in the same shape, without the separator rule; verify with unit tests in `tests/test_room_repositories.py`
- [x] 1.3 Call `parse_entity_name` from `EntityRepository.store` and `parse_room_name` from `RoomRepository.create`, so creation and import gain the refusals they lack today (entity-store and room-server: "a name is validated wherever it is set"); verify the refusal is identical from the command line and from the panel, and that `./build.sh tests` still passes after fixing any existing test that relied on an unvalidated name

## 2. Repository layer

- [x] 2.1 Add `EntityRepository.rename(public_key, name)` returning the stored-configuration refusal for a duplicate name, and verify in `tests/test_entity_store.py` that the public key, node hash, sealed key material, type and created-at are byte-for-byte unchanged across a rename
- [x] 2.2 Verify in `tests/test_entity_store.py` that a rename succeeds with no sealing secret configured, because it neither opens nor rewrites key material (entity-store: "A rename reads no key material")
- [x] 2.3 Add `RoomRepository.rename(room_id, name)` and `RoomRepository.delete(room_id)`; verify in `tests/test_room_repositories.py` (new, mirroring `tests/test_bot_repositories.py`) that delete removes the room and that its members and messages go with it by cascade, and that the `entity` row survives
- [x] 2.4 Add `BotRepository.delete(bot_id)`; verify in `tests/test_bot_repositories.py` that the bot and every `bot_state` key are gone and the `entity` row survives
- [x] 2.5 Add `ChannelRepository.rename(channel_id, name)` and `WebhookRepository.rename(webhook_id, name)`; verify in `tests/test_channel_repository.py` and `tests/test_webhook_repository.py` that the channel hash, kind and sealed key, and the webhook target, format and triggers, are unchanged, and that a duplicate name is refused by the existing unique constraint's own reason
- [x] 2.6 Verify in `tests/test_db_schema.py` that no migration was needed — the expected schema revision is unchanged and `sighop database current` still agrees (design D8)

## 3. Runtime seam

- [x] 3.1 Add `rename_entity`, `stop_serving_room` and `stop_bot` to the `PanelState` protocol in `src/sighop/web/state.py` as methods beside `reload_channels`, and verify in `tests/test_web_state.py` that `Runtime` still satisfies the protocol structurally and that `web/` still imports nothing from `runtime.py`
- [x] 3.2 Implement `Runtime.rename_entity(public_key, name)`: mutate the matching `EntityStub`'s `name` and `entity_id` in place and replace the frozen `keystore.LocalEntity` in the registry (design D1, D2); verify in `tests/test_runtime.py` that the stub object identity is unchanged, so the direct messenger, channel messenger, room server and bot host all see the new name without being re-handed anything
- [x] 3.3 Verify in `tests/test_adverts.py` that a rename leaves the identity's next scheduled flood, its advert count for this run and any active override untouched, and that the next advert built carries the new name (advert-policy delta)
- [x] 3.4 Refuse a rename that would give two loaded identities the same name, and verify the refusal names the identity that holds it (design D4, advert-policy: "A rename colliding with another loaded identity")
- [x] 3.5 Verify in `tests/test_channels.py` that a channel post made after a rename carries the new sender name, and review the post path for an `await` between `check_sender_name(entity.name)` and `build_group_text_body(..., entity.name, ...)`, reading the name into a local first if one is there (design, Risks)
- [x] 3.6 Implement `Runtime.stop_serving_room(room_id)` and verify in `tests/test_runtime_rooms.py` that the run stops answering login, keep-alive, status and telemetry for that room without a restart
- [x] 3.7 Add `BotHost.remove(worker)` and implement `Runtime.stop_bot(bot_id)` on top of the existing `BotWorker.stop(deadline)`; verify in `tests/test_runtime_bots.py` that a dispatch in flight finishes, no further dispatch starts, and the other bots keep running

## 4. Command line

- [x] 4.1 Add `sighop room delete`, stating the member and message counts first and confirming by typed room name with a flag for no-terminal use; verify in `tests/test_room_cli.py` that it refuses without a terminal and without the flag, refuses on wrong confirmation text, deletes nothing when it refuses, and names the now-unbound identity on success
- [x] 4.2 Add `sighop bot delete` in the same shape, stating what the durable state records and how many keys go; verify the equivalents in `tests/test_bot_cli.py`
- [x] 4.3 Add `sighop keys rename`, whose output states that the name travels in adverts, that neighbours keep the old name until the next one, and names the command that adverts now; verify in `tests/test_keystore.py` or `tests/test_entity_store.py`'s CLI tests
- [x] 4.4 Add `sighop room rename`, `sighop channel rename` and `sighop webhook rename`; verify in `tests/test_room_cli.py`, `tests/test_channel_cli.py` and `tests/test_webhook_cli.py` that each reports the old and new name, changes nothing else, and gives the stored-configuration refusal for a duplicate or empty name
- [x] 4.5 State on the bot command surface that a bot is named by its identity and name the command that renames one; verify there is no `bot rename` subcommand and that the help says so (runtime-cli: "Looking for a bot rename")
- [x] 4.6 Verify every new command refuses with the "requires durable storage" message when no database is configured

## 5. Guarded actions

- [x] 5.1 Add `REMOVE_IDENTITY`, `REMOVE_ROOM` and `REMOVE_BOT` to `src/sighop/web/guarded.py` with their `ACTION_DESCRIPTIONS`, putting only `REMOVE_IDENTITY` in `REAUTHENTICATED_ACTIONS` (design D6); verify in `tests/test_web_guard.py` that each nonce is target-bound, spent on use and expires
- [x] 5.2 Verify in `tests/test_web_guard.py` that a confirmation minted for one room, bot or identity cannot be spent on another, and that each refusal emits its own audit event naming the action, target, outcome and actor

## 6. Panel — deletion

- [x] 6.1 Add the room deletion confirmation view and POST under `/admin/rooms/{room_id}/delete` with a template counting members and messages, stating irreversibility and that the bound identity survives unbound; verify in `tests/test_web_admin.py` that nothing is deleted by a GET, a prefetch or a reload
- [x] 6.2 Add the bot deletion confirmation view and POST under `/admin/bots/{bot_id}/delete`, counting durable-state keys and stating what they record; verify the same no-bare-link property
- [x] 6.3 Add the identity removal confirmation view and POST under `/admin/keys/{entity_id}/remove` requiring the operator's password and the typed identity name, and applying the command line's refusal for a bound identity naming what it serves; verify in `tests/test_web_admin.py` that a wrong password, wrong typed name, missing nonce and bound identity each remove nothing and each record a refusal
- [x] 6.4 Have each deletion call its seam method from section 3 and report the durable change and the live effect separately when the live effect fails; verify with a stub whose seam method returns `False`

## 7. Panel — renaming

- [x] 7.1 Add rename controls and handlers for rooms, channels and webhooks, with no nonce and no password, re-rendering the page with the command line's own reason on refusal; verify in `tests/test_web_admin.py` that a refused channel rename renders no pre-shared key and a refused webhook rename renders no URL beyond scheme and host
- [x] 7.2 Add the identity rename control stating that the name travels in adverts and that neighbours keep the old name until the next one; verify the page carries no password field
- [x] 7.3 Add the advert choice of none, zero-hop or flood to the identity rename form with none selected by default, minting one nonce per kind and spending the chosen one (design D5); verify in `tests/test_web_adverts.py` that the flood offer states the repeat cost and the schedule move exactly as the standalone confirmation does
- [x] 7.4 Apply the rename before attempting the advert and report the two outcomes separately; verify that a closed transmit gate leaves the rename applied and states the advert's refusal reason, and that a refused rename submits no advert
- [x] 7.5 Offer no advert and state that this run does not hold the identity when it is not loaded; verify in `tests/test_web_adverts.py`
- [x] 7.6 Show on each bot that it is named by its bound identity and link to that identity's rename, offering no rename control of its own; verify in `tests/test_web_admin.py`
- [x] 7.7 Record every rename in the request's own event with the old and the new name; verify in `tests/test_web_admin.py`

## 8. Withdrawing the stated exclusion

- [x] 8.1 Replace the panel's "removal is not offered here, use the terminal" text on identity administration with the removal action itself, stating that it is irreversible and that disabling is the reversible action offered alongside it; verify in `tests/test_web_admin.py` that the four remaining exclusions — migrations, secret generation, account management and channel pre-shared keys — are still stated where they would be looked for

## 9. Parity and whole-system checks

- [x] 9.1 Extend `tests/test_web_write_parity.py` so each new delete and rename is asserted indistinguishable between the command line and the panel, as the existing writes are
- [x] 9.2 Extend `tests/test_web_exercise.py` with the round trip: create an identity, bind a room, delete the room, rename the identity, remove the identity — verifying the identity is unbound and reusable after the room goes and that removal is refused while it is bound
- [x] 9.3 Record the design decisions from `design.md` in the DESIGN.md decisions table, at minimum D1 (live rename mutates the shared stub), D2 (`entity_id` moves with the name) and D6 (the two confirmation tiers)
- [x] 9.4 Run `./build.sh` and verify the `format`, `lint`, `types` and `tests` gates all pass
- [x] 9.5 Run `openspec validate web-delete-and-rename --strict` and verify every scenario added here has a test that exercises it
