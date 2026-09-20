# Tasks

## 1. Configuration

- [x] 1.1 In `src/sighop/config.py`, add `PATH_HASH_SIZE_VARIABLE = "SIGHOP_PATH_HASH_SIZE"`, `DEFAULT_PATH_HASH_SIZE = 3` and `parse_path_hash_size(value: str | None) -> int`. The parser returns 3 for None or blank and raises `ConfigError` for anything other than 1, 2 or 3 (design D1). Verify with new unit tests in the config test module covering unset, blank, `1`/`2`/`3`, `" 2 "`, `0`, `4`, `three` and `-1`. Each error message must name the variable, the value and the accepted values.
- [x] 1.2 Add `path_hash_size_raw` to `Config`, read in `from_environment` (blank counts as unset). Add a `path_hash_size()` method returning `(size, from_env)`, and extend `as_json` with `path_hash_size`. Verify with tests: an invalid value does not make `from_environment` raise, but `path_hash_size()` does.

## 2. Packet builders and services

- [x] 2.1 `net/adverts.py`: make `build_advert_packet` take a required keyword `path_hash_size` and encode it for both flood and zero-hop. `AdvertScheduler` gains `path_hash_size: int = DEFAULT_PATH_HASH_SIZE` and passes it at its three call sites. Verify with `tests/test_adverts.py`: decode the packet and assert `hash_size == 3` by default, `== 2` when configured, and hop count 0 with an empty path.
- [x] 2.2 `net/channels.py`: make `build_channel_packet` take a required keyword `path_hash_size`. `ChannelMessenger` gains the defaulted attribute and passes it. Verify with a test in `tests/test_channels.py` asserting the decoded post's `hash_size`, and check that own-post echo recognition still matches, since it is keyed on payload.
- [x] 2.3 `net/dm.py`: give `choose_route` a required keyword `path_hash_size`, used only on the flood branch (design D4). `DirectMessenger` gains the defaulted attribute and passes it at L778 and L1155. Verify with tests in `tests/test_dm.py`: a flood send and its ACK decode with the configured width, and a learned 1-byte 2-hop route still encodes hash size 1 when the setting is 3 (spec scenario).
- [x] 2.4 `net/room.py`: `RoomServer` gains the defaulted attribute. The flood routes at the login PATH return, the request PATH return and `_route_to` use `Route(flood=True, hash_size=self.path_hash_size)`. `_route_for_record` stays unchanged. Verify with a room test: a PATH return to a member with no known route decodes with the configured width.
- [x] 2.5 Update the remaining `choose_route` callers (`runtime.py:910`, `web/routes/chat.py:647,719`) to pass the runtime's value. Verify with `uv run pyright` (or the project's type check) reporting no missing-argument errors.

## 3. Runtime wiring and output

- [x] 3.1 Add `path_hash_size` and `path_hash_size_from_env` to `RuntimeConfig`. Resolve them in `cli._run_config` via `Config.from_environment().path_hash_size()`. Pass the value to `AdvertScheduler`, `ChannelMessenger`, `DirectMessenger` and each `RoomServer` in `runtime.py` (design D2, D3). Verify with a CLI test: `SIGHOP_PATH_HASH_SIZE=4 sighop run --replay <capture>` exits non-zero with the configuration error before the pipeline starts.
- [x] 3.2 Extend `render_run_startup` in `monitor/render.py` with the path-hash-size line, printed whether the gate is open or closed (design D6), and pass the values from `Runtime._print_startup`. Verify with tests in `tests/test_render.py` for the default and from-env variants and for 1 byte versus 2–3 bytes.

## 4. Deployment and docs

- [x] 4.1 `compose.yaml`: add `SIGHOP_PATH_HASH_SIZE: "${SIGHOP_PATH_HASH_SIZE:-}"` to the service environment. `.env.example`: document the variable, its values, the default of 3 and `=1` for the old behaviour. Verify that `docker compose config` renders with the variable unset and with it set.

## 5. Verification

- [x] 5.1 Update existing tests that assert on originated packet bytes (`tests/test_first_transmit.py`, `tests/test_adverts.py`, `tests/test_channels.py`, `tests/test_dm.py`, `tests/test_room_render.py`) to the new default, or pin them with `path_hash_size=1` where the fixture bytes matter. Verify with the full suite plus `ruff check` and the type check passing.
- [x] 5.2 Run `openspec validate add-path-hash-size-setting --strict` and confirm it passes.
