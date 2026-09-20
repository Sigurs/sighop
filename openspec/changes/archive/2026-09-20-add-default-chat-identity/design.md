# Design

## Context

See `proposal.md` — Why. The constraints that shape the approach:

- **Identities are held twice.** The durable `entity` row (uuid `id`) and the run's loaded stubs
  (`page.state.adverts.stubs`, each an `EntityStub` with `entity_id: str`, `name` and
  `identity.public_key`). Composers address identities by public-key hex; the database can only
  reference a row id. The two are joined by `entity_id`.
- **Operators are durable, sessions are not.** `web_user` rows survive restarts; `Session`
  (`web/auth.py`) lives in memory, ends on restart, and already revalidates against its account row
  at most once a minute, carrying `password_set_at` from that row for exactly this reason.
- **The database may be degraded.** `web-chat` already requires channel posting and DM sending to
  continue through an outage, so nothing in the send path may depend on a read that can fail.
- **`web-auth` keeps account operations out of the browser.** The carve-out for a preference is
  written into the `web-auth` delta rather than assumed.
- The panel's page context is assembled synchronously in `Panel.context`; the contacts page
  (`web/app.py`) performs no database read today.

## Goals / Non-Goals

**Goals:**

- One place resolves "which identity is this operator chatting as", used by the channel composer,
  the conversation composer and the contact list alike.
- No database read added to rendering a page, and no send path that can be blocked by a failed read.
- A removed identity cannot linger as a default that silently posts as something else.

**Non-Goals:**

- A station-wide default, or a default per channel or per contact.
- Managing the preference from the command line, or exposing another operator's preference anywhere.
- Changing what is transmitted, or how a channel post's sender name is claimed.
- Reworking the conversation URL shape: `/chat/{entity_key}/{peer_key}` stays as it is.

## Decisions

### D1 — The default is a nullable column on `web_user`, not a new table

Migration `0009` adds `default_entity_id uuid NULL REFERENCES entity(id) ON DELETE SET NULL`.

*Why:* one preference belonging one-to-one to an account is the account's own column, and the
foreign key makes "an identity's removal clears every default naming it" a property of the schema
rather than a cleanup step that can be forgotten — the same reasoning `entity.sealed_private_key`
rests on. A `web_user_preference` key/value table would carry a second write path and an
unconstrained string where a reference belongs.

*Alternative rejected:* a flag on `entity` (station-wide — contradicts per-operator), and a cookie
(does not follow the operator to another machine, and holds station state in the browser).

*Note on the delete path:* identity removal goes through the entity repository, so the cascade runs
in the database. No route clears the column by hand.

### D2 — The session carries the default; the row is its durable home

`Session` gains `default_entity_id: str | None`. Sign-in reads it from the account row; the existing
revalidation refreshes it, so a default set in one browser reaches that operator's other sessions
within a minute, exactly as a password change already does. Setting it writes the row and updates
the live session in place.

*Why:* rendering a composer must not await the database — it is on every chat page, and the
contacts page currently reads nothing. It also gives the degraded-database behaviour the spec asks
for: a session keeps applying the default it already knows, while setting a new one is a write and
is refused with a reason.

*Alternative rejected:* reading `web_users.get(username)` per request. Correct but puts a failable
read in front of every composer, and would make an outage change which identity a post is composed
as — the one thing the spec forbids.

### D3 — `Account`/`AccountStore` grows a read, not a write

`Account` (protocol in `web/auth.py`) gains `default_entity_id`; `AccountStore` is left with its one
write (`add_first`). The set/clear route writes through `page.persistence.web_users`, which the panel
already holds.

*Why:* design D16's "the panel's account store has one write" is about authentication holding no
account-management power. A preference write does not belong behind the authenticator.

### D4 — Resolution happens once, in one helper

A single function — `default_identity(stubs, default_entity_id)` in `web/render.py` — maps the
session's `default_entity_id` to a loaded stub by `entity_id`, returning `None` when this run does
not hold it. The channel composer, the conversation composer and the contacts page all call it.

*Why:* the spec's "a default this run cannot use is not silently substituted" is one rule, so it is
one lookup with one fallback. Matching by `entity_id` rather than by name or public key means a
renamed identity keeps being the default and a recreated one (new key, new row) does not inherit it.

*Placement:* `render.py` rather than `routes/chat.py`, because `web/app.py`'s contacts route needs
it and a page route importing a sibling route module for a view decision is the import direction
this codebase avoids. It takes the stub list and the stored id rather than a `Panel`, so the route
that has no panel can call it too.

### D4a — A stored identity is adopted under its row id

`Runtime._adopt_entity` registers a stored identity with `entity_id=str(record.id)`; identities from
a keyfile or generated for the run keep their name, as they always have.

*Why (found during implementation):* `add_identity` defaults `entity_id` to the identity's *name*,
and a rename rewrites it (`AdvertScheduler.rename`). A default stored as a row id would therefore
never have matched a loaded identity in a real run, and D4's rename-safety was false. Giving exactly
the identities that have a row their row id as `entity_id` makes the reference the schema already
holds the one the panel matches on. `entity_id` is an opaque per-identity key elsewhere (advert
logs, DM in-flight keys, feed counters), so the change is a value change, not a contract change.

### D4b — Only a stored identity can be kept as a default

The chat-as control lists the identities whose `entity_id` is a row id; a submission naming any
other loaded identity is refused with its own reason. Every loaded identity stays selectable for a
single message in both composers.

*Why:* the preference outlives the run and the foreign key requires a row, so an identity generated
for this run or loaded from a keyfile cannot be one. Offering a choice that could only be refused
would be a worse interface than not offering it, and the refusal remains as the backstop for a
submission that did not come from the rendered form.

### D5 — The DM composer selects the identity, and the POST redirects to that identity's conversation

`chat/conversation.html` gains a `send as` select beside the existing text area, preselected with the
resolved default (falling back to the identity in the URL). `POST /chat/{entity_key}/{peer_key}`
accepts an `identity` form field; the send is composed as that identity and the response redirects to
`/chat/{chosen}/{peer_key}`.

*Why:* conversations are keyed by (identity, contact), so sending as another identity belongs in that
identity's conversation — the redirect makes the page the operator lands on agree with what was sent,
with no client-side script. The URL shape is unchanged, so every existing link, test and the
partial-refresh endpoint keep working; the `entity_key` in the path stays the authority on which
history is shown.

*Alternative rejected:* a peer-only URL (`/chat/{peer_key}`) with the identity purely a form field.
Cleaner on paper, but it re-keys every conversation link, the refresh endpoint and the chat index for
no behavioural gain.

### D6 — The contact list links once, through the resolved default

`contacts.html`'s message column renders one link built from `default_identity(...)`, or from the
run's single identity when exactly one is loaded. Where neither applies — several identities loaded
and no default — the row carries a link to `/chat`, where an identity to chat as is chosen, because
a conversation URL cannot be built without an identity. The page's existing "no identity to send as"
empty state covers a run holding none, and is kept.

*Why:* the per-identity fan-out is what the change removes. Because `web/app.py`'s contacts route has
no `Panel`, the resolution helper must take the session and the stub list rather than a `Panel` — it
reads no database, so that is a free constraint to honour.

### D7 — The preference is set from the chat page

A `POST /chat/identity` with the signed-in session's CSRF token sets or clears the default, taking an
identity's public-key hex or the empty string, and redirects back to where it was submitted from
(chat index, channel page or conversation). The chat index shows the current default and the control
that changes it.

*Why:* it is a chat preference, so it is set where chatting happens and not on `/admin/identities`,
which is station configuration every operator shares. No password step: it grants nothing, changes
no key material and transmits nothing, so it is not a guarded action under `web-auth`'s D7.

### D8 — Refusals keep their current shape

"An identity must be chosen" stays the refusal when nothing is selected and no default resolves, with
the author's text handed back, exactly as the channel composer refuses today. The new refusal is the
degraded-database one on setting the preference.

## Risks / Trade-offs

- **An operator posts as the wrong identity because the default was preselected** → the select is
  always visible and always shows the identity by name, on both composers; nothing is composed from a
  hidden value, and the channel page already states that the sender name is an unauthenticated claim.
- **A default set in another browser takes up to a minute to reach this session** → same window the
  platform already accepts for an account change (`web-auth`); the setting browser updates
  immediately.
- **The contact list loses direct access to non-default identities** → the conversation page's new
  `send as` select reaches every loaded identity in one step, and the chat index still lists every
  (identity, contact) conversation that exists.
- **A migration touching `web_user` runs against a table holding operator accounts** → the column is
  nullable with no default value and no data is rewritten; downgrade drops it.
- **`rename-to-siggynet` moves every path named here** → nothing in this design depends on the
  package name; whichever change lands second is a path rebase.

## Migration Plan

1. `0009_default_chat_identity` adds `web_user.default_entity_id`, nullable, referencing
   `entity(id)` with `ON DELETE SET NULL`. No backfill: no operator has a default until they set one.
2. Deploy is ordinary — an older build ignores the column, so the migration can run ahead of the code.
3. Rollback drops the column (`downgrade`), losing only the preferences; no other state depends on it.

## Open Questions

None. Two were resolved during implementation and are recorded above as D4a and D4b.
