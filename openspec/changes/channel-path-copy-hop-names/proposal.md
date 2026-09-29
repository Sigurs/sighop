# Proposal

## Why

A channel message's copied paths are bare hop hashes (`a3->28->9e`), so an operator
pasting them has to look every hash up by hand. The Discord new-node notification already
resolves hops to names (`` `afc6` Kurala Hill repeater → `bed0` Glorfalas ``); the chat copy
should say the same thing in the same way.

## What Changes

- Each copied path line names every hop the way the Discord notification does: hash in
  lowercase hex, a space, then its label — the contact's name for a single named match,
  `<unknown>` for no match, `<ambiguous>` for several, the key prefix for a single unnamed
  match — hops joined by ` → `. Example:
  `1337 <unknown> → 0c90 <unknown> → 5e3a <unknown> → afc6 Kurala Hill repeater → bed0 Glorfalas`.
- Hops are resolved against the station's contacts when the conversation is rendered, so a
  node learned after the message arrived is named on the next refresh. Nothing new is stored.
- One path per line, arrival order, `direct` for a zero-hop copy, and when the control is
  shown are unchanged.
- The hover on the received state keeps its compact hex form (hashes only) but uses the
  same ` → ` separator, so the two read alike.
- **BREAKING** (copy format only): anything parsing the old `->`-joined bare-hash text must
  change; nothing inside sighop does.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `web-chat`: the "received channel message's paths can be read and copied" requirement
  changes the copied line format to hash + resolved label joined by ` → `, and the hover
  separator to ` → `.

## Impact

- `src/sighop/web/routes/chat.py` — `_path_lines` / `_path_copy` / `_channel_state` build
  lines from resolved hops using `page.state.contacts`.
- `src/sighop/webhooks/events.py` — `resolve_hop` gains an optional advertiser (a channel
  sender has no verified key to exclude); shared label logic stays in `PathHop.label`.
- `tests/test_web_chat.py` — path-copy test updated for names; new cases for unknown,
  ambiguous and unnamed hops.
- No migration, no new dependency, no change to the Discord or JSON webhook output.
