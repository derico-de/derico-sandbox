#!/usr/bin/env bash
# Stage 20: Docker Engine, Compose v2, Node.js 24, and pnpm.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

# Docker Engine and Compose v2 run against the VM's kernel and daemon. The host
# Docker and Incus sockets are never passed through.
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
ARCH="$(dpkg --print-architecture)"
printf 'deb [arch=%s signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian %s stable\n' \
    "$ARCH" "$VERSION_CODENAME" > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y --no-install-recommends \
    containerd.io docker-buildx-plugin docker-ce docker-ce-cli docker-compose-plugin
rm -rf /var/lib/apt/lists/*
systemctl enable docker.service containerd.service

# Node.js 24 and pnpm.
curl -fsSL https://deb.nodesource.com/setup_24.x -o /tmp/nodesource.sh
bash /tmp/nodesource.sh
rm /tmp/nodesource.sh
apt-get install -y --no-install-recommends nodejs
rm -rf /var/lib/apt/lists/*
apt-get clean
# The Incus bridge may not provide IPv6 egress even though public CDNs publish
# AAAA records. Prefer IPv4 so Node downloaders do not fail before trying it.
export NODE_OPTIONS=--dns-result-order=ipv4first
corepack enable
corepack prepare pnpm@latest --activate
