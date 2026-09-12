## Context

See proposal.md — Why. What shapes the approach is what milestone 8 already built and what
reading the command line's own implementations turned up.

Three things are already in place and this change is mostly a matter of using them:

- **The guarded-action pattern** (`web/guarded.py`): a confirmation view that mints a
  one-shot nonce, a POST carrying the provenance token plus that nonce, and a
  `web_guarded_action` event naming the action, target and outcome — refusals included.
  Three actions use it today; this adds two.
- **The request guard** (`web/guard.py`): every state-changing request carries a token this
  process issued into a page it served, and every request's `Host` must be one the interface
  was configured for. New writes inherit this by being writes.
- **The rule that a write is a repository call.** `web-admin`'s first requirement is that a
  change made in the browser goes through the same call the command line makes. That is
  cheap to keep only where the command actually *is* the repository call.

Reading the four commands this change mirrors showed that mostly it is, with two exceptions
worth naming before any code is written:

- **`sighop room post` is `MessageRepository.store` plus one length check.** It does not go
  through `RoomServer` at all: it stores a row, and whichever run is serving that room picks
  it up through its own push loop. So the panel's post is the same one call, and the
  `STORED_POST_TEXT_LEN` refusal — milestone 6's "refuse rather than corrupt where a refusal
  can be *heard*" — applies to a browser author for exactly the same reason it applies to a
  command-line one.
- **`sighop keys export` needs the sealing secret and writes a path.** It opens every stored
  seed with `SIGHOP_SECRET_KEY` and hands the chosen one to `create_keyfile`, which creates
  the file `O_EXCL|0600` in a single call. Neither the secret nor a filesystem path is
  something the panel currently has.

## Goals / Non-Goals

**Goals:**

- Every write the panel gains is the repository call the equivalent command makes, so a rule
  has one implementation and a refusal reads the same in both surfaces.
- Anything that reveals key material or transmits goes through the existing guarded-action
  pattern rather than a new one.
- What this build deliberately does not offer is visible in the interface, not only in a
  document.

**Non-Goals:**

- No new table, no migration, no new dependency, nothing added to `protocol/` or `net/`.
- No authentication. This change makes an unauthenticated surface larger and does not make
  it safer; milestone 9 is still where that is fixed.
- No second implementation of any validation. If a rule turns out to live in `cli.py` rather
  than in a repository, moving it is the fix and duplicating it is not.

## Decisions

### D1. `cli.py` hands the sealing secret to the interface, and export covers every stored identity

`keys export` opens seeds with `SIGHOP_SECRET_KEY`. The panel does not have it. Two ways to
close that:

- **Export only what this run loaded.** The run's identities are already open in memory —
  `reveal` uses exactly that — so no secret is needed. But a run loads only *enabled* stored
  entities, so a disabled identity could not be exported, and "export" would quietly mean
  something narrower in the browser than on the command line.
- **Pass the secret through `cli.py`.** It already reads it, and it is the one module that
  composes both sides.

Taking the second. The panel holding the sealing secret is not a new exposure: it already
holds opened seeds for every loaded identity and can reveal them behind a confirmation. What
it buys is that "export" means the same thing in both surfaces, which is this change's whole
premise. The secret reaches the interface the way everything else does — through `cli.py` at
wiring time — and `web/state.py` gains nothing, because a secret is not panel *state*.

### D2. An export is a download of `keyfile_document`, and the difference from a file is stated

`create_keyfile` does two things: it builds the document, and it writes it `O_EXCL|0600`.
`keyfile_document(identity, name, node_type)` is already separate and is what the file
contains, so the panel serialises that and returns it with a filename — byte-for-byte the
same identity as the command line's file.

**The protection is not the same and the interface says so.** The command line's guarantee is
a property of the `open` call: owner-only, in the same syscall that creates the file, with no
check-then-write window. A browser download has whatever the download directory gives it,
which is usually the same permissions as everything else there. Stating that is the honest
version of "an exported keyfile is an unencrypted seed protected only by its permissions",
which the identities page already says and which is *less* true of a download.

Rejected: making the panel write a server-side file at an operator-chosen path. That is a
path-traversal surface on an unauthenticated port, for the convenience of not using a
browser's own download.

### D3. Creating an identity generates the key in the process that stores it

`sighop keys new` writes a keyfile; `sighop keys import` seals a keyfile's seed into the
store. The panel has no filesystem to put a keyfile on, so **create** means generate and
seal in one step — `generate_identity()` then `EntityRepository.store`, which is what
`keys import` does with a key that came from a file.

That leaves `keys new`'s other job — producing a *file* — as the export path from D2, taken
after creating. So the browser's two steps are the command line's two commands, in the same
order, and neither surface gains a step the other lacks.

**Node hash collisions are the reason this is not just `generate_identity()`.** §3 rule 3 and
`EntityRegistry` already refuse two local identities sharing a node hash. Creation through
the panel passes the taken hashes to `generate_identity(avoid_node_hashes=…)` exactly as
`create_keyfile` does, so a panel-created identity cannot be the one that makes a later run
fail to start.

### D4. `room post` is the same store, the same limit, and a guarded action

The call is `MessageRepository.store`. The length check is `STORED_POST_TEXT_LEN`, refused
with the author's text preserved — the browser author is present and can shorten it, which is
the entire argument milestone 6 made for refusing here and truncating on the air.

It is guarded because it *reaches every member of the room*, which is a different act from
setting a retention bound. It is not guarded because it is destructive; nothing about a post
is reversible, and that is the point.

**A closed transmit gate does not refuse a post.** The post is a stored row and delivery is
the push loop's, which is subject to the gate like everything else. Refusing here would be
this surface inventing a rule the command line does not have — `sighop room post` stores
regardless — so the panel stores and *says* that nothing goes on the air until the gate
opens. Same for a room this run does not serve: stored, and stated.

### D5. Greeting records are edited through `bots/greeter.py`'s own key helpers

`greeted_key` / `greeted_public_key` / `GREETED_PREFIX` already live in `bots/greeter.py`,
which `web/` may import — it is a peer of `net/`, as `db/` is. Only the *rendering* of a
record's state lives in `cli.py`, and a rendering is allowed to differ between a terminal and
a table; the panel gets its own in `web/render.py`, beside the identity and meter views.

Nothing is moved out of `cli.py` for this. If a rule turns out to be there, that is a finding
and it moves — but the key convention, which is the part both surfaces must agree on, is
already in the right place.

### D6. `db upgrade` is absent and the absence is a page, not a silence

An operator who has just seen "the database is at 0003 and this code expects 0004" will look
for the button. Not finding one is ambiguous between "not built yet" and "deliberately not
offered". The schema page states which, and why: applying DDL from an unauthenticated port
would make schema migration reachable by anything that can route to it, and DESIGN.md §6
makes migration a deliberate act rather than a side effect of anything.

The same page gives the command, so the absence costs an operator one paste rather than a
search.

### D7. Per-key configuration replaces the JSON textarea, and the textarea goes

The panel takes a whole configuration object today and validates each key on the way in;
`sighop bot config` takes one key and one value and hands them to
`drivers.validate_config(driver, key, value)`, which routes the runtime's two reserved keys
to the runtime and the rest to the driver.

Per-key is the better form for the same reason it is the command line's: a refusal names the
key it is about, and a valid change is not lost because another key in the same object was
wrong. The textarea is replaced rather than kept beside it — two ways to edit one thing is
how the two disagree.

## Risks / Trade-offs

- **This makes an unauthenticated surface materially larger.** Creating identities and
  posting to rooms are new powers for anything that can reach the port → Not mitigated, and
  not mitigable before milestone 9. What is done: every one goes through the provenance
  token, the two heavy ones are guarded and audited, and the startup warning already states
  what a non-loopback bind exposes. The honest summary is that the loopback default is now
  carrying more weight than it was.
- **The panel process holds the sealing secret** (D1) → It already holds opened seeds, so the
  material exposure is unchanged; what changes is that a disabled identity's seed is now
  reachable too. The reveal and export actions are both guarded and both audited.
- **A download has no `0600`** (D2) → Stated in the interface at the point of export rather
  than in a document. There is no way to give a browser download owner-only permissions, so
  the mitigation is that the operator knows.
- **A panel-created identity could collide with a keyfile identity** → `avoid_node_hashes`
  covers the identities the run knows about; an identity created while another process holds
  a keyfile the panel has never seen could still collide, exactly as two `sighop keys new`
  runs could. Unchanged from the command line, and the collision is caught at startup.
- **`room post` from the panel writes a row that a *different* run will deliver** → Correct
  and stated. It is the same behaviour `sighop room post` has had since milestone 6; the
  panel is the first surface where somebody might expect otherwise, because it is showing
  them a running platform at the time.

## Migration Plan

None. No schema change, no data migration, no configuration change. The new pages are new
routes on an interface that is already opt-in; a run without `--web` is unaffected, and a run
with it gains pages and loses nothing.

Rollback is removing the routes; nothing they write is in a shape only they can read.
