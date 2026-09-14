## Why

Discord webhook messages show a node's hash as its first key byte only, but most MeshCore nodes now
flood with 2- or 3-byte path hashes, so the 1-byte value is both ambiguous and not what operators
see in their apps. The message also says nothing about how the sighting reached us, and shows
only a 16-character key prefix, which cannot be copied into an app to add the node.

## What Changes

- The Discord message shows the node hash at the hash size of the advert that was heard (1, 2 or 3
  bytes, from the packet's path length byte) instead of always 1 byte.
- The Discord message shows the reception's path: each hop's hash in the order the advert travelled,
  labelled with the contact name when exactly one known contact matches the hop, `<unknown>` when
  none does, and `<ambiguous>` when several do. A zero-hop reception says it was heard directly.
- The Discord message shows the node's full public key (64 hex characters, as copyable inline
  code) instead of the abbreviated 16-character prefix.
- The JSON format gains additive fields within schema version `1`: the sized node hash and hash
  size, and the reception path with resolved hop names. The existing `node_hash` field keeps its
  1-byte meaning; the full public key is already present.
- Sample (test) events carry a sample path so both formats can be checked end to end.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `webhooks`: the JSON event requirement gains the hash size, sized hash and path fields; the
  Discord format requirement shows the sized hash, the full public key and the resolved path, with
  path names escaped like other advert content.

## Impact

- `src/sighop/webhooks/events.py`: `WebhookEvent` gains hash size and path hops with resolved
  names; `event_from_observation` and `sample_event` fill them.
- `src/sighop/webhooks/render.py`: Discord fields and JSON additive fields.
- `src/sighop/webhooks/dispatcher.py` and `src/sighop/runtime.py`: the dispatcher is given a way to
  resolve hop hashes against the contact store at event time.
- `src/sighop/net/contacts.py`: a prefix lookup for multi-byte hop hashes.
- Tests: `tests/test_webhooks_render.py`, `tests/test_webhooks_dispatcher.py`.
- No database, CLI or web form changes. No new dependencies.
