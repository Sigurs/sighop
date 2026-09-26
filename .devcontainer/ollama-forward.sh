#!/bin/sh
#
# Make the host's Ollama answer on the container's own localhost:11434.
#
# Why not just point the context engine at host.docker.internal? Because only
# some of its code paths ask `resolve_ollama_url()`, which honours
# CCE_OLLAMA_URL. The search path builds its embedding backend from
# `config.ollama_url` directly, so with the env var alone `cce search` fails
# with "Start an Ollama server at http://localhost:11434" while `cce status`
# reports the host's Ollama as reachable (code-context-engine 0.4.26).
#
# The other way to fix that is to give the container its own ~/.cce/config.yaml
# with a different ollama.url — but that file is shared with the host, where
# localhost IS the right answer, and a second copy is a second thing to keep in
# step. Forwarding the port makes every code path right at once, whichever URL
# it resolves.
#
# Run from devcontainer.json's postStartCommand, once per container start, and
# from its postAttachCommand, once per editor attach. Either way it only makes
# sure a supervisor is running and reports on the forward; the supervisor (this
# same script, with --supervise) keeps socat alive for the container's life,
# restarting it when it exits and replacing it when it stops answering.

set -u

listener=127.0.0.1:11434
upstream=host.docker.internal:11434

# Per container, so a container restart never inherits a stale lock or log.
lock=/tmp/ollama-forward.lock
log=/tmp/ollama-forward.log

# How often the supervisor checks the listener still answers, and the bounds
# of its back-off between restarts. A socat that lived past `stable` seconds
# resets the back-off, so one crash after hours restarts in `min_backoff`.
check_every=10
min_backoff=2
max_backoff=60
stable=30

say() {
    echo "$(date '+%F %T') $*" >>"$log"
}

listener_up() {
    nc -z -w 2 127.0.0.1 11434 2>/dev/null
}

supervise() {
    # One supervisor per container: every attach launches one, and all but the
    # first leave here. The lock goes with the process, so a supervisor that
    # was killed frees it for the next attach to take over.
    exec 9>"$lock"
    flock -n 9 || exit 0

    trap 'kill "$pid" "$watcher" 2>/dev/null; exit 0' TERM INT HUP
    pid=
    watcher=
    backoff=$min_backoff

    # Whatever socat is left over is either wedged or unsupervised (a previous
    # supervisor killed out from under it, or an older version of this
    # script); take the port over rather than fighting it for the bind.
    # `reuseaddr` rebinds a socket in TIME_WAIT, it does not take one off a
    # live listener. Anchored, so it matches the forward itself and not any
    # shell whose command line merely mentions it.
    pkill -f "^socat TCP-LISTEN:11434" 2>/dev/null || true
    say "supervisor $$ started"

    while :; do
        started=$(date +%s)
        # fd 9 closed for socat, or an orphaned socat would hold the lock and
        # keep every later supervisor out.
        socat "TCP-LISTEN:11434,fork,reuseaddr,bind=127.0.0.1" \
              "TCP:${upstream}" >>"$log" 2>&1 9>&- &
        pid=$!

        # A socat that is alive but no longer bound passes a process check and
        # still leaves `cce serve` unable to start, so watch the listener too.
        # socat accepts before it dials upstream, so this stays true while the
        # host's Ollama is down — which is deliberate: socat dials per
        # connection, and starts working the moment Ollama does.
        # A side loop, so the `wait` below sees an exit the moment it happens.
        (
            while sleep "$check_every" && kill -0 "$pid" 2>/dev/null; do
                if ! listener_up; then
                    say "socat $pid alive but not listening; replacing it"
                    kill "$pid" 2>/dev/null
                    sleep 1
                    kill -9 "$pid" 2>/dev/null
                    exit 0
                fi
            done
        ) 9>&- &
        watcher=$!

        wait "$pid" 2>/dev/null
        status=$?
        kill "$watcher" 2>/dev/null
        pid=

        if [ $(( $(date +%s) - started )) -ge "$stable" ]; then
            backoff=$min_backoff
        fi
        say "socat exited ($status); restarting in ${backoff}s"
        sleep "$backoff"
        backoff=$(( backoff * 2 ))
        [ "$backoff" -gt "$max_backoff" ] && backoff=$max_backoff
    done
}

if [ "${1-}" = --supervise ]; then
    supervise
    exit 0
fi

# Always launch one: it leaves at once if a supervisor already runs, and it is
# what takes over a forward started by hand or by an older version of this
# script. Detached, so it outlives the lifecycle command that started it.
already_up=0
listener_up && already_up=1
setsid nohup "$0" --supervise >/dev/null 2>&1 </dev/null &

[ "$already_up" -eq 1 ] && exit 0

upstream_up=1
if ! nc -z -w 3 host.docker.internal 11434 2>/dev/null; then
    upstream_up=0
fi

# Give it a moment, then say whether it took.
sleep 2
if ! listener_up; then
    echo "dev container: could not forward ${listener} to ${upstream}" >&2
    echo "  (the supervisor keeps retrying; see ${log})" >&2
elif [ "$upstream_up" -eq 1 ]; then
    echo "dev container: ${listener} -> the host's Ollama"
else
    echo "dev container: forwarding ${listener} to ${upstream}, but the host's" >&2
    echo "  Ollama is not answering yet. The context engine will not start until" >&2
    echo "  it does; then reconnect it with /mcp. On the host:" >&2
    echo "    ollama serve        # and make sure it listens past loopback" >&2
fi
