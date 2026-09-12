## 1. The shared shapes these pages need

- [x] 1.1 Add a form-refusal shape to `web/deps.py` or `web/render.py` — a reason, the field it concerns, and the author's submitted values — so a refused form re-renders with what was typed rather than an empty one; verify a test asserts a refused submission returns the submitted values unchanged in the response
- [x] 1.2 Extend `web/guarded.py` with the two new actions (`export_private_key`, `post_to_room`) and their descriptions, on the existing nonce and audit machinery; verify a test asserts each mints and spends a nonce good exactly once and for its own target only
- [x] 1.3 Add a `sealing_secret` to `WebInterface`/`create_app`, supplied by `cli.py` from the configuration it already reads (design D1), defaulting to `None`; verify a test asserts the panel builds without one and that `cli.py` supplies it when a database is configured

## 2. Identities: create, inspect, import (`web-admin`)

- [x] 2.1 Add a create form to the identities page that generates an identity avoiding every node hash the run knows and stores it through `EntityRepository.store` — the call `sighop keys import` makes (design D3); verify a test asserts a UI-created identity is byte-identical in the store to one created through the repository directly, and that a second creation cannot take a node hash already held
- [x] 2.2 Add a per-identity page showing name, type, public key, node hash, enabled state, creation time and advert configuration — `sighop keys show` for a stored identity; verify a test asserts every field is present and that no seed appears
- [x] 2.3 Add an import form taking a keyfile's JSON document, sealing its seed through the same repository call; verify a test asserts a keyfile written by `sighop keys new` imports and produces the same stored row as `sighop keys import`, and that a malformed document is refused with the keystore's own reason and stores nothing
- [x] 2.4 Verify by test that creating and importing are refused with the repository's own message when the public key is already stored, rather than with a message this surface invented

## 3. Exporting an identity (`web-admin`, guarded)

- [x] 3.1 Build the export confirmation view naming the identity and stating what the file is, minting a nonce for that identity alone; verify a test asserts the confirmation page contains no key material
- [x] 3.2 Serve the export as a download of `keyfile_document(...)` for any stored identity, opened with the sealing secret (design D1, D2); verify a test asserts the downloaded document is byte-identical to what `create_keyfile` writes for the same identity, and that it carries a filename and an attachment disposition
- [x] 3.3 State on the confirmation and on the identities page that the command line creates the file owner-only in one call and a download has whatever the browser's directory gives it; verify a test asserts both statements are present
- [x] 3.4 Emit the export as its own `web_guarded_action` event naming the identity, with a refused outcome when the nonce is absent or spent; verify tests for both outcomes, and that no event carries the seed
- [x] 3.5 Verify by test that a disabled stored identity can be exported — the case that made design D1 pass the secret rather than use the run's loaded identities

## 4. Rooms: create and the read-only fallback (`web-admin`)

- [x] 4.1 Add a create form binding a room to a stored room-server identity through `RoomRepository.create` with the admin password hashed off the loop; verify a test asserts the stored row matches one created by `sighop room create`, and that the second room on one identity is refused by the schema's own constraint
- [x] 4.2 Add a control for `allow_read_only` beside guest access on the room configuration page — the clause milestone 8's task 12.3 marked done without building; verify a test asserts the flag can be set and cleared and that the stored value changes
- [x] 4.3 Verify by test that a room created through the interface comes up served by the next run that loads its identity, and unserved by one that does not

## 5. Posting to a room (`web-rooms`, guarded)

- [x] 5.1 Build the post composer with the room named, refusing text over `STORED_POST_TEXT_LEN` with the limit, the overage and the author's text preserved (design D4); verify tests for a refused over-length post storing nothing and for the text surviving the refusal
- [x] 5.2 Build the confirmation stating that the post reaches every member, that it is stored now and becomes deliverable after the reference implementation's hold, and — when the gate is closed or the room is unserved — what will not happen and when it will; verify tests for the served, unserved and gate-closed wordings
- [x] 5.3 Store the post through `MessageRepository.store` as the room's own identity, on the nonce; verify a test asserts the stored row is indistinguishable from one `sighop room post` produced, with an ordering timestamp in the room's total order
- [x] 5.4 Emit the post as its own `web_guarded_action` event naming the room and the outcome, refusals included; verify tests for a post without a nonce storing nothing and emitting a refused event
- [x] 5.5 Verify by test that opening the composer and the confirmation store nothing and transmit nothing — only the confirmed POST does

## 6. Bots: create, configure per key, and their records (`web-admin`)

- [x] 6.1 Add a create form binding a driver to a stored identity through `BotRepository.create` with the driver's own defaults, refusing an unknown driver by listing what exists; verify a test asserts the stored row matches one from `sighop bot create` and that a new bot is enabled and observing
- [x] 6.2 Replace the JSON textarea with a per-key form validated by `drivers.validate_config` (design D7); verify tests that a refused value names its own key and leaves every other key unchanged, and that the textarea is gone
- [x] 6.3 Build the greeting-record view — list, clear one, set one, seed every known contact — through `BotStateRepository`, using `bots/greeter.py`'s key helpers (design D5); verify tests for each operation against the stored keys
- [x] 6.4 State before clearing a record that the contact becomes eligible to be acted on again, and before seeding that contacts with a record keep the one they have; verify tests asserting both statements precede their action
- [x] 6.5 Add clearing a bot's whole durable state, stating what it forgets and reporting how many keys went; verify a test asserts the statement precedes the change and the count is reported
- [x] 6.6 Verify by test that a greeting record cleared through the interface makes the greeter eligible for that contact again, using the greeter's own gate rather than an assertion about rows

## 7. The schema page, and what is deliberately absent (`web-admin`)

- [x] 7.1 Build a schema page showing the revision the database reports and the revision this code expects, and whether they agree; verify tests for agreement and for disagreement naming both revisions and the reconciling command
- [x] 7.2 State on that page that migrations are applied deliberately from a terminal and are not offered here, and why (design D6); verify a test asserts the statement and that no route on the interface applies a migration
- [x] 7.3 State on the identities page that the sealing secret is generated from a terminal and not here, and why; verify a test asserts the statement
- [x] 7.4 Verify by test that the schema page degrades like every other durable view — no database configured and a degraded one are two distinct wordings, neither of them an empty page

## 8. Non-regression and the seam

- [x] 8.1 Verify that every new page renders the duty-cycle meter, enumerated over the route table through `registered_routes` rather than `app.routes`
- [x] 8.2 Verify that no GET or HEAD route on the enlarged interface changes state, transmits, stores a post or reveals key material — the same sweep milestone 8's task 8.3 built, over every new route
- [x] 8.3 Verify that every new write emits exactly one request event, and every guarded action one further event of its own
- [x] 8.4 Verify that a corpus replay with the enlarged interface wired in still produces byte-identical delivered, duplicate, contact and path counts to one without it
- [x] 8.5 Verify that `web/` still imports nothing from `sighop.runtime`, that `cli.py` is still the only module knowing both sides, and that `ruff` and `mypy` are clean across `src/` and `tests/`

## 9. Documentation

- [x] 9.1 Correct milestone 8's tasks 12.1, 12.3 and 12.5, which are marked complete while their specs' create, import, export and read-only-fallback clauses were never built — either by unchecking them or by recording that this change delivers them; verify by review that the record no longer claims work that was not done
- [x] 9.2 Record in DESIGN.md §8 which command-line capabilities the interface deliberately does not expose, and why — migrations and the sealing secret; verify by review
- [x] 9.3 Record the finding that `sighop room post` is a stored row rather than a transmission, and what that means for a panel that is showing a running platform at the time; verify by review
- [x] 9.4 Write the findings this change produced into DESIGN.md §12 under milestone 8, as its own paragraph; verify by review after group 10

## 10. Exercise

- [x] 10.1 Against the development database with no radio: create an identity, export it, re-import the exported file, and confirm the CLI sees one identity and the file round-trips
- [x] 10.2 Create a room and a bot through the interface, and confirm `sighop room list` and `sighop bot list` show what the browser shows
- [x] 10.3 Clear a greeting record through the interface and confirm `sighop bot greeted` agrees
- [x] 10.4 **Exit criterion**: with a room served and the gate open, post to it from the browser and see a stock MeshCore client receive it — the post composed, confirmed, stored and delivered without a command line
