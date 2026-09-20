# Tasks

## 1. Schema

- [x] 1.1 Add migration `alembic/versions/0009_default_chat_identity.py` adding `web_user.default_entity_id` (uuid, nullable, `ForeignKey("entity.id", ondelete="SET NULL")`), with a module docstring saying what a downgrade costs as 0005 and 0008 do; verify `alembic upgrade head` then `downgrade -1` then `upgrade head` leaves no leftover objects, mirroring `test_0005_upgrade_downgrade_upgrade_leaves_no_leftover_objects` in [tests/test_web_user_schema.py](tests/test_web_user_schema.py)
- [x] 1.2 Add `default_entity_id` to the `WebUser` model in [src/sighop/db/models.py](src/sighop/db/models.py) with a docstring line saying the column is a preference and that `ON DELETE SET NULL` is what clears it when an identity is removed; verify `uv run pytest tests/test_web_user_schema.py`
- [x] 1.3 Update the head-revision assertions to `0009` in [tests/test_db_schema.py](tests/test_db_schema.py) (`test_the_migration_chain_has_one_head_the_code_expects`); verify `uv run pytest tests/test_db_schema.py`
- [x] 1.4 Add a schema test that removing an `entity` row clears the `default_entity_id` of every `web_user` naming it, in [tests/test_web_user_schema.py](tests/test_web_user_schema.py); verify it passes against the test schema

## 2. Storage and session

- [x] 2.1 Carry `default_entity_id` on `WebUserRecord` and add `set_default_identity(username, entity_id | None) -> Outcome[bool]` to `WebUserRepository` in [src/sighop/db/repositories.py](src/sighop/db/repositories.py), normalising the username as every other method there does and refusing an entity id no `entity` row holds; verify new cases in [tests/test_web_user_repository.py](tests/test_web_user_repository.py) cover set, clear, unknown account and unknown entity
- [x] 2.2 Add `default_entity_id` to the `Account` protocol and to `Session` in [src/sighop/web/auth.py](src/sighop/web/auth.py) (design D2, D3), read it at sign-in, and refresh it in the existing revalidation alongside `password_set_at`; verify `uv run pytest tests/test_web_auth.py tests/test_web_auth_routes.py`
- [x] 2.3 Add a test that a default set elsewhere reaches an existing session at revalidation and that a revalidation the database could not answer leaves the session's default as it was; verify it passes in [tests/test_web_auth.py](tests/test_web_auth.py)

## 3. Resolving the default

- [x] 3.1 Add the resolution helper (design D4) that maps a session's `default_entity_id` to a loaded stub by `entity_id` and returns `None` when this run does not hold it, taking the session and the stub list rather than a `Panel` so [src/sighop/web/app.py](src/sighop/web/app.py)'s contacts route can call it; verify unit cases for held, not-held, none-set and no-identities-loaded
- [x] 3.2 Use it in `_channel_context` in [src/sighop/web/routes/chat.py](src/sighop/web/routes/chat.py) so `chosen` preselects the default while an explicit `?identity=` still wins; verify a channel page test asserts the default is selected and that an unheld default selects nothing and states an identity must be chosen

## 4. Setting the preference

- [x] 4.1 Add `POST /chat/identity` in [src/sighop/web/routes/chat.py](src/sighop/web/routes/chat.py) taking a public-key hex or the empty string and a return path, writing through `page.persistence.web_users`, updating the live session, and redirecting back (design D7); verify tests for set, clear and a redirect that only ever lands on an in-app path
- [x] 4.2 Refuse the write when there is no persistence or it is degraded, stating the reason and leaving the default in force unchanged; verify a test asserts the refusal and that the previous default still applies afterwards
- [x] 4.3 Show the current default and the control that changes it on the chat index in [src/sighop/web/templates/chat/index.html](src/sighop/web/templates/chat/index.html), carrying the CSRF token as every other form there does; verify the page renders the control and that the guard refuses the POST without a token

## 5. Composers

- [x] 5.1 Preselect the default in the channel composer's `post as` select in [src/sighop/web/templates/chat/channel.html](src/sighop/web/templates/chat/channel.html), leaving every loaded identity selectable; verify a post made without touching the selection is sent as the default and one made with another selection is sent as that identity, in [tests/test_web_chat.py](tests/test_web_chat.py)
- [x] 5.2 Add the `send as` select to [src/sighop/web/templates/chat/conversation.html](src/sighop/web/templates/chat/conversation.html), preselected with the default and falling back to the identity in the URL; verify the rendered page selects the expected identity in both cases
- [x] 5.3 Accept an `identity` form field on `POST /chat/{entity_key}/{peer_key}` and redirect to `/chat/{chosen}/{peer_key}` (design D5), keeping every existing refusal shape and the author's text; verify a send as another identity lands in that identity's conversation and a send with no selection behaves exactly as today
- [x] 5.4 Re-render the conversation with the selected identity, not the URL's, when a send is refused; verify a refusal test asserts the draft and the selection both survive

## 6. Contact list

- [x] 6.1 Replace the per-identity link fan-out in [src/sighop/web/templates/contacts.html](src/sighop/web/templates/contacts.html) with one conversation link per contact, built from the resolved default or from the run's single identity (design D6), keeping the existing "no identity to send as" empty state; verify [tests/test_web_dashboard.py](tests/test_web_dashboard.py) covers two identities with a default, one identity with no default, and no identity loaded
- [x] 6.2 Pass the resolved default into the contacts route in [src/sighop/web/app.py](src/sighop/web/app.py) without adding a database read to that render; verify a test asserts the contacts page performs no persistence call

## 7. Verification

- [x] 7.1 Add a chat test that an operator's default is not visible to or changed by another signed-in operator; verify it passes in [tests/test_web_chat.py](tests/test_web_chat.py)
- [x] 7.2 Add a chat test that setting, clearing and applying a default transmits nothing, in the shape the existing "transmits nothing" tests use; verify it passes
- [x] 7.3 Run `uv run pytest` and the project's lint and type gates; verify all pass
- [x] 7.4 Run `openspec validate add-default-chat-identity --strict`; verify it reports no errors
