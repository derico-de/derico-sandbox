#!/usr/bin/env bash
# Build the sandboxsh golden Incus VM image. Runs as root in a disposable builder.
set -euo pipefail

DEV_UID="${1:?host uid required}"
DEV_GID="${2:?host gid required}"
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
    bash-completion build-essential ca-certificates curl fd-find git gnupg iproute2 jq \
    less locales neovim openssh-client postgresql-client procps python3 python3-dev \
    python3-pip python3-venv ripgrep rsync sudo tree unzip vim wget
rm -rf /var/lib/apt/lists/*
ln -sf "$(command -v fdfind)" /usr/local/bin/fd
update-alternatives --set editor /usr/bin/nvim || true

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
corepack enable
corepack prepare pnpm@latest --activate

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

# Keep git publication behind a deliberate host/human step. Guest root can
# bypass this (Docker membership makes guest root part of the threat model), but
# it prevents routine autonomous pushes and accidental publication.
install -d -m 0755 /etc/git-hooks
cat > /etc/git-hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
[ "${SANDBOXSH_ALLOW_PUSH:-0}" = "1" ] && exit 0
echo "sandboxsh: git push is disabled in YOLO mode." >&2
echo "Review on the host, or deliberately use SANDBOXSH_ALLOW_PUSH=1." >&2
exit 1
HOOK
chmod 0755 /etc/git-hooks/pre-push
git config --system core.hooksPath /etc/git-hooks

# Agent and Python tooling is installed as the non-root developer.
runuser -u dev -- bash -lc '
set -euo pipefail
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
sh /tmp/uv-install.sh
rm -f /tmp/uv-install.sh
curl -fsSL https://claude.ai/install.sh | bash
curl -fsSL https://pi.dev/install.sh | sh
"$HOME/.local/bin/uv" tool install --python 3.12 mistral-vibe
"$HOME/.local/bin/uv" tool install ruff
"$HOME/.local/bin/uv" tool install pytest
"$HOME/.local/bin/uv" tool install tox --with tox-uv
"$HOME/.local/bin/uv" tool install invoke --with tomlkit
pi install npm:pi-subagents
pi install npm:pi-impeccable
pi install npm:sideshow
'

cat > /etc/profile.d/sandboxsh.sh <<'PROFILE'
export SANDBOXSH=1
export DEVCONTAINER=true
export PATH="$HOME/.local/bin:$PATH"
if [ -d /agent-creds/claude ]; then
    export CLAUDE_CONFIG_DIR=/agent-creds/claude
fi
PROFILE
chmod 0644 /etc/profile.d/sandboxsh.sh

# Seed data is copied to the shared managed volume on first use. It contains
# installed packages/settings, never authentication material from the host.
for agent in claude pi vibe; do
    mkdir -p "/home/dev/.$agent"
    cp -a "/home/dev/.$agent" "/opt/sandboxsh/agent-seed/$agent"
done
chown -R dev:dev /opt/sandboxsh/agent-seed /home/dev

install -m 0755 /root/agent-init.sh /usr/local/sbin/sandboxsh-agent-init
install -m 0755 /root/instance-init.sh /usr/local/sbin/sandboxsh-instance-init

# Remove transient builder state. cloud-init performs its own clean just before
# publication; these remove package and shell residue from the golden image.
rm -rf /tmp/* /var/tmp/* /root/.cache /root/.bash_history /home/dev/.bash_history
apt-get clean

docker compose version
printf 'sandboxsh golden image provisioned for uid=%s gid=%s\n' "$DEV_UID" "$DEV_GID"
