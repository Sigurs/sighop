# Tasks

## 1. Shared hop resolution

- [x] 1.1 Make `resolve_hop`'s `advertiser` optional (`bytes | None = None`, `None` excludes nobody) in `src/sighop/webhooks/events.py`; verify `uv run pytest tests/test_webhooks_render.py tests/test_webhooks_dispatcher.py` still passes and add a unit case showing an unexcluded single match is named

## 2. Chat path lines

- [x] 2.1 In `src/sighop/web/routes/chat.py`, parse recorded paths once into hop tuples (direct = empty), and build the copy text as `"{hash} {label}"` hops joined by ` → ` using `resolve_hop(..., page.state.contacts)` and `PathHop.label`, flattening control characters in names to a space; verify with the updated test in 3.1
- [x] 2.2 Build the hover from the same parse as hash-only hops joined by ` → `, keeping `direct`, copy count and the "hide control when every path is direct / none / outbound" rule; verify with 3.1

## 3. Tests

- [x] 3.1 Update `test_a_received_message_offers_its_paths_to_copy_and_nothing_else_does` in `tests/test_web_chat.py` for the new copy text (`a3 <unknown> → 28 <unknown> → 9e <unknown>` / `direct` / …) and hover separator; verify it passes
- [x] 3.2 Add a test with contacts in the stub state covering a named match, `<ambiguous>` (two keys sharing the prefix), an unnamed single match (key prefix), a multi-byte hash size, and a name containing a newline; verify the copied `data-copy` text matches the spec scenarios
- [x] 3.3 Add a test that a contact added after the message was offered is named on the next `GET /chat/channel/{id}/messages`; verify it passes

## 4. Verification

- [x] 4.1 Run lint and the full test suite (`./build.sh` gates or `uv run ruff check . && uv run pytest`) and confirm all pass
- [ ] 4.2 Manual: open a channel with a routed received message, click the copy control, paste, and confirm the Discord-style lines
