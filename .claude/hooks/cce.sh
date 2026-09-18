#!/bin/sh
#
# The context engine's Claude Code hook, made optional.
#
# `.claude/settings.json` is tracked, so whatever it names has to exist for
# everyone who opens this checkout — and on a machine where `cce` was never
# installed, it does not. This wrapper runs the real hook when it is there and
# exits 0 in silence when it is not.
#
# It also resolves what the generated hook commands hard-code: the hook script
# and the port file, under $HOME rather than one developer's home directory.
# That is what lets the dev container use the same tracked settings file — in
# there $HOME is /home/vscode and the engine's directory is bind-mounted from
# the host.
#
# NOTE: `cce init` regenerates .claude/settings.json with absolute paths and
# undoes this. Re-point the five hook commands back at this script afterwards:
#
#     "command": ".claude/hooks/cce.sh <EventName>"
#
# Usage: .claude/hooks/cce.sh <EventName>   (hook payload arrives on stdin)

set -u

hook="${HOME}/.cce/hooks/cce_hook.sh"
[ -x "$hook" ] || exit 0

# The engine stores each project under `<basename>-<sha256(abs path)[:6]>`, so
# the port file is computed, not guessed: two checkouts that share a basename
# would otherwise be indistinguishable.
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd -P)
if command -v sha256sum >/dev/null 2>&1; then
    slug="$(basename -- "$root")-$(printf %s "$root" | sha256sum | cut -c1-6)"
    port_file="${HOME}/.cce/projects/${slug}/serve.port"
    [ -e "$port_file" ] && export CCE_PORT_FILE="$port_file"
fi

# exec, so stdin reaches the hook untouched and its exit status is ours: a
# context engine that is installed and failing should still say so.
exec "$hook" "$@"
