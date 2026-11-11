#!/usr/bin/env bash
# sandbox.sh <workspace> <oc_state_dir> <sock_dir> <cmd...>
# Runs <cmd> in a bubblewrap sandbox: the host filesystem read-only, $HOME replaced by an empty tmpfs (no
# ~/projects, no ~/steer-bc250), /run and /tmp replaced by tmpfs (no host sockets), new user/pid/ipc/uts/net
# namespaces (no host processes in /proc, no abstract unix sockets, no network).  Writable: the workspace at
# /srv/work and OpenCode's data/state/cache dir.  Read-only: ~/.config/opencode, the forwarder at /srv/tools.
# The only network path is 127.0.0.1:8080 inside, forwarded through <sock_dir>/llm.sock to the inference
# server by a host-side fwd.py (unix2tcp) that the caller runs.  Environment is cleared to a minimal set.
set -u
WS="$(readlink -f "$1")"; OC="$(readlink -f "$2")"; SK="$(readlink -f "$3")"; shift 3
TOOLS="$(cd "$(dirname "$0")" && pwd)"
H="$HOME"
HIDE=()
[ "${SANDBOX_NO_SKILLS:-0}" = 1 ] && [ -d "$H/.config/opencode/skills" ] && HIDE=(--tmpfs "$H/.config/opencode/skills")   # empty skills dir
exec /usr/bin/bwrap \
    --ro-bind / / \
    --tmpfs "$H" \
    --tmpfs /run \
    --tmpfs /tmp \
    --tmpfs /srv \
    --tmpfs /var/tmp \
    --bind "$WS" /srv/work \
    --ro-bind "$TOOLS/fwd.py" /srv/tools/fwd.py \
    --ro-bind "$SK" /srv/llm \
    --ro-bind "$H/.config/opencode" "$H/.config/opencode" \
    "${HIDE[@]}" \
    --bind "$OC" "$H/.ocrun" \
    --proc /proc --dev /dev \
    --unshare-all --die-with-parent --new-session \
    --clearenv \
    --setenv HOME "$H" --setenv USER "$(id -un)" --setenv LOGNAME "$(id -un)" \
    --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv LANG C.UTF-8 --setenv TERM dumb --setenv SHELL /bin/bash \
    --setenv XDG_DATA_HOME "$H/.ocrun/data" --setenv XDG_STATE_HOME "$H/.ocrun/state" \
    --setenv XDG_CACHE_HOME "$H/.ocrun/cache" --setenv XDG_CONFIG_HOME "$H/.config" \
    --setenv OPENCODE_CONFIG /srv/llm/opencode.json \
    --setenv OPENCODE_DISABLE_MODELS_FETCH true --setenv OPENCODE_DISABLE_AUTOUPDATE true \
    --setenv OPENCODE_DISABLE_FILEWATCHER true --setenv OPENCODE_FILEWATCHER_DISABLE true \
    --chdir /srv/work \
    -- /bin/bash -c 'python3 /srv/tools/fwd.py tcp2unix 127.0.0.1 8080 /srv/llm/llm.sock & for i in $(seq 50); do (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null && break; sleep 0.1; done; exec "$@" < /dev/null' sandbox "$@"
