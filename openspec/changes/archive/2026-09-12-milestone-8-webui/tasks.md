## 1. Dependencies and package skeleton

- [x] 1.1 Add `fastapi`, `uvicorn` (plain, **not** `[standard]` — design D1), `jinja2`, `websockets` and `python-multipart` to `pyproject.toml`, with a comment on the `uvicorn` line saying why `[standard]` is refused; verify `uv sync` succeeds and `uv run python -c "import uvloop"` fails
- [x] 1.2 Create `src/sighop/web/` with `app.py`, `state.py`, `feed.py`, `serialize.py`, `routes/`, `templates/`, `static/`; verify the package imports with no side effects and that `uv run python -c "import sighop.web"` starts no server and opens no socket
- [x] 1.3 Vendor `htmx.min.js` into `web/static/` with its version and license recorded beside it; verify a test asserts no template references an external origin, and that every `src`/`href` in `templates/` resolves under `static/`
- [x] 1.4 Extend `tests/protocol/test_import_boundary.py` to name `fastapi`, `starlette`, `uvicorn`, `jinja2` and `sighop.web` among the packages `protocol/` must not reach; verify the test fails when a deliberate import is added and passes without it

## 2. Schema and migration `0004` (`dm-history`)

- [x] 2.1 Add the `direct_message` model — id, `entity_public_key`, `peer_public_key`, `direction`, `text` as bytes, `wire_timestamp` `BIGINT`, `handled_at` `TIMESTAMPTZ`, `ref`, `packet_ids`, `attempts`, `route_flood`, `route_path`, `outcome`, `ack_latency_ms` — with `UNIQUE (entity_public_key, ref)` and an index on `(entity_public_key, peer_public_key, handled_at)` (design D7); verify a unit test asserts the unique constraint and that `wire_timestamp` is `BIGINT` rather than a timestamp type, per §6's rule
- [x] 2.2 Write migration `0004` creating the table with a `downgrade()` that drops it; verify an upgrade→downgrade→upgrade cycle in a throwaway schema leaves the schema at head with no leftover objects
- [x] 2.3 Write `0004`'s docstring recording that this is §6's tenth table, that a downgrade loses every recorded conversation, and that message text is stored **unencrypted** so a database dump exposes conversation content (design D7, `dm-history`); verify by review of the migration file
- [x] 2.4 Verify by test that the schema-version check refuses a database at `0003` when the code expects `0004`, naming both revisions and the reconciling command

## 3. The direct message repository and its writer lane (`dm-history`)

- [x] 3.1 Add `DirectMessageRepository` beside the others — `upsert_many` on `(entity_public_key, ref)` collapsing the batch to one row per conflict key keeping the latest (milestone 5's `cannot affect row a second time` finding), `conversation(entity, peer, limit, before)` newest-first by `handled_at`, `conversations(entity)` and `count` — each returning the project's `Outcome` type; verify a test writes the same `ref` twice in one batch and asserts one row carrying the later state
- [x] 3.2 Wire the writer lane in `db/persistence.py` as `WriteBehind(drop_oldest=False)` — the refusing lane the contact writer uses — with its own `dm_sink()`, and include its `discarded` count in the persistence status (design D8); verify a test fills the buffer and asserts refusal is reported rather than the oldest record being displaced
- [x] 3.3 Include recorded direct messages in the `restore` reporting and in the status line's counts; verify a test asserts a restart reports how many conversations and messages the database holds before the first frame is handled
- [x] 3.4 Verify by test that the packet-log pruner removes no `direct_message` row, and that the room retention pruner does not either

## 4. The messenger reports what it sends and receives (`direct-messaging`)

- [x] 4.1 Add an optional sink to `DirectMessenger` on the contact-sink contract — never awaits, never raises — offered a record at submission and again when the send resolves, carrying the local entity, peer, direction, text, route, attempts, packet ids and outcome; verify tests that a raising sink changes neither the acknowledgement, the retry schedule nor the reported outcome, and that with no sink every behaviour is unchanged
- [x] 4.2 Offer received messages to the same sink after the acknowledgement has been submitted, never before (design D8); verify a test asserts the acknowledgement reaches the scheduler before the sink is offered anything, by wiring a sink that raises
- [x] 4.3 Allow concurrent sends from callers other than the one-shot while preserving order within one identity-and-peer conversation; verify tests for two sends to different peers proceeding together, two to the same peer transmitting in submission order, and each acknowledgement matching its own message in the shared registry
- [x] 4.4 Verify by test that the corpus replay's delivered, duplicate, contact and path counts are byte-identical with a sink wired in and with none

## 5. The packet log becomes readable (`packet-log`)

- [x] 5.1 Add `PacketLogRepository.recent(limit)` — newest first, capped, under the engine's existing statement bound; verify a test asserts the cap, the ordering, and that an unparsed frame comes back with its raw bytes and reason
- [x] 5.2 Verify by test that the read is answered as unavailable within the configured bound while the database is degraded, and that nothing on the reception, dedup, dispatch or transmit path calls it

## 6. The runtime's service slot and the state seam (`web-server`)

- [x] 6.1 Add `services: tuple[Callable[[], Awaitable[None]], ...]` to `Runtime`, started with its own tasks and cancelled on stop (design D2); verify a test asserts a service runs, that its failure is reported without killing the run, and that stopping the run stops the service
- [x] 6.2 Define the read seam in `web/state.py` as `Protocol`s over what the panel needs — scheduler status, budget, dedup stats, path count, contacts, entities, probe result, persistence state, rooms and bots as the run is serving them — and assert structurally that `Runtime` satisfies them; verify a mypy-checked test that a stub implementation drives every page with no `Runtime` present
- [x] 6.3 Verify by test that `sighop.runtime` imports nothing from `sighop.web` and `sighop.web` imports nothing from `sighop.runtime`, and that `cli.py` is the only module importing both

## 7. The app, its binding, and the run's flags (`web-server`, `runtime-cli`)

- [x] 7.1 Build the FastAPI app factory taking the state seam, the repositories and the feed hub; verify a test constructs it with stubs and requests a page with no runtime and no database
- [x] 7.2 Run it as `uvicorn.Server(config).serve()` with `install_signal_handlers=False`, never `uvicorn.run()` (design D1); verify a test asserts the process's signal handlers after startup are the ones `Runtime.install_signal_handlers` set
- [x] 7.3 Add `--web`, `--web-host` (default `127.0.0.1`) and `--web-port` to `sighop run`, wiring the service in `cli.py`; verify a test asserts a run without `--web` listens on nothing and prints nothing about a web interface
- [x] 7.4 Report the interface at startup with its address and port, and — when the address is not loopback — that it is unauthenticated and reachable from the network, in the output and as its own logged event with no option to suppress it; verify tests for the loopback and non-loopback lines and for the event's presence
- [x] 7.5 Make a bind failure a startup failure reported before traffic is processed, naming the address, port and reason; verify a test binds the port first and asserts the run fails with that message rather than continuing without an interface
- [x] 7.6 Bound shutdown: stop accepting, let in-flight requests finish within the bound, close feed connections, cancel the task; verify a test asserts the run exits with a connected WebSocket open

## 8. Request provenance and wide events (`web-server`)

- [x] 8.1 Generate a per-process provenance token, embed it in every served page, and require it on every state-changing request (design D9); verify tests that a POST without it changes nothing and is rejected, and that the rejection appears in the request's event
- [x] 8.2 Reject any request whose `Host` header is not an address the interface was configured to serve, before any handler runs, and check `Sec-Fetch-Site` where present; verify a test asserts a rebinding-shaped `Host` is rejected with no handler invoked
- [x] 8.3 Verify by test that no GET or HEAD route changes state, transmits, or includes key material in its response — enumerated over every registered route rather than asserted per route
- [x] 8.4 Emit one wide event per completed HTTP request and per closed WebSocket, carrying the shared context plus method, route, status, outcome and duration, and for a socket its delivered and dropped counts (§9); verify tests asserting exactly one event per request and per closed connection
- [x] 8.5 Serve a generic error page on an unhandled exception with the failure recorded in the event and no traceback, path or internal detail in the response; verify a test against a deliberately raising route

## 9. The panel's shell and rendering primitives (`web-dashboard`)

- [x] 9.1 Build the base template carrying the duty-cycle meter, the transmit gate indicator and the persistence state, extended by every page (design D12); verify a test asserts every registered page renders the meter, enumerated over the route table
- [x] 9.2 Implement the meter: consumed airtime and remaining fraction against the ceiling, stating when the ceiling in force is above the regulatory default and when transmission is disabled; verify tests for a closed gate, a raised ceiling and a budget near its limit
- [x] 9.3 Implement the identity macro — verified, unverified, key-only — as the only way any view renders an identity, marking status with a glyph and text rather than colour alone (design D13); verify a test asserts no template renders a contact name outside the macro, and a rendering test asserts the distinction survives with colour stripped
- [x] 9.4 Implement the instrument-panel styling per §8 — dark-first, monospace and tabular for hashes, paths, signal and hex, colour reserved for budget state, verification and direction; verify by review against §8's four bullets, and by a test asserting hash and path columns render in a fixed-width class
- [x] 9.5 Distinguish "empty" from "unavailable" in one shared partial used by every durable-state view; verify a test asserts the two wordings differ for a readable-but-empty collection and an unreadable one

## 10. The live feed (`web-dashboard`)

- [x] 10.1 Implement the feed hub: exactly one `NetworkBus.subscribe("web-feed")` for the process plus the runtime's TX resolution callback, fanning out to bounded per-connection queues, offering without awaiting and never raising (design D4); verify a test asserts the bus reports one web subscriber with ten connections open
- [x] 10.2 Drop the oldest record for a connection that cannot keep up, count per connection, and report the count into that connection; verify a test asserts the drop count reaches the browser and that the platform's own handling of the dropped record was unaffected
- [x] 10.3 Implement the WebSocket route and the hand-written client that renders rows, marks the boundary between recorded history and live records, and shows the incomplete-feed state; verify a browser-free test drives the socket and asserts the record sequence, and an `sighop run --replay` exercise shows rows appearing
- [x] 10.4 Serialise `RxRecord`, `Submission` and `TxOutcome` in `web/serialize.py` — direction, time, route type, payload type, size, path and length, signal quality, duplicate flag, packet id, and for a transmission its outcome — reusing the existing `as_json()` where it fits and adding nothing to `net/` (design D5); verify tests over corpus records including an unparsed frame and a suppressed transmission
- [x] 10.5 Paint the feed on connection from `PacketLogRepository.recent`, newest first, before streaming; verify a test asserts the history precedes the live records and that with no database the feed starts empty saying only live records are shown
- [x] 10.6 Verify by test that a connection which stops reading entirely changes no reception, dedup, dispatch or transmit count, and is eventually closed with its event emitted

## 11. The dashboard's pages (`web-dashboard`)

- [x] 11.1 Build the overview: queue depth by priority class, scheduler counts, dedup occupancy and duplicates, learned path destinations, contact count, persistence state with discarded-write counts; verify a test asserts each value against a stub state
- [x] 11.2 Build the modem health view from the startup probe, showing a value the board did not answer as absent rather than zero (§4.1); verify a test with an `Absent` battery reading asserts the display says absent
- [x] 11.3 Build the contact table — name, public key, node hash, node type, first and last heard, learned routes with hop count, signal and confirmation time — rendering an empty path as a zero-hop direct route and marking a hash-matched route ambiguous; verify tests for the zero-hop case, the ambiguous case and a contact with no route
- [x] 11.4 Build per-entity TX/RX counters; verify a test asserts an entity's counts against a stub state

## 12. Admin and configuration (`web-admin`)

- [x] 12.1 Build the identities view — list, create, enable, disable, import, export — through the repository calls `sighop keys` uses, stating that an exported keyfile is an unencrypted seed protected only by its permissions; verify a test asserts a UI-created identity is indistinguishable from a CLI-created one, and that the export warning is present
- [x] 12.2 Warn before disabling an identity a room or bot is bound to, naming what it serves; verify a test asserts the warning names the room or bot
- [x] 12.3 Build the rooms view — create, list with member and message counts, set and rotate passwords, set guest access and read-only fallback, set and clear the two retention bounds — through the room repositories, never accepting a password in a query string and never reflecting one in a page or a request event; verify a test asserts a submitted password appears in no response body and in no emitted event
- [x] 12.4 State the consequence before applying: a rotation requires members to log in again; a retention bound removes N stored messages; verify tests asserting both are stated with the count before the action is applied
- [x] 12.5 Build the bots view — list, create, enable, disable, switch mode, edit driver configuration, read durable state including the greeting records — through the bot repositories, refusing an invalid configuration with the driver's own reason; verify tests for a refused configuration leaving the stored one unchanged and for the greeting records being readable
- [x] 12.6 Confirm a switch to active mode explicitly, stating that the bot will transmit unprompted; verify a test asserts the mode is unchanged until the confirmation is submitted
- [x] 12.7 Build the radio view from the board's readback, stating that the board does not persist a change and that a reset reverts to its build defaults; verify a test asserts the statement is present and that an unanswered parameter shows as absent

## 13. Guarded actions (`web-admin`)

- [x] 13.1 Implement the guarded-action pattern — a confirmation view stating what the action does, a POST carrying the provenance token plus a nonce minted by that view, and its own wide event naming action, target and outcome, separate from the request event (design D10); verify a test asserts a POST without the nonce is refused and emits the audit event with a refused outcome
- [x] 13.2 Apply it to revealing a private key: the material appears in exactly one response body, in no page reachable by navigation, and the reveal is its own event naming the identity; verify tests over every other route asserting no key material appears, and one asserting the event is emitted
- [x] 13.3 Apply it to enabling transmission, changing the run's gate and the panel's indication together, with the run's output reporting the change; verify a test asserts the scheduler's gate and the rendered indication agree after the action
- [x] 13.4 Apply it to raising the airtime ceiling, with the confirmation stating that the default is a regulatory limit and the event carrying the old and new values; verify a test asserts both, and that the meter states the ceiling is above the default for as long as it is

## 14. Room browsing (`web-rooms`)

- [x] 14.1 Build the room history view — newest first, bounded pages, each message with its ordering timestamp, author, stored time and text rendered from bytes, showing undisplayable bytes as such rather than substituting silently; verify tests for paging order across pages and for a message whose bytes are not valid text
- [x] 14.2 Identify authors by public key, adding a name only from a verified contact and marking anything else unauthenticated (design D13); verify tests for a verified author and for an author matching no verified contact
- [x] 14.3 Build the member list — public key, node hash, permission, sync cursor, last activity, first login, unsynced count — and handle two members sharing a node hash as two members; verify a test with a deliberate hash collision asserts both are listed and distinguished
- [x] 14.4 Offer revocation from this view, stating that it removes membership, permissions, cursor and replay guard together, confirmed before applying; verify a test asserts the statement and that the row is gone afterwards
- [x] 14.5 List rooms this run is not serving as unserved with the reason, keeping their history browsable; verify a test with a room whose identity was not loaded asserts both
- [x] 14.6 Verify by test that opening, paging and refreshing any room view transmits nothing and advances no member's cursor

## 15. The chat client (`web-chat`)

- [x] 15.1 Build the conversation list and view keyed by local identity and contact, never merging two identities' messages with one contact; verify a test with two identities and one contact asserts two conversations
- [x] 15.2 Send through `DirectMessenger.send` as the chosen identity, showing the message's state as it progresses — awaiting, attempt N, acknowledged with latency, or unacknowledged; verify tests for an acknowledged send and for one where every attempt goes unanswered, asserting the second is not shown as delivered
- [x] 15.3 Refuse at composition, with the reason and the author's text preserved: text over `MAX_TEXT_LEN`, no known route without flooding explicitly chosen, and a closed transmit gate (design D11); verify tests for each, asserting nothing is queued and no message enters the conversation's history
- [x] 15.4 Offer flooding as an explicit per-send choice rather than performing it; verify a test asserts a send to a routeless contact floods only when the choice was made
- [x] 15.5 Show received messages in their conversation as they arrive without a reload, and indicate a conversation that has something new; verify a test drives a reception and asserts both
- [x] 15.6 Identify a received message's sender by the contact whose key decrypted it, stating that the protocol authenticates key possession rather than identity, and marking an unverified contact's message accordingly; verify a test asserts the statement and the marking
- [x] 15.7 State that channels are unsupported and why — no channel key store, no group text decryption — where a user would look for them, and show a received group text in the feed as the undecrypted payload it is rather than as a chat message; verify a test asserts no channel composer exists and that a corpus `GRP_TXT` reception produces no chat message
- [x] 15.8 Keep chat usable with no database and with a degraded one, stating that the conversation is not being recorded; verify tests for both, asserting sending and receiving still work
- [x] 15.9 Verify by test that opening, scrolling and refreshing a conversation transmits nothing, including any receipt or presence indication

## 16. Non-regression

- [x] 16.1 Verify that a corpus replay with the whole interface wired in — feed hub subscribed, a connection open, the DM sink attached — produces byte-identical delivered, duplicate, contact and path counts to a replay without it, the assertion milestones 6 and 7 both used
- [x] 16.2 Verify that `protocol/` gained nothing: the import boundary test passes and the protocol test suite is unchanged
- [x] 16.3 Verify the whole suite passes with no database configured, no web interface enabled, and both enabled, and that `ruff` and `mypy` are clean across `src/` and `tests/`
- [x] 16.4 Run the suite repeatedly to catch a probabilistic assertion of the kind milestone 7 found twice; verify no test in the new groups generates a key or hash and then asserts a property that §3's collision rate makes occasionally false

## 17. Documentation

- [x] 17.1 Correct DESIGN.md §8's authentication rule: a non-loopback bind is permitted before milestone 9 when configured, and is announced — recording that this is a deliberate operator decision and what bounds it (design D9); verify by review that the section no longer states a rule the code does not enforce
- [x] 17.2 Update DESIGN.md §11's `web/` line to describe the package that now exists, and record that the companion needed no module of its own — §7's companion turned out to be a user interface over milestone 4's send path, so `entities/` still does not exist (design D6); verify by review
- [x] 17.3 Record the tenth table in DESIGN.md §6 with its unencrypted-text consequence, and the `packet_log` read in the same section; verify by review
- [x] 17.4 Write the milestone 8 findings into DESIGN.md §12 as milestones 0–7 are written — what the offline work produced, and what the live exercise overturned; verify by review after group 18
- [x] 17.5 Write `runbook.md` for the live exercise: what to open, in what order, what to watch, and the exit criterion; verify by review before the exercise runs

## 18. Live exercise

- [x] 18.1 Run `sighop run --web --replay` against the corpus with a browser open; verify the feed paints, the meter reads, the contact table fills, and the counts match the replay's own reporting
- [x] 18.2 Run against the live modem receive-only with the gate closed; verify the feed keeps up with real advert volume, and record the per-connection queue depth and drop count the session produced (design's first open question)
- [x] 18.3 Exercise the admin surface against the development database: create an identity, create a room on it, rotate its password, set retention, create a bot and switch it to active and back; verify each change is visible to the CLI and each guarded action produced its own event
- [x] 18.4 **Exit criterion**: with the gate open and a stock MeshCore peer, send a direct message from the browser as a companion entity, see it acknowledged in the conversation, receive the peer's reply in the same conversation without a reload, restart the platform, and see both messages still there — the whole exchange driven from the browser with no command line
- [x] 18.5 Append the exercise's capture to the corpus whole if it carries a shape the corpus lacks, per §12's rule; verify the corpus tests pass against the enlarged set
