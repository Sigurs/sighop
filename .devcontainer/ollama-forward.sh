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

if pgrep -f "TCP-LISTEN:11434" >/dev/null 2>&1; then
    exit 0
fi

if ! nc -z -w 3 host.docker.internal 11434 2>/dev/null; then
    echo "dev container: the host's Ollama is not reachable at ${upstream}." >&2
    echo "  The context engine will still search; it needs Ollama to embed new" >&2
    echo "  queries, so searches will fail until it is up. On the host:" >&2
    echo "    ollama serve        # and make sure it listens past loopback" >&2
    exit 0
fi

nohup socat "TCP-LISTEN:11434,fork,reuseaddr,bind=127.0.0.1" \
            "TCP:${upstream}" >/dev/null 2>&1 &

# Give it a moment, then say whether it took.
sleep 1
if nc -z -w 2 127.0.0.1 11434 2>/dev/null; then
    echo "dev container: ${listener} -> the host's Ollama"
else
    echo "dev container: could not forward ${listener} to ${upstream}" >&2
fi
