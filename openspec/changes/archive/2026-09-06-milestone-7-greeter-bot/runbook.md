# Milestone 7 live exercise — runbook

**Exit criterion (task 13.5):** the greeter sends a welcome message to a genuinely unknown
test peer, the peer receives and acknowledges it, and after sighop is restarted and the peer
adverts again **no second greeting is sent**.

This is the first milestone in which sighop transmits **at a stranger who asked for
nothing**. Milestone 6 was a stranger asking sighop to transmit, and silence on failure was
enough to bound it; nothing about a greeting is asked for, and the trigger is the one packet
type anyone can forge cheaply. So the exercise is staged: **observe first, and read the
decision log before anything is put on the air.** Read the whole of this before running any
of it — task 13.1's verification is a review before anything is transmitted, not after.

## Before the air

### 0. What is being risked, and what bounds it

Everything sighop puts on the air still goes through the existing transmit scheduler, under
the existing duty-cycle ceiling and the existing `--enable-transmit` gate; milestone 7
changes none of that. What it adds is that sighop may **originate** a direct message to a
node it has never exchanged anything with, because an advert arrived. Six things bound that,
and all six are in the code rather than in this document:

- **observe mode is the default**, and it is a stored column rather than a command-line
  flag (design D4). A newly created bot runs its whole decision path and puts nothing on the
  air. Making it active is a deliberate `uv run sighop bot mode … active`, and the run's
  `--enable-transmit` gate applies on top of that — two gates that answer to different
  people;
- **once ever, per contact**, guarded by a durable greeting record (design D7). Whether the
  advert *created* the contact does not enter into it — a node known for weeks and never
  greeted has an unsent welcome — so what bounds a new greeter is that **creating one seeds
  a record for every contact this node already knows**. It starts owing nothing, and
  `uv run sighop bot greeted` is where an operator releases one deliberately. The record is
  written **before** the transmission, so a crash between them costs an un-sent greeting
  rather than a duplicate (design D6);
- **at most three attempts, fifteen minutes apart**, because an unacknowledged greeting is
  not a delivered one (design D6, revised after the first run of this exercise). A greeting
  the peer could not read looks exactly like a peer that was not listening, so silence earns
  a retry rather than a permanent record — bounded by `greeting_attempts` (3) and spaced by
  `retry_after_minutes` (15), so a node adverting every few minutes cannot turn one silence
  into a burst;
- **a hop limit**, defaulting to 1 — nodes we hear directly and nodes one repeater away;
- **a node-type gate**, defaulting to chat nodes only, so no repeater or room server is sent
  a message no human will read;
- **a per-bot rate limit** (default 6/hour, burst 3), spent before anything is composed, so
  a burst of adverts after an outage cannot become a burst of greetings;
- **never flooded**: a greeting to a peer with no known route is suppressed with `no_route`
  rather than shouted across the mesh (design D10). Note that the *advert* below may be
  flooded — an advert is addressed to nobody and is the packet a mesh expects to repeat;
  the unsolicited private message is the one that is never shouted.

**Expect an advert before each greeting to a direct neighbour, and after the first silence
from anyone further** (design D18, and the reason the first two runs of this exercise never
got an acknowledgement from a distant peer). A direct message is encrypted under a secret
derived from the *sender's* public key, so a peer that has never heard the greeter's advert
cannot read a byte of a greeting and has nothing to acknowledge — indistinguishable, from
our side, from a peer that is not listening. So:

| the contact was heard | what goes on the air |
| --- | --- |
| **directly (h0)** | zero-hop advert, then the greeting — every attempt |
| **over a repeater (h1+), first try** | the greeting alone, betting the peer already holds our key |
| **h1+, that greeting unanswered** | flood advert, then the greeting again — **immediately**, once |

The escalation does not wait for the cooldown. Silence from a peer that cannot decrypt us
will still be silence in fifteen minutes, and the peer is adverting *now*, which is the one
moment it is known to be awake. After that single escalation the ordinary cooldown governs
everything further.

**A greeting keeps listening for 30 s past its last attempt** (`ack_grace_seconds`). An
acknowledgement returning over a longer path than the one we sent on is late rather than
absent, and the action taken on the difference is a flood the whole mesh repeats. Nothing is
transmitted during the window — expect the packet count to be unchanged and the resolution
to arrive about half a minute after the fourth attempt.

**The hop gate does not bound radio distance, and this exercise must not pretend it does.**
A tropospheric or ducted path delivers a zero-hop advert from a node hundreds of kilometres
away, and no hop count separates that from a neighbour across the street. `min_snr_db` is
offered and is null by default because a strong ducted signal defeats it too. What bounds
the damage when one gets through is the once-ever rule, and what makes it visible is the
counters. If the observe stage shows zero-hop adverts from nodes that cannot plausibly be
local, that is a finding for §13 and not a bug to fix in this milestone.

**Greeting the live mesh is not a deliverable.** The transmit stage is against a dedicated
test peer. Whether to point a greeter at a shared public channel afterwards is the
operator's decision, made deliberately and not as a side effect of finishing a milestone.

### 1. Preconditions

- Two boards: one running sighop over KISS, one running stock MeshCore as the test peer. A
  dedicated peer rather than the live mesh, for the same reason milestones 4 and 6 used one.
- `DATABASE_URL` and `SIGHOP_SECRET_KEY` in the environment. Every command below is written
  as a bare `uv run sighop …`; in this checkout those two come from `.env.dev`, which is
  gitignored and is **not** read automatically — so the commands are really:

  ```
  uv run --env-file .env.dev sighop …
  ```

  The `--env-file` is left out of the rest of this document only to keep the lines readable.
  Losing `SIGHOP_SECRET_KEY` makes every stored identity unrecoverable.

- The schema at head. **Bots require durable storage** (design D5): §7 rule 1 is a statement
  about durable greeting records, and without a database there are none.

  ```
  uv run sighop db current          # applied and expected must agree — expect 0003
  uv run sighop db upgrade          # if they do not — never a side effect of `run`
  ```

- A capture file path decided. The whole session is appended to the corpus afterwards
  (task 13.8), so it must be captured from the first frame, not from the interesting part.

### 2. Create the bot identity

Being a bot is **explicit at creation** and never a side effect of having a bot bound to the
identity (`entity-store`). A bot adverts as an ordinary chat node — it is a companion to
every other node on the mesh, and its automation is sighop's business rather than the
mesh's — so only the stored type tells them apart:

```
uv run sighop keys new --name syn-greeter-bot --out keys/syn-greeter-bot.json --node-type CHAT
uv run sighop keys import keys/syn-greeter-bot.json --bot
uv run sighop keys list                      # expect: type=bot  node_type=CHAT
```

Both must appear. `uv run sighop keys import --bot` refuses a keyfile that adverts as anything
else, rather than silently forcing the node type and leaving the stored identity disagreeing
with the file it came from.

### 3. Create the greeter — which is created observing

```
uv run sighop bot create syn-greeter-bot --driver greeter
```

Expect, and read every line of it:

```
bot        syn-greeter-bot
driver     greeter
mode       observe
enabled    yes
  ack_grace_seconds  30
  burst          3
  greeting       Hi — you are new to me. I am a sighop node listening on this mesh.
  greeting_attempts  3
  max_hops       1
  min_snr_db     none
  node_types     [1]
  rate_per_hour  6.0
  retry_after_minutes  15
the bot is enabled and in observe mode: it runs its whole decision path and will transmit
nothing until `sighop bot mode syn-greeter-bot active` says otherwise
transmission still requires the run's --enable-transmit flag, and stays under the
duty-cycle ceiling
seeded     N existing contacts recorded as already greeted, so this greeter starts owing
nothing to the contacts this node already knew. Release one with `sighop bot greeted
syn-greeter-bot <peer> --clear`
```

(sighop names its own commands without the `uv run` prefix, because it does not know how it
was launched. Everything it suggests is run the same way as everything else here.)

`mode observe` is the line that matters. If it says anything else, stop.

**Check `seeded N` against what this node knows.** `uv run sighop keys list` and the contact
count in a previous run's status line are the cross-check. If it says `seeded nothing` on a
node that has been receiving for weeks, the seed did not happen and the greeter owes a
greeting to every contact in the table — run
`uv run sighop bot greeted syn-greeter-bot --seed` before going any further.

Set a greeting appropriate to the mesh you are on, if the default is not:

```
uv run sighop bot set syn-greeter-bot greeting "welcome — you are new to this node"
```

An over-long greeting is refused **here**, with the limit and the length given, rather than
discovered at 3am when the first new node adverts (design D14). A greeting is never
truncated or split at send time.

## Stage one — observe (tasks 13.2, 13.3)

### 4. Run against live adverts, receive-only

**Transmit stays disabled.** Start the run with no `--enable-transmit`:

```
uv run sighop run --device /dev/serial/by-id/<board> \
           --capture captures/2026-XX-XX-greeter.jsonl
```

Check the startup output before anything else:

- `transmit: DISABLED` — the gate is closed.
- `persistence: on — …  schema=0003` and the restored counts.
- `bot 'syn-greeter-bot'[xx]  driver=greeter  mode=observe  rate_per_hour=6.0 burst=3
  max_hops=1 node_types=CHAT min_snr_db=none greeting_bytes=NN
  greeting_attempts=3 retry_after_minutes=15 ack_grace_seconds=30`

If the bot line says `NOT RUNNING`, it says which of the five reasons it is: the bot is
disabled, its identity is not enabled, its identity was not loaded, its identity serves a
room, or this build has no such driver. If it says `bots: none — a bot's decisions depend
on state restored before any traffic…`, no database is configured and nothing below will
work.

**Two gates are closed during this stage and that is deliberate**: the mode says the bot
transmits nothing, and the run's gate says nothing transmits at all. The point of the stage
is the decision log, not a demonstration that the gates hold — though it is that too.

### 5. Let it run, and read the decision log (task 13.2)

Leave it running long enough to see a representative sample of the mesh's adverts. Every
decision is a line as it happens:

```
.. bot 'syn-greeter-bot'  suppressed: already_greeted  ✗<key prefix>
.. bot 'syn-greeter-bot'  suppressed: too_many_hops  ✗<key prefix>  4 hops, limit 1
.. bot 'syn-greeter-bot'  suppressed: node_type  ✗<key prefix>  REPEATER
.. bot 'syn-greeter-bot'  would send to ✗<key prefix>  transmitted nothing (observe mode)  '…'
```

and the periodic status line carries the running totals:

```
== bot 'syn-greeter-bot' driver=greeter mode=observe acted=0 observed=N pending=0
   dropped=0 failures=0 suppressed=already_greeted=A,node_type=B,too_many_hops=C
```

`already_greeted` will dominate on an established node, and that is the seed working:
every contact present at creation carries a record. The interesting number is `observed` —
those are nodes this greeter has said nothing to and would have greeted.

**Record these numbers from the run's own counters, not from an impression of the output.**
What the log has to answer:

- how many adverts were seen at all (the contact store's `adverts_recorded` in the status
  line, and the frame lines);
- how many were from nodes carrying no greeting record — that is `observed` plus the
  suppressions that are not `already_greeted`;
- how each gate suppressed what it suppressed, by reason.

`acted=0` must hold for the whole stage. If it does not, an observe-mode bot transmitted and
the milestone is not finished.

### 6. Decide whether `max_hops = 1` is right for this mesh (task 13.3)

From the counters, not from impression. The question the numbers answer:

- if `too_many_hops` dominates and the hop histogram shows most new contacts arriving at 2
  hops, a limit of 1 greets almost nobody on this mesh and the default deserves changing —
  **with the observation that justified it recorded**, in §12's milestone 7 entry;
- if `too_many_hops` is small, the default is doing what it was chosen to do and stays;
- if zero-hop adverts arrive from nodes that cannot plausibly be local, record that as the
  ducted-path observation design D8 predicted and did not claim to solve. It is a §13 entry,
  not a change to the gate.

Change it, if the numbers say so, with:

```
uv run sighop bot set syn-greeter-bot max_hops 2
```

## Stage two — the test peer (tasks 13.4 to 13.7, 13.9)

Only now does anything reach the air, and only at a peer that is ours.

### 7. Make the peer one the greeter owes a greeting to

Two ways, and either is valid because the gate is the greeting record rather than the
contact:

- **Release it.** The peer is almost certainly in the contact table from milestone 4 or 6,
  and was therefore seeded at creation. Clear its record:

  ```
  uv run sighop bot greeted syn-greeter-bot <peer name or key prefix> --clear
  # cleared the greeting record for <name> (<key prefix>): bot 'syn-greeter-bot' will greet it
  # the next time it adverts, if the hop, node-type and rate gates pass
  ```

- **Re-key it.** Factory-reset or re-key the peer so its public key is one sighop has never
  heard. This also exercises the contact store's create path, which the first option does
  not.

Prefer re-keying if the board is easy to reset, because it tests one more thing. Either way,
confirm before going on:

```
uv run sighop bot greeted syn-greeter-bot <peer>
# <name> (<key prefix>) has no greeting record from bot 'syn-greeter-bot' and will be greeted
# when it next adverts
```

If that line says the contact *has* a record, the exercise cannot test what it is for.

### 8. Make the greeter active

```
uv run sighop bot mode syn-greeter-bot active
```

Expect:

```
bot 'syn-greeter-bot' is now active: it may transmit. Transmission still requires the run's
--enable-transmit flag and stays under the duty-cycle ceiling
```

### 9. Run with the gate open, and let the peer advert (task 13.4)

```
uv run sighop run --device /dev/serial/by-id/<board> \
           --capture captures/2026-XX-XX-greeter.jsonl \
           --enable-transmit
```

Confirm the startup line now says `mode=active`, then prompt an advert from the peer (a
factory-reset MeshCore node adverts on its own; a zero-hop advert can be requested from its
UI). Expect, in order:

```
[frame line for the peer's advert]
-> advert  syn-greeter-bot  zero-hop  109B
-> bot 'syn-greeter-bot'  sent to ✗<key prefix>  DIRECT h0  attempts=1  acknowledged  '…'
```

- **The advert comes first, and this is the fix for the first run's failure.** A peer that
  has never heard `syn-greeter-bot` holds no key for it, cannot derive the shared secret,
  cannot read the greeting, and has nothing to acknowledge (design D18). If the greeting line
  appears with no advert line before it, the ordering is broken — an advert is class 3 and a
  message is class 2, so a send that did not wait would be transmitted first.
- `DIRECT h0` — a zero-hop advert teaches a zero-hop route, so the greeting goes back the
  way it came. **If this says `FLOOD`, something is wrong**: the greeter never asks to flood
  a *greeting*, and `choose_route` refuses to without being asked. (A flood *advert* is
  expected only on a retry to a peer heard over a repeater.)
- `acknowledged` — the peer answered. `NOT ACKNOWLEDGED` after four packet attempts is
  recorded as an **attempt**, not a delivery: the contact is retried after
  `retry_after_minutes` on its next advert, up to `greeting_attempts` (design D6, revised).
- The message must appear on the peer, as a direct message from `syn-greeter-bot`.

**If the greeting is unacknowledged, that is now a testable path rather than a dead end.**
For a peer heard **directly**, the advert already went out, so silence means something the
greeter cannot fix by repeating itself: it waits. Leave the run going and let the peer
advert again inside 15 minutes: expect

```
.. bot 'syn-greeter-bot'  suppressed: greeting_cooldown  ✗<key prefix>  attempt 1 unanswered, retry in Nm
```

and after the cooldown, a second greeting. Three attempts in, expect
`greeting_exhausted` and no further traffic to that peer.

### 9b. A peer heard over a repeater — the escalation (task 13.9)

The case the second run of this exercise failed, and the one worth reproducing deliberately.
Use a peer whose adverts reach sighop at **h1** (through a repeater), that has never heard
`syn-greeter-bot`, and that carries no greeting record. Expect, in order, from a single
advert:

```
[frame line for the peer's advert, h1]
-> dm sent  to '<name>'  attempt 0  DIRECT h1  …          # bare: no advert before it
   … three more packet attempts, then ~30 s of grace …
! dm unacknowledged to '<name>' after 4 attempt(s)
-> bot 'syn-greeter-bot'  sent to ✗<key prefix>  DIRECT h1  attempts=1  NOT ACKNOWLEDGED  '…'
-> advert  syn-greeter-bot  flood  118B                   # the escalation
-> bot 'syn-greeter-bot'  sent to ✗<key prefix>  DIRECT h1  attempts=2  acknowledged  '…'
```

What each part is asserting:

- **no advert before the first greeting** — the bet that the peer already holds our key,
  which costs nothing to make and saves a flood whenever it wins;
- **about 30 s between the fourth packet attempt and the resolution** — the grace window. No
  packet is transmitted during it; if the packet count for that greeting exceeds four,
  something is transmitting that should not be;
- **the flood advert follows immediately**, in the same reaction, with no cooldown in
  between. If instead the next line is `greeting_cooldown`, the escalation did not fire;
- **the second greeting is acknowledged**, which is the whole point: the peer can now derive
  the secret, so it can read us. `attempts=2` in the record is the escalation having
  happened.

If that second greeting is *also* unacknowledged, the greeter stops and hands over to the
cooldown — expect `greeting_cooldown` on the next advert and no third packet inside 15
minutes. That is correct behaviour and a finding worth recording: the peer is out of range,
asleep, or ignoring us, and none of those is fixed by transmitting more.

**Watch `announced=` in the status line for the whole stage.** It counts adverts the bots
asked for. One per h0 greeting and one per h1+ escalation is expected; a number climbing
faster than that means the escalation is firing more than once per contact.

**If no greeting appears at all**, the line before it says why. `no_route` means the advert
taught no route — unexpected for a zero-hop advert and worth recording. `rate_limited` means
earlier decisions drained the bucket. `storage_degraded` or `state_write_failed` means the
database did not answer, and the greeter correctly greeted nobody rather than greeting
blind.

### 10. Restart, and advert again — the exit criterion (task 13.5)

**This step needs the greeting from step 9 to have been acknowledged.** An acknowledgement is
what settles a contact for good; an unacknowledged one is an attempt, and a restart inside
the cooldown will correctly say `greeting_cooldown` rather than `already_greeted`. If step 9
ended unacknowledged, work out why before continuing — that is the interesting result, not
this one.

Stop the run. Start it again with the same command. Prompt another advert from the same
peer. Expect:

```
.. bot 'syn-greeter-bot'  suppressed: already_greeted  ✗<key prefix>
```

`already_greeted`, because the greeting record was written before the transmission, records
the acknowledgement, and is durable. **No greeting is sent, and no advert either.** This is
the milestone's exit criterion, and it is what §7 rule 1 means by "never re-greet across a
restart".

Then delete the contact row and advert once more. The record must still suppress, still as
`already_greeted` — the record is per bot and the contact table is the platform's memory of
the mesh, so losing the second does not re-open the first.

Confirm the record from the other side:

```
uv run sighop bot greeted syn-greeter-bot <peer>
# <name> (<key prefix>)  acknowledged  at 2026-…
```

An unsettled record shows its attempt count instead, which is what tells an operator whether
the peer is about to be left alone:

```
# <name> (<key prefix>)  unacknowledged after 2 attempt(s)  at 2026-…
```

### 11. Confirm the route and the airtime (tasks 13.6, 13.7)

- **Not flooded (13.6):** the run's own line said `DIRECT h0`; confirm it in the captured
  frames — the transmitted advert-triggered `TXT_MSG` must carry `RouteType.DIRECT`. A
  greeting is unsolicited traffic to a peer that has never contacted us, and flooding one
  imposes its cost on the whole mesh.
- **Airtime (13.7):** read the fraction of the hourly ceiling the exercise cost from the
  run's own counters in the status line, not from an estimate. Record it as a fraction, as
  the earlier milestones did.

### 12. Append the session to the corpus (task 13.8)

The whole session, with its provenance header, exactly as milestones 4 and 6 did. Update the
corpus counts and `tests/protocol/CORPUS.md`, and confirm the corpus tests pass against the
new totals. This session is the first that contains a packet sighop originated **on its own
initiative**, which is what makes it worth keeping.

## Afterwards

Leave the greeter in whatever mode the operator intends. If the exercise is over and the
mesh is a shared one, `uv run sighop bot mode syn-greeter-bot observe` is the state a bot
should be left in — a greeter pointed at strangers is a decision that should be made on
purpose each time, not inherited from the last exercise.
