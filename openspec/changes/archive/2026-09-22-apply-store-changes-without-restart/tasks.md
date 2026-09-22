# Tasks

Ordered so that each group leaves the run releasable. Group 1 is a standalone bug fix that
everything after it depends on; groups 3–6 land one lifecycle at a time.

## 1. Fix the aliasing that live adoption depends on (design D5)

- [x] 1.1 Change `runtime.py:508` (`stop_serving_room`) and `runtime.py:977` (`_serve_room`'s release) to mutate `path_bodies.entities` in place with `.append()` / `.remove()` instead of rebinding it, so the alias to `adverts.stubs` established at `runtime.py:376` survives; verify with a new test in `tests/test_runtime_rooms.py` asserting `runtime.path_bodies.entities is runtime.adverts.stubs` after a room is served and after it is stopped
- [x] 1.2 Add a regression test in `tests/test_runtime_rooms.py` that an identity appended to `adverts.stubs` after a room has been served and stopped is visible to the path-body reader, which fails before 1.1

## 2. Non-fatal admission and removal primitives (design D4, D6)

- [x] 2.1 Add `AdvertScheduler.admission_refusal(identity, name) -> str | None` returning the reason a live adoption would be refused (node-hash collision, name clash) without raising, leaving `add_identity`'s raising form untouched; verify with unit tests in `tests/test_adverts.py` covering a collision, a name clash and a clean admission
- [x] 2.2 Add `AdvertScheduler.remove(public_key) -> EntityStub | None` removing the stub from `stubs` **in place** for the reason stated at `adverts.py:313`, and verify in `tests/test_adverts.py` that the list object is unchanged, that the removed identity's schedule is gone, and that no other identity's `next_flood_at`, `adverts_sent`, override or the global `last_global_flood_at` is disturbed
- [x] 2.3 Add `AdvertScheduler.reconfigure(public_key, *, flood_interval_seconds, zero_hop_interval_seconds)` applying intervals in place under the existing floor check, leaving `next_flood_at`, `adverts_sent` and any override untouched, and re-deriving the next advert time from the new interval only when the new configuration makes it due sooner; verify in `tests/test_adverts.py` including that a below-floor interval is refused and the schedule in force is unchanged
- [x] 2.4 Add `EntityRegistry.remove(public_key) -> bool` and a non-raising `EntityRegistry.collision(identity) -> LocalEntity | None`, keeping `_register`'s raising form for startup; verify in `tests/test_keystore.py`, including that startup still fails naming both entities on a colliding keyfile
- [x] 2.5 Read the stored advert configuration at adoption: pass `flood_interval_seconds` and `zero_hop_interval_seconds` from the entity row's `advert_config` through `_adopt_entity` to `add_identity`, treating `None` as the 24 hour floor; verify in `tests/test_entity_store.py` that a row with no interval set adverts at the floor exactly as today (the migration-free claim in design.md — Risks) and a row carrying one is honoured

## 3. Identity reconcile (specs: entity-store, advert-policy)

- [x] 3.1 Add `Runtime.entity_loader: Callable[[], Awaitable[OpenedEntities]] | None`, defaulted in `__post_init__` from `persistence.entities.load_openable(secret, enabled_only=True)` the way `channel_loader` is defaulted at `runtime.py:371`, with `cli.py` supplying the closure over `SIGHOP_SECRET_KEY`; verify in `tests/test_runtime.py` that a run with a database gets a loader and a run without one gets `None`
- [x] 3.2 Implement `Runtime.reconcile_entities() -> bool` diffing the loader's openable set against `adverts.stubs` by public key into adopt / withdraw / reconfigure, guarded by an `asyncio.Lock`, never withdrawing a keyfile-sourced stub, and keeping the loaded set and reporting the failure when the read fails; verify in `tests/test_runtime.py` against a stub loader for each of the four outcomes including the degraded-database one
- [x] 3.3 Implement withdrawal in the order design D6 sets — stop bot, stop room, remove stub, remove registry entry — and verify in `tests/test_runtime.py` that a withdrawn identity originates no further advert, that an advert already submitted is left to the transmit scheduler, and that a requested zero-hop or flood advert for it is refused stating this run does not hold it
- [x] 3.4 Implement the refused-adoption set: report a collision once, keep refusing it on later reconciles without re-reporting, and clear it for a public key when the identity it collided with is withdrawn; verify in `tests/test_runtime.py` all three of `entity-store`'s collision scenarios, including that the run continues and transmits nothing differently
- [x] 3.5 Add the adoption/withdrawal event on `ChannelSetChanged`'s shape, naming what was adopted and withdrawn, silent when nothing changed, disclosing no key material; verify in `tests/test_runtime.py` and the rendering test alongside the channel one
- [x] 3.6 Add `entity_refresh_seconds` to `RuntimeConfig` defaulting to 60 s and an `_entity_refresh_loop` sleeping on `self.clock` the way `_channel_refresh_loop` does; verify in `tests/test_runtime.py` with a manual clock that no database read happens per event-loop turn and that a change made behind the loader is adopted within the interval

## 4. Room reconcile (spec: room-server)

- [x] 4.1 Implement `Runtime.reconcile_rooms()` reusing `_serve_room` unchanged, taking up a stored room whose identity the run now holds and stopping one whose identity it no longer holds, running after `reconcile_entities` in the same pass; verify in `tests/test_runtime_rooms.py` that a room created mid-run is served and answers requests addressed to its identity
- [x] 4.2 Handle `RoomRetentionPruner` lifecycle across reconcile — constructed and started when the first room is taken up mid-run, stopped when the last is withdrawn; verify in `tests/test_runtime_rooms.py`
- [x] 4.3 Cover the spec's ordering scenarios in `tests/test_runtime_rooms.py`: a room whose identity is not held is not served and states the startup reason; the same room is served once its identity is adopted; a room whose identity is disabled stops being served with its members and messages intact and is served again with them intact when the identity is enabled
- [x] 4.4 Verify in `tests/test_runtime_rooms.py` that taking up a room mid-run leaves every already-served room's members, unsynced positions and counters untouched and logs no member out

## 5. Bot reconcile (spec: bot-runtime)

- [x] 5.1 Implement `Runtime.reconcile_bots()` reusing `_run_bot` unchanged, running after `reconcile_rooms` so a bot on a room-held entity is refused where that is known, and stopping a bot that stops qualifying with its in-flight dispatch awaited; verify in `tests/test_runtime_bots.py`
- [x] 5.2 Cover the spec's scenarios in `tests/test_runtime_bots.py`: a disabled bot is not started and states the startup reason; a bot whose entity arrives later is then run; an observing bot started mid-run touches no radio; starting a bot leaves every running bot's durable state, rate-limit position and counters untouched
- [x] 5.3 Verify in `tests/test_runtime_bots.py` that a bot stopped by a disable keeps its durable state and, when enabled again, resumes without repeating actions that state records

## 6. Panel wiring (spec: web-admin)

- [x] 6.1 Add the reconcile methods to the `LiveState` protocol in `web/state.py` beside `reload_channels`, `rename_entity`, `stop_serving_room` and `stop_bot`, and extend the stub in `tests/webfixtures.py` to match; verify `tests/test_web_state.py`'s existing acyclic-import and protocol-satisfaction assertions still pass
- [x] 6.2 Call through the seam after the store has taken the write in `web/routes/keys.py` — create, import, enable, disable — and in `web/routes/admin.py` — remove identity, create room, create/enable/disable bot — as the rename and the deletions already do; verify in `tests/test_web_admin.py` that each write reaches the run without waiting for the periodic reconcile
- [x] 6.3 Report the effect on the running process in each write's result, and distinguish "the store took it and this run did not" with its reason; verify in `tests/test_web_admin.py` the spec's "Stored but not taken up" scenario for both a not-enabled identity and a colliding one
- [x] 6.4 State the immediate consequence on the disable and remove confirmations — naming the room or bot that stops, that the room's members and messages and the bot's durable state survive, and that an operator holding it as a default will have nothing preselected; verify in `tests/test_web_admin.py`
- [x] 6.5 Verify in `tests/test_web_admin.py` and `tests/test_web_adverts.py` that the identities page after each write shows the identities the run now holds with their advert schedules, matching what the run is doing

## 7. Integration and documentation

- [x] 7.1 End-to-end test: create an identity, a room on it and a bot on a second identity from the command line against a database a run is serving, and assert within the refresh interval that the run adverts for both, serves the room and runs the bot, with no restart
- [x] 7.2 End-to-end test: disable then remove those identities and assert the run withdraws them, stops the room and the bot, and that the stored rooms, members, messages and bot state survive
- [x] 7.3 Run the full suite and the type checker, and confirm no existing test relied on `stored_entities` being fixed after construction or on `path_bodies.entities` being rebindable
- [x] 7.4 Update `DESIGN.md` where it states that identities are loaded once at startup, and record the `advert_config` interval finding and its resolution so the next reader does not re-derive it
- [x] 7.5 Run `openspec validate apply-store-changes-without-restart --strict` and confirm every scenario in the five deltas has a test that exercises it
