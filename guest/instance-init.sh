#!/usr/bin/env bash
# Per-instance initialization invoked by the trusted host-side launcher.
set -euo pipefail

WANTED_UID="${1:?uid required}"
WANTED_GID="${2:?gid required}"
CURRENT_UID="$(id -u dev)"
CURRENT_GID="$(id -g dev)"

if [ "$CURRENT_GID" != "$WANTED_GID" ]; then
    if getent group "$WANTED_GID" >/dev/null; then
        echo "sandboxsh: guest gid $WANTED_GID already exists; refusing ambiguous remap" >&2
        exit 1
    fi
    groupmod --gid "$WANTED_GID" dev
fi
if [ "$CURRENT_UID" != "$WANTED_UID" ]; then
    if getent passwd "$WANTED_UID" >/dev/null; then
        echo "sandboxsh: guest uid $WANTED_UID already exists; refusing ambiguous remap" >&2
        exit 1
    fi
    usermod --uid "$WANTED_UID" --gid "$WANTED_GID" dev
    find /home/dev -xdev -uid "$CURRENT_UID" -exec chown -h "$WANTED_UID:$WANTED_GID" {} +
fi

systemctl start containerd.service docker.service
/usr/local/sbin/sandboxsh-agent-init

# Assert that the only Docker socket is the daemon inside this VM.
test -S /var/run/docker.sock
test ! -S /var/run/incus/unix.socket
test ! -S /run/incus/unix.socket
runuser -u dev -- docker compose version >/dev/null
