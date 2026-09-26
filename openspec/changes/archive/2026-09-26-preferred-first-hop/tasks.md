# Tasks

## 1. Route resolution in the path store

- [x] 1.1 Add `RouteRewrite` enum and `ResolvedPath` to `src/sighop/net/paths.py`; add `preferred_first_hop` (setter validating a 32-byte key or None) and a `prepend_overflow` counter surfaced in `PathStore.as_json`; verify with a unit test in `tests/test_paths.py` that setting/clearing works and `as_json` carries the counter
- [x] 1.2 Implement `PathStore.resolve(key)` per design D2 (unset, destination-is-preferred, candidate-already-through-preferred, shortened, prepended, overflow fallback), comparing hops at each candidate's width; verify with `tests/test_paths.py` cases for every `route-preference` scenario, including widths 1/2/3, `11 ab 22` → `ab 22`, 21 hops at width 3 → unchanged + counter, and `lookup()` still returning most-recently-confirmed
- [x] 1.3 Add `resolve_for_contact(paths, public_key, node_hash)` (public key first, node hash second, ambiguity kept); verify with a test that an ambiguous hash-keyed route stays ambiguous after prepending

## 2. Senders use the one resolution

- [x] 2.1 Add `rewrite` to `Route` in `src/sighop/net/dm.py`, extend `label` with `via-pref`, and switch `choose_route` to `resolve_for_contact`; verify in `tests/test_dm.py` that with no preference packets are byte-identical to before, that a zero-hop contact is sent `DIRECT` h1 through the preferred repeater with the one-hop ack timeout, and that no-route flood/refusal behaviour is unchanged
- [x] 2.2 Add `route_rewrite` to the DM send and ack structured logs; verify a test asserts the field on a prepended send
- [x] 2.3 Switch `RoomServer._known_route_to` in `src/sighop/net/room.py` to `resolve_for_contact`, leaving `_route_for_record` and `PATH` return bodies untouched; verify in a room test that a reply to a zero-hop member goes through the preferred repeater and a path return still echoes the inbound path
- [x] 2.4 Verify `src/sighop/net/collect.py` `_route` inherits the preference through `choose_route` and that polling the preferred repeater itself is not rewritten; add both cases to `tests/test_collect.py`

## 3. Storage

- [x] 3.1 Add `RoutePreferenceRow` (one row, `id = 1`, nullable 32-byte `preferred_first_hop`, `updated_at`) to `src/sighop/db/models.py` and migration `alembic/versions/0014_route_preference.py` (down-revision `0013`); verify `tests/test_db_schema.py` passes with the new head and upgrade/downgrade round-trips
- [x] 3.2 Add `RoutePreferenceRepository` (`get`, `save`) to `src/sighop/db/repositories.py` and hang it on `Persistence`; verify with a repository test that absent row reads as None, save/clear round-trips, and a non-32-byte key is refused

## 4. Runtime wiring

- [x] 4.1 In `src/sighop/runtime.py`, read the stored preference after paths are restored and before traffic, and set it on `pipeline.paths`; verify with a runtime test that a stored preference is in force for the first send after start
- [x] 4.2 Confirm replay (`src/sighop/replay.py`) runs with no preference; verify the replay tests still pass unchanged

## 5. Web

- [x] 5.1 Add `POST /system/route-preference` to `src/sighop/web/routes/system.py`: accept a hex key or blank, refuse keys not among repeater contacts with the reason, store, and on success set it on the live `PathStore`; verify in `tests/test_web_admin.py` choose, clear, unknown-key refusal, failed-write-leaves-memory-unchanged
- [x] 5.2 Add the section to `src/sighop/web/templates/system.html`: repeater select (none first), statement that it applies to every DIRECT send from every identity, the repeater in force (name or abbreviated key if forgotten), its zero-hop status with last-confirmed time and SNR, and the not-heard-directly warning; verify with a rendering test for both the heard and not-heard cases
- [x] 5.3 Switch `_route_for` in `src/sighop/web/render.py` to `resolve_for_contact`, add the rewrite flag to `RouteView`, and mark rewritten routes in the contact table template; verify in `tests/test_web_dashboard.py` that a zero-hop contact shows as one hop through the preferred repeater with the marker, and shows zero-hop with no preference

## 6. Finish

- [x] 6.1 Run lint (`uv run ruff check` and `uv run ruff format --check`), type check (`uv run mypy src`) and the full test suite (`uv run pytest`); both pass
