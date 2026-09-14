## 1. Repository (`web-auth`, design D6)

- [x] 1.1 Add `WebUserRepository.count()` (all accounts, enabled or not); verify a database test in `tests/test_web_user_repository.py` counting 0, then 2 with one disabled
- [x] 1.2 Add `WebUserRepository.add_first(username, *, password_hash)`: one `Database.run` unit taking `LOCK TABLE web_user IN SHARE ROW EXCLUSIVE MODE`, counting, inserting only when zero, returning the record or `None`; verify database tests that it inserts into an empty table, returns `None` and inserts nothing when any account (including a disabled one) exists, and that two concurrent `add_first` calls with different usernames leave exactly one row
- [x] 1.3 Verify the lock ordering against a plain `add`: a test that starts `add_first` holding its lock, runs `add` concurrently, and finds `add` committed after it (two rows, setup's first), and the reverse order yields `add_first` returning `None`

## 2. Setup decision (`web-auth`, design D2, D3, D5)

- [x] 2.1 Add `FirstRunSetup` to `web/auth.py`: 20-character Crockford base32 code from `secrets`, `display` grouped `XXXXX-XXXXX-XXXXX-XXXXX`, `check()` normalising case, `-` and whitespace and comparing with `hmac.compare_digest`, `pending`, `close()`, a `repr` that omits the code; verify unit tests for alphabet, length, grouping, case/separator/space tolerance, a wrong code, a closed setup refusing the right code, and `repr` not containing the code
- [x] 2.2 Extend the `AccountStore` protocol with `count()` and `add_first()`, and `MemoryAccounts` in `tests/webfixtures.py` to match (including `degraded`); verify `uv run mypy` passes and existing `test_web_auth.py` still passes
- [x] 2.3 Add `Authenticator.setup` and `Authenticator.complete_setup(code, username, password, password_again, *, client, presented)` implementing design D5's order under an `asyncio.Lock`, issuing a session on success, closing setup, and emitting `web_setup` with `outcome`/`reason`/`username`/`client` only; verify unit tests over `MemoryAccounts` with an injected clock: success issues a session and closes setup; `bad_code` hashes nothing (counting hasher) and does not echo; `bad_username`, `password_empty`, `password_mismatch` leave the code usable; `add_first` returning `None` closes setup with `setup_closed`; a closed setup refuses the old code; no captured event contains the code or password
- [x] 2.4 Verify a sign-in attempt while setup is pending fails with reason `unknown_user` and the standard `LOGIN_FAILED` response (unit test)

## 3. Routes and guard (`web-auth`, `web-server`, design D7)

- [x] 3.1 Add `GET`/`POST /setup` to `PUBLIC_ROUTES` and make the guard redirect unauthenticated safe requests to `/setup` while `auth.setup` is pending; verify `tests/test_web_guard.py` cases for both redirect targets and that `tests/test_web_auth_routes.py`'s route walk passes with the new public pair and fails if a third route is made public
- [x] 3.2 Add `web/routes/setup.py` and standalone `templates/setup.html` (process `login_token`, `setup_code`, `username`, `password`, `password_again` with `autocomplete="new-password"`, note naming the run output and `sighop web user add`); factor the session cookie setting shared with `/login` into one helper; include the router in `create_app`; verify `uv run pytest tests/test_web_login.py` still passes
- [x] 3.3 `GET /setup`: `303 /login` when setup is absent or closed, or when `accounts.count()` is above zero (closing setup); render the form when the count read fails; `GET /login` redirects to `/setup` while pending; verify route tests in new `tests/test_web_setup.py` for each branch, and that the rendered form contains no code, no platform state and no key material
- [x] 3.4 `POST /setup`: call `complete_setup`, on success set the cookie and `303 /`, on refusal re-render the form with the reason's message (fixed message for `bad_code`, username never echoed) or `303 /login` for `setup_closed`; verify `tests/test_web_setup.py` end-to-end over a test client: successful setup lands on the overview signed in; wrong code; mismatch then success with the same code; a second submission after success creates nothing; a submission without `_token` is `403` with nothing checked (counting hasher and `MemoryAccounts` unchanged)
- [x] 3.5 Verify with a database test that an account added through `WebUserRepository.add` while a served app is in setup mode makes `GET /setup` redirect to `/login` and a setup `POST` with the code create nothing

## 4. Startup (`runtime-cli`, design D1, D4)

- [x] 4.1 In `cli._attach_web`, read `count()` and `count_enabled()`: zero total builds a `FirstRunSetup` on the `Authenticator`; accounts present with none enabled raises `NO_ENABLED_ACCOUNT` reworded to name `sighop web user enable <username>` and `sighop web user add <username>`; verify `tests/test_web_server.py` cases for all three rows of design D1's table, including that no port is listened on in the failing case
- [x] 4.2 `WebInterface.bind` accepts `accounts_enabled == 0` only with a pending setup; `startup_lines()` prints the setup-pending line, `/setup` URL and the grouped code before the loopback or plain-HTTP line; `report()` adds `setup_pending` and never the code; verify tests asserting the output lines for loopback and non-loopback binds and that the captured `web_interface_listening` event has `setup_pending: true` and no field containing the code
- [x] 4.3 Announce setup completion through `announce` ("web: first-run setup completed; account '<name>' created"); verify a test capturing the announcement after a successful `POST /setup`
- [x] 4.4 Verify a restart generates a different code and the old one is refused (test building two interfaces over the same empty store)

## 5. Account CLI wording (`runtime-cli`, design D8)

- [x] 5.1 Make `_leaves_no_account` distinguish "last enabled account, disabled ones remain" from "last account at all"; `remove` of the only account without `--allow-no-accounts` refuses stating the next `run --web` would offer first-run setup to whoever holds its code, and with it states setup will be offered; `disable` of the last enabled account keeps `NO_ACCOUNT_LEFT`; verify `tests/test_web_user_cli.py` cases for each
- [x] 5.2 `web user list` with no accounts states that `run --web` will offer first-run setup and names `sighop web user add <username>`; verify the existing empty-list test updated to the new wording

## 6. Documentation

- [x] 6.1 `compose.yaml` header: step 4 becomes opening `http://localhost:8080/setup` with the code from `docker compose logs sighop`, with `docker compose run --rm -it sighop web user add <name>` as the alternative; remove "Until an account exists, sighop refuses to serve the panel and restarts"; verify `uv run pytest tests/test_deployment_files.py` passes and `docker compose -f compose.yaml config` resolves with the example environment
- [x] 6.2 `DESIGN.md` §8 Authentication: accounts terminal-managed except first-run setup (code in run output only, 100 bits, no throttle and why, table-lock atomicity, empty table vs all-disabled); "What the interface deliberately does not expose" → managing accounts beyond the first; `login.html` note mentions setup only if still accurate; verify by review
- [x] 6.3 `DESIGN.md` §10: accounts added through first-run setup or `web user add`; verify by review

## 7. Gates and live check

- [x] 7.1 Run `uv run ruff check`, `uv run mypy` and the full `uv run pytest` (database tests against the development database); verify all pass
- [x] 7.2 Live: against a fresh compose Postgres with no account, `docker compose -f compose.yaml up -d`, confirm the container stays up and `docker compose logs sighop` shows the setup line and code while the JSON `web_interface_listening` event has `setup_pending: true` and no code; complete setup in a host browser with one wrong code first; confirm the overview loads, `sighop web user list` inside the container shows the account, `/setup` now redirects to `/login`, and after `docker compose restart sighop` no setup line is printed and sign-in works
