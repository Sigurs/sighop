# sighop

Host several virtual [MeshCore](https://meshcore.io) nodes on one LoRa radio.

sighop drives a single modem running the MeshCore **KISS modem firmware**, which is a plain TNC with no routing or encryption. On top of it, sighop runs its own Python implementation of the MeshCore protocol stack. That lets one antenna host 2–5 independent entities (room servers, companions and bots), each with its own identity, keys and state.

## Features

- **Full protocol stack in Python**: packet and payload codecs, Ed25519/X25519 crypto, path learning, duplicate filtering.
- **Airtime-aware TX scheduler**: one packet in flight at a time, gated on `TxDone`, and held to a duty-cycle ceiling (10% by default, the EU 868 limit).
- **Receive-only by default**: nothing is transmitted until an operator opens the transmit gate.
- **Virtual entities**:
  - room servers with ACLs and history
  - companions for DMs and channels (Public, hashtag and PSK channels)
  - pluggable bots, e.g. a greeter
- **Web panel**: server-rendered FastAPI + HTMX pages with no SPA build.
  - admin and key management
  - live packet feed and a duty-cycle dashboard
  - contacts with learned paths and SNR/RSSI
  - room browsing
  - a chat client
  - repeater metrics with history charts
- **Webhooks, packet log, JSONL capture and replay.** Webhooks name an advertised position's neighborhood, city and country offline.
- **Encrypted at rest**: entity seeds are sealed under `SIGHOP_SECRET_KEY`.

Out of scope for now: repeating or forwarding, multipart payloads, bridging to other protocols, and more than one modem.

## Requirements

- A LoRa board flashed with the MeshCore KISS modem firmware (developed against Heltec V3/V4)
- Python 3.13 and [uv](https://docs.astral.sh/uv/)
- PostgreSQL (the driver is `postgresql+asyncpg`)

## Quick start

```bash
cp .env.example .env.dev          # then fill it in; every variable is documented there
# at minimum:
#   DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/sighop
#   SIGHOP_SECRET_KEY=$(openssl rand -base64 32)
#   SIGHOP_MODEM=/dev/serial/by-id/usb-...

uv sync
uv run --env-file .env.dev sighop
```

sighop is configured only through environment variables. On startup it applies any pending migrations, then serves the web panel (port 8080 by default).

> **Back up `SIGHOP_SECRET_KEY`.** If you lose it, every stored identity becomes unrecoverable.

## Docker

```bash
./build.sh                        # lock, format, lint, types, test, image, smoke, replay, scan
UID=$(id -u) GID=$(id -g) DIALOUT_GID=$(stat -c %g /dev/ttyUSB0) \
  SIGHOP_MODEM=/dev/serial/by-id/usb-... \
  DATABASE_URL=... SIGHOP_SECRET_KEY=... \
  docker compose up -d
```

The container runs as an arbitrary UID on a read-only root with every capability dropped. The modem is passed through as `/dev/modem`. CI builds arm64 images and pushes them to GHCR on every push to `master`.

## Development

```bash
uv run ruff check && uv run mypy
SIGHOP_TEST_DATABASE_URL=postgresql+asyncpg://... uv run pytest
```

The test suite requires a database. Each run works in its own throwaway schema. Protocol tests run against a generated synthetic corpus in `tests/corpus/`.

The dev container (`.devcontainer/`) has the full toolchain.

Place names come from a gazetteer bundled in `src/sighop/geo/`. Regenerate it from GeoNames with `uv run python scripts/build_places.py`.

## Documentation

- [DESIGN.md](DESIGN.md): architecture, TX scheduler, cryptography, persistence and deployment
- [openspec/specs/](openspec/specs/): one behavioural spec per capability

## Data

Place names are from [GeoNames](https://www.geonames.org), licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
