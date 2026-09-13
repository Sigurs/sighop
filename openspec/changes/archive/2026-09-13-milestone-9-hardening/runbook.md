# Milestone 9 live exercise — runbook

**Exit criterion (design.md, Migration Plan):** the image built by `./build.sh`, started by
`docker compose up` as a non-root UID with the Heltec V4 mapped by `/dev/serial/by-id`; an
operator signs in from a browser on the host; enables transmit with a password re-entry;
sends a direct message as a stored companion identity that the stock peer acknowledges;
`docker compose restart sighop` signs them out and the conversation is still there after
signing back in; and every `web_request` and `web_guarded_action` in the container's log
names the account. The board is reset once while containerised and the outcome recorded,
whichever it is.

Read all of this before running any of it. Steps 1–5 put nothing on the air. Step 6 is the
first transmission and happens only after the gate is opened from the browser, with a
password, in step 6 itself.

## Before the air

### 0. What is being risked, and what bounds it

This milestone changes *how* sighop is run, not what it transmits. The new risks are the
deployment's, and each is bounded by something in the files rather than in this document:

- **The panel now requires a signed-in account**, on every page, form and the feed socket.
  There is no switch that turns that off.
- **It is published on host loopback only** (`127.0.0.1:8080`). Inside the container it
  binds `0.0.0.0`, so the startup output *always* prints the plain-HTTP warning there — that
  is expected, and it is correct: the process cannot see that compose published it on
  loopback. If you change the published address, the warning is the one that applies.
- **The container runs as your UID** with the serial group as a supplementary group, a
  read-only root, no capabilities and `no-new-privileges`. It cannot write anywhere but
  `/tmp`.
- **A fresh deployment is receive-only.** The compose command has no `--enable-transmit`;
  the gate opens only from the panel's confirmation page, with the account's password, and
  closes again on every restart.
- **The database is a separate, throwaway Postgres** on an internal network with no
  published port — *not* the development database. sighop migrates *that* database on
  start (`run --migrate`); nothing here migrates or writes the shared dev database.

**Test entities keep the `dev-` convention** (DESIGN.md §2): the account is `dev-operator`.
The companion identity is **reused**, not created: `keys/[redacted].json` is the companion
that exchanged direct messages with the stock peer in milestone 8, so the peer already holds
its key and can decrypt and acknowledge (milestone 7's finding: a peer that has never heard
an identity's advert cannot decrypt a byte from it).

### 1. What to have open

1. **A terminal** in the repository, for compose.
2. **The container's events**, filtered:
   ```bash
   docker compose logs -f --no-log-prefix sighop | grep --line-buffered '^{' \
     | jq -c 'select(.event | test("^web_")) | {event, outcome, actor, route, action, reason}'
   ```
3. **The container's rendered output** (the status line, frames, and the lines the panel's
   guarded actions print):
   ```bash
   docker compose logs -f --no-log-prefix sighop | grep --line-buffered -v '^{'
   ```
4. **The browser**, at `http://localhost:8080`.
5. **The stock peer** — "[redacted]" on the CP2102 board
   (`/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0`)
   — through its own client, able to see an incoming message.

---

## 15.2 — Build and deploy

### 1. Build

```bash
./build.sh
```

**Expect** every gate to pass and a final report naming `sighop:0.1.0-<commit>` (with
`-dirty` if the tree has uncommitted changes) and `sighop:local`. `compose.yaml` uses
`sighop:local`.

**The `scan` gate prints HIGH/CRITICAL findings and does not fail on them** (operator
decision). Read the report before deploying and record the counts; the gate fails only if
the scan could not run.

### 2. `.env`

Once, in the repository root. The secret key is a *new* one: this deployment has its own
database, and the only identity it will hold is imported in step 4.

```bash
modem=/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_90:70:69:83:D3:F8-if00
umask 077
cat > .env <<EOF
UID=$(id -u)
GID=$(id -g)
DIALOUT_GID=$(stat -L -c %g "$modem")
SIGHOP_MODEM=$modem
POSTGRES_PASSWORD=$(openssl rand -hex 24)
$(uv run sighop keys secret | grep '^SIGHOP_SECRET_KEY=')
EOF
git check-ignore .env               # must print .env
docker compose config -q && echo "compose resolves"
```

On this host the serial group is `uucp` (984), not `dialout`; `stat -L` finds whichever it is.
*(Corrected in the exercise: without `-L`, `stat` reads the by-id symlink, owned by root, and
returns `0`.)*
**Check first** that nothing else holds the V4 open: `fuser -v "$modem"` prints nothing.

### 3. Up: migrate on start, and the no-account refusal

```bash
docker compose up -d
docker compose logs --no-log-prefix sighop | grep '^{' | jq -c 'select(.event == "database_migrated")'
docker compose logs --no-log-prefix sighop | grep -v '^{' | tail -5
```

**Expect** `database_migrated` with `from_revision: null`, `to_revision: 0005`, `applied: true`,
then a refusal naming `sighop web user add`, with no port listened on — and `docker compose ps`
showing sighop restarting. That loop is expected until step 4 adds the account.

### 4. Account and identity

```bash
docker compose run --rm -it sighop web user add dev-operator
docker compose run --rm -v "$PWD/keys/[redacted].json:/keys/[redacted].json:ro" \
  sighop keys import /keys/[redacted].json
docker compose run --rm sighop web user list             # dev-operator, enabled, no hash
```

**Watch for:** the account prompt asking twice and not echoing; `add` stating the account can
sign in to any run using this database; `list` showing no `$argon2id$`.

### 5. Serving, and sign in

```bash
docker compose restart sighop
docker compose ps       # postgres: no host port. sighop: running, 127.0.0.1:8080->8080/tcp
ss -ltn | grep 8080     # 127.0.0.1:8080 only
docker compose logs --no-log-prefix sighop | grep '^{' \
  | jq -c 'select(.event == "database_migrated") | .applied' | tail -1   # false: nothing to apply
```

**The startup lines** (window 3) must read `web: http://0.0.0.0:8080 — sign-in required; 1
enabled account(s)`, the plain-HTTP warning, and `also answers to: localhost:8080,
127.0.0.1:8080`. The status line must say `TX=disabled`.

In the browser: `http://localhost:8080` redirects to the sign-in form. **Try a wrong password
once** — the page says only that the username and password did not sign you in, and window 2
shows `web_login` with `reason: bad_password`. Then sign in as `dev-operator`. The header
names the account and offers *sign out*.

**Record:** whether frames and the meter move in the browser as they do in window 3.

---

## 15.2 (continued) — The first transmission of this deployment

### 6. Open the gate, with the password

In the browser: **admin → transmit** (`/admin/transmit`). The confirmation page asks for your
password. Enter it and confirm.

**Watch for:**

- the meter and gate indication changing to transmit enabled;
- window 3 printing `web: transmission ENABLED — this node now transmits (by account
  'dev-operator' from the web interface)`;
- window 2: `web_guarded_action` with `action: enable_transmit`, `outcome: success`,
  `actor: dev-operator`.

**Optional:** submit the confirmation once with a wrong password first — nothing changes, the
refusal is its own `web_guarded_action` with `reason: bad_password`, and you stay signed in.

### 7. Send the direct message

In the browser: **chat**, pick `[redacted]` and "[redacted]", send a short text
(`sighop milestone 9, from a container`).

**Watch for:** `awaiting transmission` → `attempt 1 in progress` → `delivered — acknowledged
after N attempt(s), … ms`, and the peer showing the message. Window 3 reports the same send
and outcome.

**If it is never acknowledged**, the peer may not hold `[redacted]`'s key. The panel has no
advert control, so send one zero-hop advert from the same image with the service stopped —
this transmits one advert and nothing else — then bring the service back, sign in, open the
gate again and resend:

```bash
docker compose stop sighop
docker compose run --rm sighop run --device /dev/modem --enable-transmit \
  --advert-zero-hop [redacted] --status-interval 3600     # Ctrl-C once the advert is sent
docker compose up -d sighop
```

### 8. Restart, sign back in, find the conversation

```bash
docker compose restart sighop
```

**Watch for:** the browser's next navigation going to the sign-in form (sessions are in
memory; a restart ends them); the gate closed again (`TX=disabled`); `docker compose logs
sighop` showing a graceful stop (exit status 0) rather than a kill. Sign in again, open the
same conversation: **the sent message is there, still marked delivered.**

### 9. Every request names the account

```bash
docker compose logs --no-log-prefix sighop | grep '^{' \
  | jq -r 'select(.event == "web_request" or .event == "web_guarded_action")
           | [.event, .actor, (.route // .action)] | @tsv' | sort | uniq -c
```

**Expect** every `web_guarded_action` to name `dev-operator`, and every `web_request` to name
`dev-operator` **except** the ones a signed-out browser makes by definition — the redirect to
the sign-in form, `GET /login`, the static assets that page loads, and a failed `POST
/login` — which read `unauthenticated`. A successful `POST /login` names the account. Anything
else reading `unauthenticated` is a finding.

---

## 15.3 — Reset the board while containerised

With the deployment running and the gate closed (restart it, or close the gate from the
panel):

```bash
ls -la /dev/serial/by-id/ | grep Espressif          # before: -> ../../ttyACMn
docker compose exec sighop ls -la /dev/modem         # before: major, minor
```

Press **RST** on the V4 once. Wait 15 seconds.

```bash
ls -la /dev/serial/by-id/ | grep Espressif          # after: same ttyACMn, or a new one?
docker compose ps sighop                             # running / restarting?
docker compose exec sighop ls -la /dev/modem
docker compose logs --since 1m sighop | grep -v '^{' | tail -20
```

**Record, whichever it is:**

- whether the host re-enumerated the board under the same `ttyACMn` (same minor number) or a
  new one;
- whether sighop's reconnect loop recovered, or the process exited and `restart:
  unless-stopped` brought it back — and if so, whether the recreated process could open
  `/dev/modem`;
- whether frames resume in window 3.

A probe during deployment verification found that sighop **exits** when `/dev/modem` cannot
be opened at start (it does not retry an initial open), so a board that comes back under a
different minor number would put the container into a restart loop against a dead node.
**If it strands**, apply design.md's mitigation — a `device_cgroup_rules` entry for the ACM
major (`c 166:* rmw`) plus a read-only bind mount of `/dev/serial/by-id` with `SIGHOP_MODEM`
read through it — re-run this step, and record the change to `compose.yaml`.

---

## Afterwards

```bash
docker compose down            # keeps the pgdata volume; add -v to discard it
rm .env                        # unless keeping the volume: its key opens the stored seed
```

Write what the exercise overturned into DESIGN.md §12 (task 15.4), and resolve or restate
design.md's open questions: whether a V4 reset strands `/dev/modem`, whether an `arm64` image
is wanted, and whether 12 h idle / 24 h absolute matched how the panel was actually left open.
A milestone that discovered nothing is a milestone whose exercise was not demanding enough.
