## Why

The panel milestone 8 built can watch everything and change almost nothing. Eleven pages
read live and durable state; five write anything at all, and the five that do were chosen by
what was easy to reach rather than by what an operator needs. Every identity, room and bot
in the system still has to be *created* from a terminal, which means the browser is a viewer
of a platform that is administered somewhere else — and §8's first priority area is "admin
and config", not "admin and config, minus the part where things come into existence".

**Three of milestone 8's own tasks were marked complete without their specs being met, and
that is part of the motivation here rather than a footnote.** `web-admin` requires the
identities view to allow "creating a new identity, enabling and disabling one, and importing
and exporting one"; the rooms view to allow "creating a room on an identity … setting guest
access and read-only fallback"; and the bots view to allow "creating one". Tasks 12.1, 12.3
and 12.5 are checked. What exists is listing, enabling, disabling, password rotation,
retention, mode and configuration — **create, import, export and the read-only fallback
control were never built**, and the identities page states what an export *is* without
offering one. This change delivers those clauses, and milestone 8's checkboxes should be
read against it rather than trusted.

The rest is the remaining distance between the two surfaces. Twelve `sighop` subcommands
have no browser equivalent; nine of them are ordinary configuration and three carry weight.
Two of the three are in scope here and one is deliberately not.

## What Changes

- **Identities become creatable, inspectable, importable and exportable.** `keys new` and
  `keys import` write through `EntityRepository.store`, the call `sighop keys` already makes.
  `keys show` becomes a per-identity page. **`keys export` is a guarded download** — an
  unencrypted seed leaving the box as a file — behind the confirm-then-act pattern already
  built for revealing a key, with its own audit event.
- **Rooms become creatable, and postable to.** `room create` binds a room to a stored
  room-server identity through `RoomRepository.create`. **`room post` transmits**, so it is a
  guarded action: it reaches every member of the room, and a post is not a configuration
  change.
- **The read-only fallback gains a control.** `room.allow_read_only` is displayed today and
  cannot be set, which is half a feature: the column says what the room does and the page
  offers no way to change it.
- **Bots become creatable, and their records editable.** `bot create` through
  `BotRepository.create` with the driver's own defaults. `bot greeted` — inspect, clear, set,
  seed — is the control surface over the records that decide whether a greeter will ever act
  on a contact again, and clearing one is how an operator releases a peer deliberately.
  `bot state --clear` forgets everything a bot knows.
- **Bot configuration is validated per key, as the command line validates it.** The panel
  currently takes the whole configuration as JSON and validates each key on the way in; the
  CLI edits one key at a time with the driver's own parser and its own message. The panel
  gains the same per-key form, so a refusal reads the same in both surfaces.
- **The schema revision the run is against becomes visible.** `db current` is read-only and
  answers the question a degraded panel raises first: is this database the one this code
  expects?
- **`db upgrade` is deliberately absent, and the interface says so where an operator would
  look for it.** DESIGN.md §6 makes applying migrations a deliberate operator act that is
  never a side effect of starting; an unauthenticated port would make DDL reachable by
  anything that can route to it. Until milestone 9 that is not a trade worth making, and
  naming the absence is cheaper than leaving somebody to discover it.
- **`keys secret` is deliberately absent** for a smaller reason: it generates a value that
  must be kept and never regenerated, and a browser is a poor place to hand somebody
  something they must not lose. It stays a terminal command.
- **`capture`, `monitor` and `run` are not UI candidates and the reasoning is recorded.**
  `run` *is* the process serving the panel; `capture` and `monitor` are offline tools against
  a serial device that a running platform already holds open.

## Capabilities

### New Capabilities

None. Every surface here is an addition to a capability milestone 8 introduced.

### Modified Capabilities

- `web-admin`: identities can be created, inspected, imported and exported (the export as a
  guarded action); rooms can be created and their read-only fallback set; bots can be
  created, their driver configuration edited one validated key at a time, their greeting
  records inspected and changed, and their durable state cleared; the applied and expected
  schema revisions are shown; the actions this build deliberately does not offer are named
  where an operator would look for them.
- `web-rooms`: a room can be posted to as its own identity, which transmits and is therefore
  guarded and audited.

## Impact

- **New**: `src/sighop/web/routes/keys.py` (identity lifecycle) and the templates for it;
  new guarded actions in `web/guarded.py`; forms and templates under
  `web/templates/admin/`.
- **Modified**: `web/routes/admin.py` (create and per-key configuration), `web/routes/rooms.py`
  (create and post), `web/deps.py` if a shared form-refusal shape is wanted, `cli.py` only if
  a repository call proves to live in the command rather than the repository — which would
  be the finding, not the fix.
- **Unchanged**: `protocol/` and `net/` gain nothing. No new table, no migration, no new
  dependency. Every write goes through a repository call that already exists, so a rule the
  command line applies is applied here by being the same code.
- **Security posture**: unchanged in kind and larger in degree. This build still has no
  authentication, and this change adds the ability to *create* identities and *transmit* to
  a room through it. Both are guarded and audited on the pattern milestone 8 established;
  neither is safe on a routable address, which is what the startup warning already says.
