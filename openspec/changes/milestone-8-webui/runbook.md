# Milestone 8 live exercise — runbook

**Exit criterion (task 18.4):** with the gate open and a stock MeshCore peer, send a direct
message from the browser as a companion entity, see it acknowledged in the conversation,
receive the peer's reply in the same conversation without a reload, restart the platform,
and see both messages still there — **the whole exchange driven from the browser with no
command line.**

Read all of this before running any of it. Group 18's steps are ordered on purpose: the
first two put nothing on the air, the third touches a database and no radio, and only the
fourth transmits.

## Before the air

### 0. What is being risked, and what bounds it

Every milestone so far has added a way for sighop to transmit. This one adds a way for
**anything that can reach a TCP port** to transmit, and to read private key material — and
until milestone 9 there is no authentication in front of it. That is the whole of the new
risk, and it is bounded by four things, all of them in the code rather than in this
document:

- **The interface is off unless asked for.** No `--web`, no socket. A run without it is a
  run of a build without it.
- **It binds to `127.0.0.1` by default.** A non-loopback bind is permitted — the operator's
  call — and is announced at startup in the run's output *and* as its own logged event,
  with no option that suppresses either. If you did not mean to expose it, the startup lines
  will tell you that you did.
- **Every state-changing request must come from a page this process served.** A token issued
  at process start, present only in those pages, plus a `Host` header the interface was
  configured to answer to. That is not authentication and is not presented as one: it is the
  difference between "reachable by anything that can route to the port" and "reachable by
  anything that can render a page in the operator's browser".
- **The three dangerous actions are confirmed individually and audited individually.**
  Revealing a private key, opening the transmit gate and raising the airtime ceiling each
  have their own confirmation page, their own one-shot nonce, and their own
  `web_guarded_action` event — refusals included.

**Do not bind this to a routable address on an untrusted network.** If you need to reach it
from another machine, forward the port over SSH rather than binding wide:
`ssh -L 8080:127.0.0.1:8080 <host>`.

### 1. What to have open

Three windows, and the order matters because two of them are how you check the third:

1. **The run.** `uv run sighop run …` — the startup lines and the periodic status line.
2. **The events.** The same run's structured output, or `--log-file` tailed with `jq`.
   This is where `web_request`, `web_feed_closed` and `web_guarded_action` appear.
3. **The browser**, at whatever address the run reported.

Keep the terminal visible while using the browser. §8's whole claim is that the panel shows
what the platform is doing; the way to check that claim is to watch both at once.

---

## 18.1 — Replay, with a browser open

No radio, no transmission, no database needed.

```bash
uv run sighop run --replay captures/2026-09-04-03.jsonl --web --status-interval 30
```

**A replay run ends when the capture is exhausted, and the panel ends with it.** A
1000-frame capture is consumed in well under a second, so the window in which anything is
open is a moment — long enough for the offline check that every page answers, far too short
to look at. For a session you can actually watch, either replay a long capture and open the
browser immediately, or do this step against the live modem with the gate closed, which is
18.2 and is the better exercise anyway. This step's value is the *comparison* below, not the
watching.

Open the reported address. **Watch for:**

- the startup lines naming the address and port, and saying the interface is
  unauthenticated;
- the **feed** painting rows as the replay runs — receptions with route type, payload type,
  size, path, SNR and RSSI, and a `dup` marking on the duplicates the platform dropped;
- the **duty-cycle meter** at the top of every page, reading 0% with the gate closed and
  saying `transmit disabled — nothing will be transmitted; the meter shows what would have
  been charged`;
- the **contact table** filling as adverts verify, with routes appearing beside the
  contacts they belong to; a zero-hop route must read `direct, zero hops`, not "no route";
- the counters on the overview agreeing with the run's own status line: delivered,
  duplicates, learned path destinations, contacts.

**The check that matters:** the numbers on the page and the numbers on the status line are
the same numbers. They are read from the same objects; if they disagree, something in `web/`
has made a copy.

**Record:** whether the feed kept up, and whether the connection reported any drops.

---

## 18.2 — Live modem, receive-only

Gate closed. Nothing is transmitted.

```bash
uv run --env-file .env.dev sighop run \
  --device /dev/serial/by-id/<your-board> --web --status-interval 60
```

Leave it for at least an hour with the browser open on the overview.

**Watch for:**

- the feed keeping up with real advert volume — the busiest measured session was 555
  receptions in 2 h 54 min;
- the **feed status line** above the table. `live — N record(s) shown` is healthy;
  `feed incomplete — N record(s) lost` means this browser fell behind.

**Record, because the design's first open question is exactly this:**

- the per-connection queue depth that was actually needed (the default is 256);
- the drop count the session produced, if any;
- what the browser did with a long session — memory, responsiveness, whether the 500-row
  cap in `feed.js` was enough.

Also close the tab and check the events for exactly one `web_feed_closed` carrying
`delivered` and `dropped`.

---

## 18.3 — The admin surface, against the development database

A database, no radio required.

```bash
uv run --env-file .env.dev sighop run --replay captures/2026-09-04-03.jsonl --web
```

Walk the whole surface, and **check each change from the command line afterwards** — that
is the point of the step, not a formality. Two surfaces over one repository is the claim;
the CLI is how it gets checked.

| in the browser | check with |
| --- | --- |
| create an identity (`sighop keys new` — the UI lists, it does not create) | `uv run sighop keys list` |
| disable and re-enable an identity | `uv run sighop keys list` |
| create a room on it (`sighop room create`) | `uv run sighop room list` |
| rotate its password | `uv run sighop room list`, then a login from a real client |
| set a retention bound | `uv run sighop room list` |
| create a bot and switch it to active, then back | `uv run sighop bot list` |

**Watch for:**

- the retention page stating **how many stored messages** the bound would remove, before
  it is applied;
- the password page stating that members must log in again, before it is applied;
- the bot mode page refusing to switch to active until the box is ticked;
- a `web_guarded_action` event for each of: reveal, enable transmit, raise ceiling —
  including a **refused** one, which you can produce by reloading a confirmation page and
  submitting the stale form;
- **no password anywhere** in the events or in any served page.

Reveal a private key once, deliberately, and confirm it appears in exactly that one
response and in no page you can navigate to afterwards.

---

## 18.4 — The exit criterion

**This is the first transmission driven from a browser.** Everything before it was reading.

Preconditions:

- a stock MeshCore peer within reach, with sighop's companion identity in its contacts
  (send it a zero-hop advert first if not — a peer that has never heard our advert cannot
  decrypt a byte of what we send it, which is milestone 7's finding and applies here
  unchanged);
- the run started with `--enable-transmit`, **or** the gate opened from the browser through
  its confirmation page — doing it from the browser is the better exercise;
- a database configured, because the last step is a restart.

```bash
uv run --env-file .env.dev sighop run \
  --device /dev/serial/by-id/<your-board> --web --entity keys/companion.json
```

Then, **entirely in the browser**:

1. Open **chat**, pick the companion identity and the peer contact.
2. Type a message and send it. The conversation shows it immediately as
   `awaiting transmission`, then `attempt N in progress`.
3. Watch it become `delivered — acknowledged after N attempt(s), … ms`. The run's own output
   reports the same send and the same outcome; they must agree.
4. Have the peer reply. It appears in the same conversation **without a reload** — the
   message list refreshes itself every three seconds.
5. Stop the run (Ctrl-C, which must still work: the interface does not take the signal
   handlers). Start it again with the same command.
6. Open the same conversation. **Both messages are still there**, in the same order, with
   the sent one still marked delivered.

**If step 6 shows nothing**, check the startup line: `restored: … held: conversations=N
messages=M`. It says what the database holds before any traffic arrives, which separates
"nothing was written" from "nothing is being read".

**Record:**

- the acknowledgement latency the browser showed against the one the run's output logged;
- whether the composer's refusals behaved — try a message over 160 bytes, and a send to a
  contact with no known route without ticking flood;
- anything the panel showed that the terminal did not, or the other way round.

---

## 18.5 — The capture

If the session carries a shape the corpus lacks — a first browser-driven direct message is
one — append the capture whole, per §12's rule, and run the corpus tests against the
enlarged set. Do not trim it to the interesting frames: the corpus is evidence of sessions,
not a collection of examples.

```bash
uv run pytest tests/protocol -q
uv run pytest tests/test_corpus_pipeline.py -q
```

---

## Afterwards

Write what the exercise overturned into DESIGN.md §12, as milestones 0–7 are written
(task 17.4). The findings that matter are the ones that contradict something written here:
a bound that was wrong, a page that showed a number the terminal disagreed with, a refusal
that fired when it should not have. A milestone that discovered nothing is a milestone whose
exercise was not demanding enough.
