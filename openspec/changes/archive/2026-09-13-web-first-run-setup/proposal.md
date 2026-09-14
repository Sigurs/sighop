## Why

A fresh deployment cannot serve the panel until an operator runs
`docker compose run --rm -it sighop web user add <name>`; until then `run --web` refuses at
startup and the container restart-loops. Creating the first account from the browser removes
that terminal step without opening the panel to whoever reaches the port first.

## What Changes

- `run --web` against a database whose `web_user` table is **empty** no longer fails at startup.
  It serves the interface in **first-run setup** mode: the radio runs as it would with any other
  run, and the only thing the interface offers is a setup form.
- At startup the run generates a random **one-time setup code** and prints it, with the setup
  URL, in the run's output (so `docker compose logs sighop` shows it). The code is never written
  to a structured event, never rendered in a page, and dies on restart.
- `GET`/`POST /setup` join the fixed public route set. The form takes the setup code, a username
  and a password entered twice. A correct code creates the first account, enabled, signs that
  browser in, and closes setup for the life of the process.
- While setup is pending, every non-public request is sent to the setup form instead of the
  sign-in form. Once any account exists — created by the form or by `sighop web user add` from a
  terminal — `/setup` sends the browser to the sign-in form and accepts nothing.
- Creation is atomic against the database: two concurrent submissions, or a submission racing a
  terminal `web user add`, produce at most one setup-created account and never one beside an
  existing account.
- A database holding accounts that are **all disabled** is unchanged: startup still fails, now
  naming `sighop web user enable` as well as `add`. Setup never undoes a deliberate lockout.
- `sighop web user remove --allow-no-accounts` on the last account states that the next
  `run --web` will offer first-run setup, instead of stating that no run could start the panel.
- Documentation: `compose.yaml` header step 4 and `DESIGN.md` §8/§10 describe setup through the
  browser, with the terminal command as the alternative.

Not changing: accounts beyond the first remain terminal-only; there is still no browser password
change, account list or disable; authentication still cannot be turned off.

## Capabilities

### New Capabilities

None. First-run setup is part of how operator accounts come to exist, which `web-auth` owns.

### Modified Capabilities

- `web-auth`: the public route set gains the setup form and its submission; accounts are
  terminal-managed except the first, which first-run setup may create once with a one-time code;
  new requirement describing setup mode, the code, atomic creation and closure.
- `runtime-cli`: `run --web` with no account at all serves setup mode and prints the code instead
  of failing; with only disabled accounts it still fails, naming enable and add; removing the last
  account states the setup consequence.
- `web-server`: the process-issued provenance token covers the setup form as it covers the sign-in
  form, and a setup submission without it is refused before any code or password is checked.

## Impact

- `src/sighop/web/guard.py` — public set, setup-pending redirect target.
- `src/sighop/web/auth.py` — setup code holder (generation, constant-time check, retirement).
- `src/sighop/web/routes/session.py` (or a new `routes/setup.py`) and `templates/setup.html`.
- `src/sighop/web/app.py` — `WebInterface.bind` accepts zero accounts when the table is empty,
  setup startup lines and event field; `NO_ENABLED_ACCOUNT` wording.
- `src/sighop/db/repositories.py` — `WebUserRepository.count`, and `add_first` that inserts only
  into an empty table under a table lock.
- `src/sighop/cli.py` — `_attach_web` decision (empty vs all-disabled), `web user remove`
  message.
- Tests: `test_web_server.py`, `test_web_auth_routes.py`, `test_web_login.py`,
  `test_web_user_repository.py`, `test_web_user_cli.py`, `webfixtures.MemoryAccounts`, plus a new
  `test_web_setup.py`.
- `compose.yaml` comments, `DESIGN.md`. No migration, no new dependency, no new configuration.
