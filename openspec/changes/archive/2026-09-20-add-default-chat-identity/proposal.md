# Proposal

## Why

Every channel post and every conversation starts by picking an identity again. The channel
composer opens on "choose an identity" and refuses a post without one; the contact list offers one
link per loaded identity, so a run with three identities draws three links on every contact row.
An operator who almost always chats as the same identity pays that choice on every page, and the
contact list grows a column that scales with the number of identities rather than with the number
of contacts.

## What Changes

- Each operator account gains a **default chat identity**: the identity its channel posts and
  direct messages are composed as unless another is chosen. It is per operator, not per station —
  two operators signed into the same run can hold different defaults.
- The default is set and cleared from the chat interface, and stored on the operator's account row
  so it survives sign-out, a browser change and a restart.
- The channel composer preselects the default in its existing `post as` select. Any other loaded
  identity is still selectable per post; nothing is locked.
- The direct-message composer gains the same `send as` select, preselected with the default. Posting
  as another identity lands in that identity's conversation with that contact.
- **BREAKING (interface)** the contact list's message column becomes **one** conversation link per
  contact instead of one per loaded identity. The link opens the conversation as the operator's
  default identity, or as the single loaded identity when only one is loaded.
- An identity that is removed, or that this run has not loaded, stops being anyone's effective
  default: the composer falls back to "choose an identity" and says so, rather than posting as
  something else.

## Capabilities

### New Capabilities

None. The behaviour belongs to the existing chat and authentication capabilities.

### Modified Capabilities

- `web-chat`: a default identity is preselected in the channel and direct-message composers; the
  contact list offers one conversation link per contact rather than one per identity; the
  direct-message composer chooses the sending identity.
- `web-auth`: the operator account row carries the account's default chat identity, and setting it
  is an interface preference rather than one of the account operations the browser must not offer.

## Impact

- **Schema**: migration `0009` adds a nullable `default_entity_id` to `web_user`, referencing
  `entity(id)` with `ON DELETE SET NULL` so removing an identity clears every default naming it.
- **Code**: `src/sighop/db/models.py` (`WebUser`), `src/sighop/db/repositories.py`
  (`WebUserRepository`), `src/sighop/web/deps.py` (the panel resolves the signed-in operator's
  default), `src/sighop/web/routes/chat.py` (channel and DM composers, a route that sets the
  default), `src/sighop/web/templates/chat/*.html` and `src/sighop/web/templates/contacts.html`.
- **Degraded database**: the default cannot be read during an outage. Channel posting and DM
  sending continue as they do today, with the composer falling back to an unselected list — the
  outage must not make a composer refuse work it would otherwise accept.
- **No protocol change**: nothing new is transmitted, and a channel post's sender name remains the
  unauthenticated claim it already is.
- **Overlaps `rename-to-siggynet`**, an in-flight change that moves `src/sighop` to `src/siggynet`.
  Paths here are written against `src/sighop`; whichever change lands second rebases onto the other.
