#!/usr/bin/env bash
# Install sandboxsh and prepare Incus' restricted per-user service.
set -euo pipefail

REPOSITORY_URL="${SANDBOXSH_REPOSITORY_URL:-https://github.com/derico-de/derico-sandbox.git}"
INSTALL_REF="${SANDBOXSH_INSTALL_REF:-main}"
SCRIPT_PATH="${BASH_SOURCE[0]:-}"
INSTALL_DEPS=1

# Prefer the checkout when this script is run from one. When it is streamed to
# bash (or downloaded by itself), let pipx clone and build the package directly
# from GitHub instead.
if [ -n "$SCRIPT_PATH" ] && [ -f "$SCRIPT_PATH" ]; then
    ROOT="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
else
    ROOT=""
fi
if [ -n "${SANDBOXSH_INSTALL_SOURCE:-}" ]; then
    INSTALL_SOURCE="$SANDBOXSH_INSTALL_SOURCE"
elif [ -n "$ROOT" ] && [ -f "$ROOT/pyproject.toml" ] && [ -d "$ROOT/src/sandboxsh" ]; then
    INSTALL_SOURCE="$ROOT"
else
    INSTALL_SOURCE="git+$REPOSITORY_URL@$INSTALL_REF"
fi

usage() {
    cat <<'EOF'
Usage: ./install.sh [--no-deps] [--forward-unit] [--publish-helper]
       curl -fsSL https://raw.githubusercontent.com/derico-de/derico-sandbox/main/install.sh | bash

Installs the Python CLI with pipx and prepares Incus. The user is added only to
`incus`, which exposes a restricted per-user project. It is deliberately never
added to the host-root-equivalent `incus-admin` group.

  --no-deps         Skip apt dependency installation.
  --forward-unit    Install a systemd unit that re-adds the iptables allow rule
                    for this user's Incus bridge at boot. Needed when Docker or
                    podman sets the IPv4 FORWARD policy to DROP, which otherwise
                    blackholes every sandbox after each reboot.
  --publish-helper  Install the helper that publishes declared development ports
                    on this host's tailnet address, so `sandboxsh up` can offer
                    them as <tailnet-node>:<port>. Without it, declared ports stay
                    reachable only at the VM's private address.
EOF
}

FORWARD_UNIT=0
PUBLISH_HELPER=0
while [ $# -gt 0 ]; do
    case "$1" in
        --no-deps) INSTALL_DEPS=0 ;;
        --forward-unit) FORWARD_UNIT=1 ;;
        --publish-helper) PUBLISH_HELPER=1 ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
fail() { echo "install.sh: $*" >&2; exit 1; }

if [ "$INSTALL_DEPS" = 1 ]; then
    command -v apt-get >/dev/null || fail "automatic dependency install currently supports apt hosts"
    say "Installing host dependencies"
    sudo apt-get update
    if ! sudo apt-get install -y incus qemu-system-x86 ovmf pipx git kmod; then
        fail "Incus packages were unavailable. Install current Incus from https://linuxcontainers.org/incus/docs/main/installing/ and rerun with --no-deps"
    fi
fi

for command in incus pipx git modprobe; do
    command -v "$command" >/dev/null || fail "missing dependency: $command"
done

# Incus before 6.0.6/6.22.0 starts a bridged NIC by resolving `security.acls` in
# the instance's project, while ACLs can only be created in the `default` network
# project. Sandboxes on such a daemon never start.
incus_acl_lookup_broken() {
    awk -v version="$1" 'BEGIN {
        split(version, part, ".")
        major = part[1] + 0; minor = part[2] + 0; patch = part[3] + 0
        if (major != 6) { exit major < 6 ? 0 : 1 }
        if (minor == 0) { exit patch < 6 ? 0 : 1 }
        exit minor < 22 ? 0 : 1
    }'
}

INCUS_VERSION="$(incus --version 2>/dev/null | head -n1)"
if [ -n "$INCUS_VERSION" ] && incus_acl_lookup_broken "$INCUS_VERSION"; then
    fail "Incus $INCUS_VERSION cannot enforce per-VM network ACLs in a restricted user project (fixed upstream in 6.0.6 and 6.22.0). Install a newer Incus, for example from https://github.com/zabbly/incus, then rerun with --no-deps"
fi
[ -e /dev/kvm ] || fail "/dev/kvm is missing; enable CPU virtualization/KVM"

say "Loading bridge netfilter support"
sudo modprobe br_netfilter
printf 'br_netfilter\n' | sudo tee /etc/modules-load.d/sandboxsh.conf >/dev/null

say "Enabling Incus services"
sudo systemctl enable --now incus.socket incus-user.socket
if ! sudo incus admin waitready --timeout=30 >/dev/null 2>&1; then
    fail "Incus daemon did not become ready"
fi

# A fresh daemon has no storage/network. `--minimal` creates the defaults used by
# incus-user's restricted user project. Do not reinitialize an existing daemon.
if ! sudo incus storage show default >/dev/null 2>&1; then
    say "Initializing Incus with its minimal managed storage/network defaults"
    sudo incus admin init --minimal
fi

if getent group incus-admin | cut -d: -f4 | tr ',' '\n' | grep -qx "$USER"; then
    fail "$USER is explicitly listed in incus-admin. Remove that root-equivalent membership before using sandboxsh"
fi

if ! id -nG "$USER" | tr ' ' '\n' | grep -qx incus; then
    say "Adding $USER to the restricted incus group"
    sudo usermod -aG incus "$USER"
    NEED_LOGIN=1
else
    NEED_LOGIN=0
fi

# incus-user names the per-user bridge after the uid, shortening it when the
# interface name would exceed the 15-character kernel limit.
user_bridge() {
    uid="$(id -u)"
    name="incusbr-$uid"
    if [ "${#name}" -gt 15 ]; then
        name="user-$uid"
    fi
    # Prefer what Incus actually attached, when the group membership is active.
    actual="$(incus --force-local --project "user-$uid" profile device get default eth0 network 2>/dev/null || true)"
    if [ -n "$actual" ]; then
        name="$actual"
    fi
    printf '%s\n' "$name"
}

if [ "$FORWARD_UNIT" = 1 ]; then
    BRIDGE="$(user_bridge)"
    say "Installing the boot-time forward rule for bridge $BRIDGE"

    # The helper adds two rules and nothing else. It never flushes a chain, so a
    # partially applied host firewall cannot be made worse by running it, and it
    # is safe to run repeatedly.
    sudo tee /usr/local/sbin/sandboxsh-bridge-forward >/dev/null <<'HELPER'
#!/bin/sh
# Allow forwarded traffic for one Incus bridge. Installed by sandboxsh.
set -eu

BRIDGE="${1:?bridge name required}"

# Nothing to police without iptables. Exit clean so boot is never held up.
command -v iptables >/dev/null 2>&1 || exit 0

# Docker preserves DOCKER-USER across daemon restarts, so prefer it when it is
# already there. Otherwise go straight into FORWARD, which works whether or not
# a container runtime ever starts, and stays valid when one starts later.
CHAIN=FORWARD
if iptables -n -L DOCKER-USER >/dev/null 2>&1; then
    CHAIN=DOCKER-USER
fi

# An iptables interface match does not require the interface to exist, so this
# is correct even though the per-user bridge appears only on first sandbox use.
iptables -C "$CHAIN" -i "$BRIDGE" -j ACCEPT 2>/dev/null ||
    iptables -I "$CHAIN" -i "$BRIDGE" -j ACCEPT
iptables -C "$CHAIN" -o "$BRIDGE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT 2>/dev/null ||
    iptables -I "$CHAIN" -o "$BRIDGE" -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
HELPER
    sudo chmod 0755 /usr/local/sbin/sandboxsh-bridge-forward

    # Ordering only: After= on an absent unit is inert, and nothing Requires=
    # this service, so a failure here can never block the boot.
    sudo tee /etc/systemd/system/sandboxsh-bridge-forward.service >/dev/null <<UNIT
[Unit]
Description=Allow forwarded traffic for the sandboxsh Incus bridge $BRIDGE
Documentation=https://github.com/derico-de/derico-sandbox
After=network-online.target docker.service podman.service incus.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/sandboxsh-bridge-forward $BRIDGE

[Install]
WantedBy=multi-user.target
UNIT
    sudo systemctl daemon-reload
    sudo systemctl enable --now sandboxsh-bridge-forward.service
fi

if [ "$PUBLISH_HELPER" = 1 ]; then
    say "Installing the tailnet port publishing helper"

    # sandboxsh calls this with sudo and nothing else: two verbs, fixed unit
    # names derived from a validated instance name, and no shell interpolation
    # of caller data into the units. It only ever touches units it wrote.
    sudo tee /usr/local/sbin/sandboxsh-publish-port >/dev/null <<'HELPER'
#!/bin/sh
# Publish sandbox guest ports on a host address. Installed by sandboxsh.
#
#   sync <instance> <listen-ip> <guest-ip> <hostport>:<guestport>...
#   clear <instance>
#
# Each mapping becomes a socket-activated systemd-socket-proxyd listener. The
# host dials the guest over the Incus bridge, so the guest sees the bridge
# gateway as the source and the VM's own ACL keeps deciding what is reachable.
set -eu

UNIT_DIR=/etc/systemd/system
PREFIX=sandboxsh-publish

die() { echo "sandboxsh-publish-port: $*" >&2; exit 1; }

valid_instance() {
    printf '%s\n' "$1" | grep -Eq '^[a-zA-Z0-9][a-zA-Z0-9.-]{0,62}$'
}

valid_port() {
    case "$1" in ''|*[!0-9]*) return 1 ;; esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

valid_ipv4() {
    printf '%s\n' "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$' || return 1
    old_ifs=$IFS
    IFS=.
    # shellcheck disable=SC2086
    set -- $1
    IFS=$old_ifs
    for octet in "$@"; do
        [ "$octet" -le 255 ] || return 1
    done
    return 0
}

find_proxy() {
    for candidate in \
        /usr/lib/systemd/systemd-socket-proxyd \
        /lib/systemd/systemd-socket-proxyd; do
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    die "systemd-socket-proxyd is missing; install the systemd package"
}

# Print each socket unit this tool wrote for one instance.
existing_units() {
    for unit in "$UNIT_DIR/$PREFIX-$1-"*.socket; do
        [ -e "$unit" ] || continue
        printf '%s\n' "$(basename "$unit" .socket)"
    done
}

remove_unit() {
    systemctl disable --now "$1.socket" >/dev/null 2>&1 || true
    systemctl stop "$1.service" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$1.socket" "$UNIT_DIR/$1.service"
}

# Replace a unit only when its content actually changes, so re-running `up`
# does not drop live connections to unchanged ports.
install_unit() {
    path="$1"
    temporary="$path.sandboxsh-tmp"
    cat >"$temporary"
    chmod 0644 "$temporary"
    if [ -f "$path" ] && cmp -s "$temporary" "$path"; then
        rm -f "$temporary"
        return 1
    fi
    mv "$temporary" "$path"
    return 0
}

command="${1:-}"
[ -n "$command" ] || die "usage: sandboxsh-publish-port sync|clear ..."
shift

instance="${1:-}"
valid_instance "$instance" || die "invalid instance name: $instance"
shift

case "$command" in
clear)
    changed=0
    for base in $(existing_units "$instance"); do
        remove_unit "$base"
        changed=1
    done
    if [ "$changed" = 1 ]; then
        systemctl daemon-reload
    fi
    exit 0
    ;;
sync) ;;
*) die "unknown command: $command" ;;
esac

listen="${1:-}"
valid_ipv4 "$listen" || die "invalid listen address: $listen"
shift
guest="${1:-}"
valid_ipv4 "$guest" || die "invalid guest address: $guest"
shift
[ "$#" -gt 0 ] || die "sync requires at least one <hostport>:<guestport> mapping"

PROXY="$(find_proxy)"
wanted=""
reload=0
rebind=""
rebackend=""

for mapping in "$@"; do
    host_port="${mapping%%:*}"
    guest_port="${mapping##*:}"
    valid_port "$host_port" || die "invalid host port: $host_port"
    valid_port "$guest_port" || die "invalid guest port: $guest_port"
    base="$PREFIX-$instance-$host_port"
    wanted="$wanted $base"

    # FreeBind lets the listener come up before tailscaled has assigned the
    # address, which is the normal ordering after a reboot.
    if install_unit "$UNIT_DIR/$base.socket" <<UNIT
[Unit]
Description=sandboxsh published port $host_port for $instance
Documentation=https://github.com/derico-de/derico-sandbox

[Socket]
ListenStream=$listen:$host_port
FreeBind=yes

[Install]
WantedBy=sockets.target
UNIT
    then
        reload=1
        rebind="$rebind $base"
    fi

    if install_unit "$UNIT_DIR/$base.service" <<UNIT
[Unit]
Description=sandboxsh proxy for $instance port $guest_port
Documentation=https://github.com/derico-de/derico-sandbox
Requires=$base.socket
After=$base.socket

[Service]
ExecStart=$PROXY $guest:$guest_port
DynamicUser=yes
NoNewPrivileges=yes
PrivateTmp=yes
ProtectHome=yes
ProtectSystem=strict
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
UNIT
    then
        reload=1
        rebackend="$rebackend $base"
    fi
done

# Withdraw ports this instance no longer declares.
for base in $(existing_units "$instance"); do
    keep=0
    for candidate in $wanted; do
        if [ "$base" = "$candidate" ]; then
            keep=1
        fi
    done
    if [ "$keep" = 0 ]; then
        remove_unit "$base"
        reload=1
    fi
done

if [ "$reload" = 1 ]; then
    systemctl daemon-reload
fi

# `start` is a no-op on an already-listening socket, so unchanged ports keep
# serving untouched; only a rewritten listener is rebound.
for base in $wanted; do
    systemctl enable "$base.socket" >/dev/null 2>&1 || die "cannot enable $base.socket"
    action=start
    for candidate in $rebind; do
        if [ "$base" = "$candidate" ]; then
            action=restart
        fi
    done
    systemctl "$action" "$base.socket" || die "cannot $action $base.socket"
done

# A proxy already running against the previous guest address must not keep
# serving it; socket activation starts a fresh one on the next connection.
for base in $rebackend; do
    systemctl stop "$base.service" >/dev/null 2>&1 || true
done
HELPER
    sudo chmod 0755 /usr/local/sbin/sandboxsh-publish-port
fi

say "Installing sandboxsh with pipx from $INSTALL_SOURCE"
pipx install --force "$INSTALL_SOURCE"
SANDBOXSH_BIN="${PIPX_BIN_DIR:-$HOME/.local/bin}/sandboxsh"
[ -x "$SANDBOXSH_BIN" ] || fail "pipx installed sandboxsh but $SANDBOXSH_BIN is missing"

if [ "$NEED_LOGIN" = 1 ]; then
    echo
    echo "Log out and back in so the new incus group membership applies. Then run:"
    echo "  sandboxsh doctor"
    echo "  sandboxsh image build"
else
    "$SANDBOXSH_BIN" doctor
    echo
    echo "Next, build the shared VM image once:"
    echo "  sandboxsh image build"
fi
