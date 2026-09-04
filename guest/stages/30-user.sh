#!/usr/bin/env bash
# Stage 30: the `dev` account, matched to the host uid/gid, and its directories.
set -euo pipefail

DEV_UID="${1:?host uid required}"
DEV_GID="${2:?host gid required}"

# Cloud images often already own uid/gid 1000 (for example user `debian`).
# Reuse and rename that account instead of assuming the IDs are vacant.
if ! getent group dev >/dev/null; then
    existing_group="$(getent group "$DEV_GID" | cut -d: -f1 || true)"
    if [ -n "$existing_group" ]; then
        groupmod --new-name dev "$existing_group"
    else
        groupadd --gid "$DEV_GID" dev
    fi
fi
if ! id dev >/dev/null 2>&1; then
    existing_user="$(getent passwd "$DEV_UID" | cut -d: -f1 || true)"
    if [ -n "$existing_user" ]; then
        usermod --login dev --home /home/dev --move-home "$existing_user"
        usermod --gid dev --shell /bin/bash dev
    else
        useradd --uid "$DEV_UID" --gid dev --create-home --shell /bin/bash dev
    fi
fi
usermod -aG docker,sudo dev
printf 'dev ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/dev
chmod 0440 /etc/sudoers.d/dev

install -d -m 0755 /workspaces /opt/sandboxsh/agent-seed
chown dev:dev /workspaces
