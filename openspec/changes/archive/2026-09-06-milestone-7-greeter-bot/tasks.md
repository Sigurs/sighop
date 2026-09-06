## 1. Schema and migration (`bot-runtime`)

- [x] 1.1 Add the `bot` model — id, unique `entity_id` foreign key to `entity`, `driver`, `enabled`, `mode`, `config` (JSONB), `created_at` as `TIMESTAMPTZ` (design D3, milestone 5 design D7); verify a unit test asserts the unique constraint on `entity_id` and that a new row defaults to enabled and to observe mode
- [x] 1.2 Add the `bot_state` model with primary key `(bot_id, key)`, JSONB `value` and `updated_at`, cascading from `bot`; verify a test asserts two bots may hold the same key with different values and that one key cannot be inserted twice for one bot
- [x] 1.3 Write migration `0003` creating both tables with `ON DELETE CASCADE` from `bot` to `bot_state`, and a `downgrade()` that drops them; verify an upgrade→downgrade→upgrade cycle in a throwaway schema leaves the schema at head with no leftover objects
- [x] 1.4 Write `0003`'s docstring recording that §6's `bot_state` arrives here, that a `bot` table joined it and why (design D3), and what a downgrade loses — greeting records, so previously greeted nodes may be greeted again if their contacts are also lost; verify by review of the migration file
- [x] 1.5 Verify the schema-version check refuses a database at `0002` when the code expects `0003`, naming both revisions and the reconciling command, by a test that stamps `0002` and asserts startup fails with that message

## 2. Repositories (`bot-runtime`)

- [x] 2.1 Add `BotRepository` beside the room repositories — create (refusing a second bot on one entity and a bot on an entity a room is bound to), list, get by name, set enabled, set mode, set config — each returning the project's `Outcome` type; verify tests for each refusal naming what already holds the entity
- [x] 2.2 Add `BotStateRepository` — get, set, delete, list for one bot — writing straight through rather than via the write-behind queue (design D16), under the engine's existing connect and statement timeouts; verify a test asserts a failed write reports failure rather than silently succeeding
- [x] 2.3 Define the storage seam as read-only-property `Protocol`s the way `RoomStorage`/`MemberSink` are, and add an in-memory implementation in `tests/botfixtures.py`; verify that every bot-runtime and greeter test in groups 5–7 passes with no database configured

## 3. Bot identities (`entity-store`)

- [x] 3.1 Allow an entity to be created as a bot — stored type `bot`, chat node type in its advert configuration (design of `entity-store` delta); verify a test asserts both are recorded and that inspecting the entity reports them
- [x] 3.2 Verify by test that a bot identity's seed is sealed identically to any other, that the node-hash collision rule applies across bot, room-server and ordinary entities, and that `sighop keys show` discloses no key material for a bot

## 4. Contact observations reach a listener (`contacts`)

- [x] 4.1 Add an optional observation listener to the contact store, invoked after the store is updated with the observation and the `RxRecord` that produced it (design D2); verify tests for a created contact, an updated one, and that a lookup from inside the listener already finds the contact
- [x] 4.2 Catch and report a listener that raises, without losing the contact, its persistence offer, or the processing of the next advert; verify a test asserts all three after a raising listener
- [x] 4.3 Verify by test that with no listener configured the store's behaviour, persistence and reporting are byte-identical to today, including the corpus replay counts

## 5. The receiving entity is named in a report (`direct-messaging`)

- [x] 5.1 Carry the receiving `LocalEntity` on the received-message report alongside the existing display name (design D15); verify a test with two entities sharing a display name asserts the report distinguishes them
- [x] 5.2 Verify by test that the acknowledgement is submitted before any consumer of the report runs, by wiring a consumer that raises and asserting the acknowledgement still reached the scheduler

## 6. The plugin seam (`bot-runtime`)

- [x] 6.1 Define the `Bot` protocol with `on_advert` and `on_direct_message` only, and document in the module why `on_channel_message` is absent until channels exist; verify by review and by a test asserting the protocol's members
- [x] 6.2 Define `BotContext` exposing exactly send, contact lookup and durable state; verify a test asserts nothing reachable from a context exposes the scheduler, the bus, the modem or a database session
- [x] 6.3 Define the advert event handed to `on_advert` — contact, created flag, hop count, signal quality, packet identity — built only from a verified advert's observation; verify a test asserts an unverified advert produces no driver invocation
- [x] 6.4 Implement the driver registry as a name→class dict with a lookup that refuses an unknown driver by listing what exists (design D14); verify a test asserts the refusal names the available drivers

## 7. Dispatch, isolation and limits (`bot-runtime`)

- [x] 7.1 Give each bot a bounded queue and a worker task; offer events from the contact-store listener and the received-message consumer without awaiting (design D12); verify a test asserts a driver that sleeps does not delay reception, decoding or acknowledgement
- [x] 7.2 Drop the oldest pending dispatch on overflow, count it per bot and report it; verify a test fills the queue and asserts the drop count and the report, and that the runtime keeps running
- [x] 7.3 Isolate a raising driver handler: report with the bot and triggering event, count, continue; verify a test asserts the runtime, the radio and a second bot are unaffected and that the failure count appears in the bot's counters
- [x] 7.4 Implement the per-bot token bucket over the sustained rate and burst in the bot's config, spent before anything is composed or queued (design D11); verify tests for a burst that exhausts it, for refusals counted by reason, and for refill over time on a fake clock
- [x] 7.5 Implement observe mode: run the full decision path, record and report the would-have-sent action, submit nothing (design D4); verify a test asserts zero submissions reach the scheduler from an observe-mode bot that decided to send
- [x] 7.6 Route an active bot's send through `DirectMessenger.send` at `PriorityClass.MESSAGE` with the existing retry, acknowledgement and timeout behaviour; verify a test asserts the submission's priority class and that no new retry policy was introduced
- [x] 7.7 Implement the state seam on `BotContext` — get, set, delete, namespaced to the bot, restored before the first event, reporting a failed write as a failure (design D16); verify tests for isolation between two bots, for a value surviving a restart, and for a failed write

## 8. The greeter driver (`greeter-bot`)

- [x] 8.1 Implement the not-yet-greeted gate: greet whenever no greeting record exists for the contact, whether or not the observation created it (design D7, revised); verify tests for a first advert, a known-but-never-greeted contact, an operator-added key later heard, a repeat advert after a greeting, and a restart
- [x] 8.2 Implement the hop gate against the reception's hop count with a configured `max_hops`, defaulting to 1 (design D8); verify tests for a zero-hop advert, a one-hop advert, an over-limit advert, and that the suppression reports the hop count and the limit
- [x] 8.3 Implement the node-type gate defaulting to chat nodes (design D9); verify tests asserting a repeater is suppressed with its type named and a chat node passes
- [x] 8.4 Implement the offered `min_snr_db` option, null by default, and document in the module that neither it nor the hop gate identifies a ducted long-haul path (design D8); verify a test for a below-floor advert and by review of the documented limitation
- [x] 8.5 Suppress and count a greeting when no route to the contact is known, never flooding one (design D10); verify a test asserts no submission and the `no_route` reason
- [x] 8.6 Write the greeting record to durable state **before** transmitting, and suppress the greeting when that write fails or storage is degraded (design D6); verify tests for the ordering, for a failed write producing no transmission, and for a degraded database producing a reported suppression
- [x] 8.7 Record the send outcome and the attempt count on the greeting record, settle the contact permanently on an acknowledgement (or a seed, an operator's mark, or an observation), and retry an unacknowledged one after `retry_after_minutes` up to `greeting_attempts` (design D6, revised after the live exercise); verify tests for an acknowledged greeting settling for good, a retry after the cooldown, an advert inside the cooldown reporting the time left, the attempts running out, and a retry surviving a restart
- [x] 8.8 Validate the greeting text at configuration time against a single direct message (`MAX_TEXT_LEN` 160 bytes), refusing with the limit and the length given, and never truncating at send time (design D14); verify tests for an over-long text refused and for a sent greeting matching the configured text exactly
- [x] 8.9 Maintain per-reason suppression counters and greeting counters, and expose them for reporting; verify a test drives one advert into each gate and asserts every counter
- [x] 8.10 Seed a new greeter with every contact the platform already holds, recorded as greeted and marked `seeded` rather than sent, so creating one on an established node owes nothing (design D7); verify tests for a seeded contact not being greeted, for a contact heard afterwards being greeted, and for an empty contact table seeding nothing
- [x] 8.11 Introduce the greeter's identity before messaging a peer that may not hold its key: `BotContext.announce(hops)`, awaited so the advert cannot be overtaken by the class-2 message, zero-hop for a contact heard directly and a flood only on a retry to one heard further away (design D18); verify tests for the zero-hop case up front, the bare first attempt further away, the flood on its retry, the ordering, and observe mode transmitting neither

## 9. Runtime wiring (`bot-runtime`, `runtime-cli`)

- [x] 9.1 Load bots at startup from the database, bind each to its entity, and refuse to run one whose entity serves a room; verify a test asserts the refusal and its reason
- [x] 9.2 Run no bots when no database is configured and state that at startup (design D5); verify a test asserts the startup line and that a replay run wires no bots
- [x] 9.3 Report each bot before any traffic is handled — identity, driver, mode, limits — and report each bot not run with its reason; verify tests for a running bot, a disabled bot and a bot on a disabled entity
- [x] 9.4 Start and stop bot workers with the runtime's existing lifecycle, draining or cancelling cleanly on shutdown; verify a test asserts no pending task survives a stop and that a shutdown mid-dispatch does not raise
- [x] 9.5 Include per-bot counters in the periodic status report — actions, observations, suppressions by reason, dropped dispatches, driver failures; verify a test asserts each appears

## 10. `sighop bot` command surface (`runtime-cli`)

- [x] 10.1 Implement `sighop bot create` on a stored identity with a named driver, stating that the bot is enabled, in observe mode, will transmit nothing until made active, and how many existing contacts it seeded as already greeted; verify tests for a successful creation, an unknown driver, an entity that already has a bot, and an entity a room is bound to
- [x] 10.2 Implement `sighop bot list` and `sighop bot show`, reporting identity, driver, mode, enablement, configuration, limits and counters; verify a test asserts each field is present and that no key material appears
- [x] 10.3 Implement `sighop bot enable`/`disable` and `sighop bot mode`, with the mode change stating that transmission still requires the run's transmit flag; verify tests asserting the stored value changes and the stated consequence
- [x] 10.4 Implement `sighop bot set` handing each value to the driver's validator and leaving stored configuration unchanged on refusal; verify tests for an accepted value and for a rejected one carrying the driver's reason
- [x] 10.5 Implement `sighop bot state` to inspect and clear durable state, stating on clear what the bot will do again; verify tests asserting the listing and the stated consequence
- [x] 10.6 Implement `sighop bot greeted` to list greeting records, inspect one contact's, clear one so it is greeted when it next adverts, and set one so it never is — resolving the contact by name or key prefix and naming what it resolved (design D7); verify tests for each action, for the stated consequence of a clear, and for an unresolvable peer

## 11. Rendering (`runtime-cli`)

- [x] 11.1 Add pure render functions for bot startup lines, bot decisions (acted, would-have-acted, suppressed with reason) and bot status counters, in `monitor/render.py` so `bots/` never imports `monitor/`; verify string-comparison tests for each
- [x] 11.2 Verify by test that an observe-mode decision is rendered plainly as transmitting nothing and is never formatted like a transmission

## 12. Regression and boundaries

- [x] 12.1 Verify `tests/protocol/test_import_boundary.py` passes unchanged and that `protocol/` gained nothing in this milestone
- [x] 12.2 Verify the reception path is unaffected by bots: replay the corpus with and without a bot wired and assert identical delivered, duplicate, contact and path counts
- [x] 12.3 Verify no test added in this milestone requires Postgres to pass, by running the full suite with no database URL set
- [x] 12.4 Verify a bot in observe mode produces zero submissions to the scheduler across the whole corpus replay, whatever its driver decides

## 13. The exercise

- [x] 13.1 Write the runbook: create the bot identity, create the greeter in observe mode, run against live adverts receive-only, then the transmit stage against the test peer; verify by review before anything is transmitted
- [ ] 13.2 Run the observe stage against live adverts and produce the decision log: how many adverts were seen, how many created contacts, and how each gate suppressed what it suppressed; verify the numbers come from the run's own counters
- [ ] 13.3 From that log, record whether the default `max_hops` of 1 is the right default on this mesh, or change it with the observation that justified the change; verify by the counters rather than by impression
- [ ] 13.4 Make the greeter active with the test peer in range, factory-reset or re-key the peer so it is genuinely unknown, and verify the greeting is transmitted, received on the peer, and acknowledged
- [ ] 13.5 Restart sighop and advert the peer again; verify no second greeting is sent — the milestone's exit criterion
- [ ] 13.6 Verify the greeting was sent over a known route and not flooded, from the run's own routing report and the captured frames
- [ ] 13.7 Record the airtime the exercise cost as a fraction of the hourly ceiling, read from the run's counters
- [ ] 13.8 Append the session whole to the corpus with its provenance header, update the corpus counts and `tests/protocol/CORPUS.md`, and verify the corpus tests pass against the new totals
- [ ] 13.9 Exercise the escalation against a peer heard at one hop that has never held the greeter's key: verify the first greeting goes out bare, the grace window adds listening and no packets, the flood advert and second greeting follow in the same reaction without a cooldown, and the second greeting is acknowledged

## 14. Documentation

- [x] 14.1 Update DESIGN.md §6: move `bot_state` from "not built yet" to built, record that a `bot` table joined it and why, and note that §6's eight-table sketch is now complete; verify by review
- [x] 14.2 Update DESIGN.md §7's Bots section: the interface as shipped without a channel hook and why, the fourth greeting rule (hop distance) and its stated limitation, the observe-mode default, and the write-record-before-transmit ordering; verify by review
- [x] 14.3 Update DESIGN.md §11 to record that the bot runtime lives in `src/sighop/bots/` rather than the sketched `entities/bots/`, for the same reason the room server lives in `net/room.py` (design D13); verify by review
- [ ] 14.4 Write DESIGN.md §12's milestone 7 entry in the established form: what was done, and the findings the offline work could not have produced — including what the observe stage showed about advert volume and hop distribution; verify by review
- [ ] 14.5 Record in §13 whatever the observe stage settled or failed to settle about greeting policy on this mesh, recording a non-observation as a non-observation; verify by review
