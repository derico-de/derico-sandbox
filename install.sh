#!/usr/bin/env bash
# Install sandboxsh and prepare Incus' restricted per-user service.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DEPS=1

usage() {
    cat <<'EOF'
Usage: ./install.sh [--no-deps]

Installs the Python CLI with pipx and prepares Incus. The user is added only to
`incus`, which exposes a restricted per-user project. It is deliberately never
added to the host-root-equivalent `incus-admin` group.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --no-deps) INSTALL_DEPS=0 ;;
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
    if ! sudo apt-get install -y incus qemu-system-x86 ovmf pipx git; then
        fail "Incus packages were unavailable. Install current Incus from https://linuxcontainers.org/incus/docs/main/installing/ and rerun with --no-deps"
    fi
fi

for command in incus pipx git; do
    command -v "$command" >/dev/null || fail "missing dependency: $command"
done
[ -e /dev/kvm ] || fail "/dev/kvm is missing; enable CPU virtualization/KVM"

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

say "Installing sandboxsh with pipx"
pipx install --force "$ROOT"
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
