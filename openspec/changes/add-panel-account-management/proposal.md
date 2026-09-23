# Proposal

*A follow-up recorded by `require-database-web-drop-cli`, so the gap it opened is not lost. Only
the proposal exists; specs, design and tasks are still to be written.*

## Why

`require-database-web-drop-cli` removed the command line, and with it the only surface that managed
operator accounts: `sighop web user add|list|passwd|disable|enable|remove`. The panel has never
offered account management — by design, because with no roles any signed-in operator could mint an
account for themselves and a stolen session would become a credential that outlives it.

Since that change the account first-run setup creates is the only one. There is no way to add a
second operator, change the password, or disable or remove an account short of editing the
`web_user` table by hand, and an operator who forgets the password is locked out: `/setup` closes
once any account exists. That was accepted deliberately as a temporary loss.

## What Changes

- Account management returns, in the panel: list accounts, add one, change a password, disable and
  re-enable one, remove one.
- Every one of those is a **guarded action** — confirmed, re-authenticated with the acting
  operator's own password, and audited — because the reason the panel never offered them still
  holds: a session must not be enough to create a credential that outlives it.
- The panel refuses to disable or remove the last enabled account, so the node cannot be locked by
  its own interface.
- The copy that currently says accounts are "not managed anywhere" (`/system`, `/login`, `/setup`)
  and the `NO_ENABLED_ACCOUNT` startup refusal, which tells the operator to edit the database, are
  rewritten to point at the new pages.
- A recovery path for a forgotten password is decided explicitly — for example a one-time reset code
  printed at startup the way the first-run setup code is — rather than left as a database edit.

## Capabilities

### New Capabilities

- None expected; this restores behaviour to existing capabilities.

### Modified Capabilities

- `web-auth`: "Operator accounts are durable, hashed, and created only by first-run setup" becomes
  managed-through-the-panel, behind the guarded-action rules.
- `web-admin`: gains the account pages.

## Impact

`src/sighop/web/routes/` (a new accounts route module or a section of `admin.py`), templates, the
guarded-action registry in `web/guarded.py`, and the copy listed above. `WebUserRepository` already
has `add`, `set_password`, `set_enabled` and `remove` — kept, and tested, for exactly this change —
so no schema change is expected.
