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
# Run from devcontainer.json's postStartCommand, once per container start.

set -u

listener=127.0.0.1:11434
upstream=host.docker.internal:11434

# Anchored, so it matches the forward itself and not any shell whose command
# line merely mentions it.
if pgrep -f "^socat TCP-LISTEN:11434" >/dev/null 2>&1; then
    exit 0
fi

# Forward whether or not the host's Ollama is up yet. socat dials upstream per
# connection, so a forward started before Ollama (the host still booting, say)
# starts working the moment Ollama does. Bailing out here instead left no
# forward until the next container start, and `cce serve` refuses to start at
# all without an embedding backend.
upstream_up=1
if ! nc -z -w 3 host.docker.internal 11434 2>/dev/null; then
    upstream_up=0
fi

nohup socat "TCP-LISTEN:11434,fork,reuseaddr,bind=127.0.0.1" \
            "TCP:${upstream}" >/dev/null 2>&1 &

# Give it a moment, then say whether it took.
sleep 1
if ! nc -z -w 2 127.0.0.1 11434 2>/dev/null; then
    echo "dev container: could not forward ${listener} to ${upstream}" >&2
elif [ "$upstream_up" -eq 1 ]; then
    echo "dev container: ${listener} -> the host's Ollama"
else
    echo "dev container: forwarding ${listener} to ${upstream}, but the host's" >&2
    echo "  Ollama is not answering yet. The context engine will not start until" >&2
    echo "  it does; then reconnect it with /mcp. On the host:" >&2
    echo "    ollama serve        # and make sure it listens past loopback" >&2
fi
