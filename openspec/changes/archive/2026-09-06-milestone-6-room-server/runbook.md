# Milestone 6 live exercise — runbook

**Exit criterion (task 14.5):** a stock MeshCore client logs in to a sighop room server over
the air, posts a message, and — after sighop is restarted — receives the history it missed.

This is the first milestone in which sighop transmits **because a stranger asked it to**.
The whole shape of the runbook follows from that: the room is created with a guest password
rather than opened, the advert is confirmed with transmit still disabled, and the gate is
opened only for the exchange itself. Read the whole of it before running any of it — task
14.1's verification is a review before anything is transmitted, not after.

## Before the air

### 0. What is being risked, and what bounds it

Everything sighop puts on the air goes through the existing transmit scheduler under the
existing duty-cycle ceiling and the existing `--enable-transmit` gate; milestone 6 changes
none of that. What it adds is that a *successful* login makes sighop transmit at a peer's
request. Four things bound that, and all four are already in the code:

- a login that fails is answered with **silence** (`MyMesh.cpp:353`), so an unauthenticated
  stranger cannot make sighop transmit at all;
- replies are throttled per source node hash (6/min) and globally (30/min), and every
  refusal is counted by reason and printed in the status line (design D8);
- Argon2id verification is bounded to 2 concurrent runs, so a login storm costs a bounded
  amount of memory rather than the process (design D1);
- the duty-cycle ceiling and the priority classes are untouched: history sync is class 1
  (`REPLY`) and yields to acknowledgements.

Replies are sent **unscoped** (no transport codes), which is out of scope for this
milestone and has an observable consequence worth watching for: a repeater configured with
`flood.max.unscoped=0` drops an unscoped flood at hop 0, so a room server reachable only
through such a repeater will not complete a flooded login (design D7).

### 1. Preconditions

- Two boards: one running sighop over KISS, one running stock MeshCore `v1.17.1` as the
  client. A dedicated peer board rather than the live mesh, for the same reason milestone 4
  used one — it keeps the exercise repeatable and keeps our debugging off other people's
  networks.
- `DATABASE_URL` set and the schema at head. Rooms require durable storage (design D5), and
  a half-migrated database must fail at startup rather than at the first login:

  ```
  sighop db current          # applied and expected must agree
  sighop db upgrade          # if they do not — never a side effect of `run`
  ```

- `SIGHOP_SECRET_KEY` set. Losing it makes every stored identity unrecoverable.
- A capture file path decided. The whole session is appended to the corpus afterwards
  (task 14.8), so it must be captured from the first frame, not from the interesting part.

### 2. Create the room-server identity

Being a room server is **explicit at creation** and never a side effect of having a room
bound to the identity (`entity-store`):

```
sighop keys new --name lounge-rs --out keys/lounge-rs.json --node-type ROOM_SERVER
sighop keys import keys/lounge-rs.json
sighop keys list                      # expect: type=room_server  node_type=ROOM_SERVER
```

Both must appear. `sighop room create` refuses an identity that adverts as anything else.

### 3. Create the room — with a guest password, not opened

```
sighop room create lounge-rs --name lounge --guest-password
```

It prompts for the admin password and then the guest password, without echoing, and reads
neither from `argv` — `ps` publishes `argv` to every user on the host (design D16). Use a
throwaway guest password for the exercise and a distinct admin password; §7 asks for a
distinct admin password per room-server entity precisely so that compromising one does not
expose all.

**Do not use `--open`.** An open room admits any password including an empty one, and this
exercise puts sighop on the air answering strangers; the room being password-gated is what
keeps the blast radius to one known client.

Confirm what was created, and confirm no hash is in the output:

```
sighop room show lounge
# room       lounge
# members    0
# messages   0
# guest      password
# read_only  refused
# retention  unlimited
```

`retention  unlimited` is correct and deliberate: both bounds default to unset, so nothing
is ever deleted until an operator sets a policy (design D15). §13's unknown #1 asks what a
sensible retention policy is, and this is the first milestone that can observe any volume —
a default that deleted history before the question was asked would answer it by accident.

### 4. Receive-only: confirm the advert is seen

**Transmit stays disabled for this step.** Start the run with no `--enable-transmit`:

```
sighop run --device /dev/serial/by-id/<board> --capture captures/2026-XX-XX-room-server.jsonl
```

Check the startup output before anything else:

- `transmit: DISABLED` — the gate is closed.
- `persistence: on — …  schema=0002` and the restored counts.
- `room 'lounge' on lounge-rs[xx]  members=0 messages=0  guest=password  retention=unlimited`

If the room line says `NOT SERVED`, stop: either the identity is not enabled or it was not
loaded, and the line says which. If it says `rooms: none — a room is bound to a stored
identity…`, no database is configured and nothing below will work.

Now confirm the *client* sees the advert. With transmit disabled, sighop's adverts are
scheduled, charged against the budget, logged and then dropped at the hand-off to the modem
— so **the client will not see one yet**. That is the point of this step: it confirms the
room is served, the identity is right and the pipeline is receiving, with nothing on the
air. Watch for the client's own adverts arriving in sighop's frame lines, which confirms the
link in the direction that costs nothing.

### 5. Enable transmit, and let the client learn us

Stop the run. Restart it with the gate open and one deliberate advert:

```
sighop run --device /dev/serial/by-id/<board> \
           --capture captures/2026-XX-XX-room-server.jsonl \
           --enable-transmit \
           --advert-zero-hop lounge-rs
```

A zero-hop advert reaches direct neighbours and stops there. Milestone 4 measured what
happens next and it is exactly §13's unknown #4: a zero-hop advert carries no path, so a
peer that learns us this way records `out_path_len = -1` (unknown) and will **flood** its
replies. Watch for that during the login — it is the observation task 14.7 exists to make,
and it must be read off the captured frames rather than inferred.

## On the air

### 6. Log in from the client (task 14.2)

On the MeshCore client: find `lounge-rs` in its contact list, choose to join the room, and
give it the **guest** password.

What to expect in sighop's output:

```
<- room 'lounge'  joined ✗<key prefix>  as read_write  (flooded)  id=<packet id>
```

- `joined` rather than `returned` — a first login.
- `as read_write` — the guest password earns read-write. The admin password would say
  `admin`; a wrong password with `--allow-read-only` set (not used here) would say `guest`,
  which is the read-only spectator level.
- `✗` before the key: a member's public key is a **claim**. A MAC match selects a key; it
  never authenticates a sender (§5).
- `(flooded)` or `(direct)` — record which. Flooded means the reply went back as a `PATH`
  return carrying the 13-byte login response inside it, so the client learns a route home in
  the same packet that tells it the login succeeded (design D7).

Then check the durable side:

```
sighop room members lounge     # the client's public key, read_write, sync position
```

**If nothing appears at all**, that is a meaningful result rather than a failure to
investigate blindly: a login sighop did not authenticate produces no transmission. Check
the status line's `refused=` counters — `bad_password` means the password did not match,
`throttled_source` means the client retried faster than the throttle allows, `replay` means
its timestamp was not newer than the recorded one.

### 7. Post from the client (task 14.3)

Post a short message from the client's room view. Expect:

```
<- room 'lounge'  post @<ordering value> from ✗<key prefix>  'the text'
```

The acknowledgement is sent **only after the row lands** (design D6) — an acknowledgement is
a promise the client will not retry, so the client showing the post as delivered is
evidence the row is real. Confirm from the other side:

```
sighop room history lounge
```

**Post the longest message the client will let you compose**, because that is where the
first run found a divergence. A room keeps 156 bytes (`STORED_POST_TEXT_LEN`), which is
`MAX_FRAME_SIZE` (176) less the 20-byte prefix the client's own `queueMessage` puts in front
of the text — the same budget that sizes its composer, so filling the composer must produce
a post stored **whole**, with no truncation marker on the line:

```
<- room 'lounge'  post @<ordering value> from ✗<key prefix>  'the text'
```

If that line says `(TRUNCATED from N bytes)`, this client's frame budget is not the one the
constant was derived from — record `N`, because it is the real limit and 156 is wrong.

A post that does exceed 156 is stored shortened and **still acknowledged**, over the text as
the client sent it, so the client stops retrying. The client must show it as delivered; if
it retries instead, the acknowledgement was computed over the stored text rather than the
received text and the exchange is broken.

### 8. Post from the server (task 14.4)

```
sighop room post lounge "hello from the server"
```

The post is held for `POST_SYNC_DELAY_SECS` (6 s) before it becomes eligible, then pushed
on the round-robin. Expect, in order:

```
-> room 'lounge'  pushed @<ordering value> to <key prefix>  <route>  expect=<checksum>
<- room 'lounge'  delivery acknowledged @<ordering value> by <key prefix>
```

**Check the cursor before and after the acknowledgement**, because that is the property
under test:

```
sighop room members lounge     # before the ACK: sync_since unchanged
sighop room members lounge     # after: sync_since == the pushed post's ordering value
```

A cursor that advanced on the *push* rather than on the acknowledgement would look identical
here on a good link and would lose messages on a bad one. If the acknowledgement arrives
`(bundled in a path return)`, that is design D12's case — the client answered a flooded push
with a path return carrying the ACK inside it — and it is worth recording, because without
decrypting that body the push would have been retried three times for nothing.

### 9. Restart, and receive what was missed (task 14.5 — the exit criterion)

While the client is **out of range or powered off**, post two or three more messages from
the server. Then stop sighop entirely, restart it with the same command line, and bring the
client back.

What must be true:

- The startup line reads `members=1` — the member was restored, not re-authenticated.
- The client does **not** log in again. Its traffic arrives as ordinary addressed traffic.
- The messages posted while it was away are pushed to it, from its stored position, oldest
  first — not merely the most recent, and not from the beginning.

That is the milestone's exit criterion, and it is the thing a 32-entry RAM ring cannot do.

## After the air

### 10. Read the numbers off the run (task 14.6)

From the run's **own counters**, not estimated:

- the status line's `duty=` field — airtime used against the hourly ceiling, as a
  percentage and as seconds;
- `tx=` / `sup=` / `drop=` — what reached the air, what the gate suppressed, what was
  dropped;
- the room status line's `stored=`, `outstanding=`, `behind=`, `refused=`.

The message volume goes against §13's unknown #1. An exercise this short will almost
certainly be too short to say what a sensible retention default is; **record that as a
non-observation rather than as confirmation**, in the same terms milestone 5 used for path
candidates.

### 11. Record the path observation (task 14.7)

§13's unknown #4: does a peer that learns us only from a zero-hop advert flood its replies?
Answer it from the **captured frames** — the route type on the client's login and on its
acknowledgements — and not from what the client's UI says. If the exchange happened to run
over a route the client already had, no opportunity arose, and that is recorded as no
opportunity rather than as a negative result.

### 12. Append the session to the corpus (task 14.8)

The whole capture, with its provenance header, exactly as every session since milestone 2:

- copy the capture into `captures/`,
- update the counts in `tests/protocol/corpus.py`,
- update `tests/protocol/CORPUS.md`,
- run `uv run pytest tests/protocol -q` and confirm the totals agree.

This session is the first in the corpus to contain a room login, a post and a history push,
which is what makes it worth keeping whether or not everything worked.

## If it goes wrong

| What you see | What it means |
| --- | --- |
| Nothing at all after a login attempt | The password did not match and `allow_read_only` is off. Check `refused=bad_password`. |
| `login refused: replay` | The client's timestamp is not newer than the recorded one — a clock that went backwards. `sighop room revoke <prefix>` and let it log in fresh; the message says so. |
| `login refused: throttled_source` | The client is retrying faster than 6/min. Wait, or raise the bound for the exercise. |
| A post the client keeps retrying, with no refusal logged | The acknowledgement did not match what the client computed. For an over-long post this means the checksum was taken over the truncated text instead of the received text. |
| `post refused: storage_degraded` | The database is unreachable. A room is exactly as available as its history, and nothing is acknowledged while it is not. |
| `REFUSING POSTS` in the status line | The same, seen without reading the event stream. |
| Pushes sent, never acknowledged, then `backed off` | Three consecutive unacknowledged deliveries. Delivery resumes when the member is next heard from. |
| `not sent (gate closed)` | `--enable-transmit` is missing. |

## Rollback

`sighop db downgrade` plus removing the room-server entity. No other component reads the
three tables, and a runtime with no room rows behaves exactly as milestone 5's does.
