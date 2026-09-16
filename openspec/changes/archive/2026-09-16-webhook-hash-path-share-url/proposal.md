## Why

Discord webhook messages show a node's hash as its first key byte only, but most MeshCore nodes now
flood with 2- or 3-byte path hashes, so the 1-byte value is both ambiguous and not what operators
see in their apps. The message also says nothing about how the sighting reached us, and shows
only a 16-character key prefix, which cannot be copied into an app to add the node. A located node's
coordinates are in the JSON event but nowhere in the Discord message, so an operator cannot see where
a new repeater is without leaving Discord; the SNR field takes a column nobody acts on in a chat
message, and stays in JSON for anyone who does.

## What Changes

- The Discord message shows the node hash at the hash size of the advert that was heard (1, 2 or 3
  bytes, from the packet's path length byte) instead of always 1 byte.
- The Discord message shows the reception's path: each hop's hash in the order the advert travelled,
  labelled with the contact name when exactly one known contact matches the hop, `<unknown>` when
  none does, and `<ambiguous>` when several do. A zero-hop reception says it was heard directly.
- The Discord message shows the node's full public key (64 hex characters, as copyable inline
  code) instead of the abbreviated 16-character prefix.
- The Discord message shows the node's advertised position as a Google Maps link labelled with the
  coordinates, or `not advertised` when the advert carried none.
- The Discord message drops the SNR field.
- The JSON format gains additive fields within schema version `1`: the sized node hash and hash
  size, the reception path with resolved hop names, and a maps URL on an advertised position. The
  existing `node_hash` field keeps its 1-byte meaning, `snr_db` stays, and the full public key is
  already present.
- Sample (test) events carry a sample path and a sample position so both formats can be checked end
  to end.

## Capabilities

### New Capabilities
<!-- none -->

### Modified Capabilities
- `webhooks`: the JSON event requirement gains the hash size, sized hash, path and position maps
  URL fields; the Discord format requirement shows the sized hash, the full public key, the resolved
  path and the position as a maps link, drops SNR, and keeps path names escaped like other advert
  content; the sample event carries a sample path and position.

## Impact

- `src/sighop/webhooks/events.py`: `WebhookEvent` gains hash size and path hops with resolved
  names; `event_from_observation` and `sample_event` fill them, and `sample_event` gains a position.
- `src/sighop/webhooks/render.py`: Discord fields (location added, SNR removed) and JSON additive
  fields, plus the maps URL helper both formats use.
- `src/sighop/webhooks/dispatcher.py` and `src/sighop/runtime.py`: the dispatcher is given a way to
  resolve hop hashes against the contact store at event time.
- `src/sighop/net/contacts.py`: a prefix lookup for multi-byte hop hashes.
- Tests: `tests/test_webhooks_render.py`, `tests/test_webhooks_dispatcher.py`.
- No database, CLI or web form changes. No new dependencies.
