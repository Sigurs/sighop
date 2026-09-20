# Proposal

## Why

Every packet sighop originates with an empty path (flood adverts, zero-hop adverts, channel
posts, flooded direct messages and acknowledgements, room login/reply floods) is hardcoded to a
1-byte path hash. Repeaters append their own hash at whatever width the origin chose
(`Mesh.cpp:349`), so the hops recorded on our floods, and the routes peers learn back to us, are
1 byte per hop and collide at 1 in 256. MeshCore firmware accepts 1–3 bytes at origin
(`Mesh::sendFlood`, `Mesh.cpp:642`), and the mesh sighop runs on already carries 3-byte paths.
Operators need one node-wide setting for this width, defaulting to 3.

## What Changes

- New environment variable `SIGHOP_PATH_HASH_SIZE`, accepting `1`, `2` or `3`, default `3`.
  It is node-wide, not per identity. Any other value fails startup with a configuration error
  that names the variable and the accepted values.
- Every packet sighop originates with an empty path uses that width in the header's path-length
  byte. This covers flood and zero-hop adverts, channel posts, direct messages and
  acknowledgements sent by flood, and room-server PATH/reply floods.
- Packets that follow a learned, non-empty route keep that route's own hash size. The path bytes
  are fixed at that width, so re-encoding them at another width would corrupt the route.
- `sighop run` startup output states the path hash size in force.
- **BREAKING** (on-air behaviour): with no variable set, originated floods change from 1-byte to
  3-byte path hashes. Set `SIGHOP_PATH_HASH_SIZE=1` to keep the previous behaviour.

## Capabilities

### New Capabilities
- `path-hash-size`: the node-wide path hash width for originated packets. Covers how it is
  configured, its default, its validation, and which outbound packets it governs and which it
  does not.

### Modified Capabilities
- `runtime-cli`: `sighop run` startup output reports the path hash size in force.

## Impact

- `src/sighop/config.py`: new variable, parsing and validation on `Config`.
- `src/sighop/cli.py` / `src/sighop/runtime.py`: the value is carried into `RuntimeConfig` and
  handed to the advert scheduler, channel service, direct messenger and room servers, and
  rendered at startup.
- `src/sighop/net/adverts.py`, `net/channels.py`, `net/dm.py`, `net/room.py`: packet builders and
  `Route(flood=True)` construction take the configured width instead of the literal `1`.
- `compose.yaml` and `.env.example`: pass the variable through and document it.
- Tests that assert on the encoded bytes of originated packets (these currently assume a 1-byte
  hash).
- No database migration. No web UI change.
