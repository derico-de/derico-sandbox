#!/usr/bin/env bash
# Build the sandboxsh golden Incus VM image. Runs as root in a disposable builder.
set -euo pipefail

DEV_UID="${1:?host uid required}"
DEV_GID="${2:?host gid required}"
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y --no-install-recommends \
    bash-completion build-essential ca-certificates curl fd-find git gnupg iproute2 jq \
    less libcairo2 libffi-dev libgdk-pixbuf-2.0-0 libldap2-dev libpango-1.0-0 \
    libpangocairo-1.0-0 libsasl2-dev locales neovim openssh-client \
    postgresql-client procps python3 python3-dev python3-pip python3-venv ripgrep \
    rsync shared-mime-info sudo tig tree unzip vim wget
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

# Yazi, a terminal file manager. Debian has no package for it, so take the
# upstream release build; upstream ships gnu binaries for these two targets only.
case "$ARCH" in
    amd64) YAZI_TARGET=x86_64-unknown-linux-gnu ;;
    arm64) YAZI_TARGET=aarch64-unknown-linux-gnu ;;
    *) YAZI_TARGET="" ;;
esac
if [ -n "$YAZI_TARGET" ]; then
    curl -fsSL "https://github.com/sxyazi/yazi/releases/latest/download/yazi-$YAZI_TARGET.zip" \
        -o /tmp/yazi.zip
    unzip -q -d /tmp/yazi /tmp/yazi.zip
    install -m 0755 "/tmp/yazi/yazi-$YAZI_TARGET/yazi" "/tmp/yazi/yazi-$YAZI_TARGET/ya" \
        /usr/local/bin/
    rm -rf /tmp/yazi /tmp/yazi.zip
    yazi --version
else
    printf 'sandboxsh: skipping yazi on %s (no upstream build)\n' "$ARCH" >&2
fi

# Google Chrome and the Chrome DevTools MCP server, so agents can drive and
# inspect a real browser. Chrome's own postinst would re-add this repository
# with a legacy keyring; repo_add_once=false keeps the pinned entry below the
# only one. Google publishes Chrome for amd64 only -- on another architecture
# the image is built without a browser and agent-init skips the MCP server.
if [ "$ARCH" = "amd64" ]; then
    curl -fsSL https://dl.google.com/linux/linux_signing_key.pub \
        -o /etc/apt/keyrings/google-chrome.asc
    chmod a+r /etc/apt/keyrings/google-chrome.asc
    printf 'deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.asc] https://dl.google.com/linux/chrome/deb/ stable main\n' \
        > /etc/apt/sources.list.d/google-chrome.list
    printf 'repo_add_once=false\n' > /etc/default/google-chrome
    apt-get update
    apt-get install -y --no-install-recommends google-chrome-stable
    rm -rf /var/lib/apt/lists/*
    npm install -g chrome-devtools-mcp
    google-chrome --version
else
    printf 'sandboxsh: skipping Google Chrome on %s (amd64 only)\n' "$ARCH" >&2
fi

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

# Playwright: the CLI, the MCP server, and a browser build agents and project
# test suites can share. Browsers are downloaded once into a system-wide
# PLAYWRIGHT_BROWSERS_PATH instead of a per-user cache, and dev owns it so a
# project pinned to another Playwright version can add its revision beside the
# baked-in one from the allowlisted Playwright CDN.
npm install -g playwright @playwright/mcp
install -d -m 0755 -o dev -g dev /opt/ms-playwright
PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright playwright install --with-deps chromium
rm -rf /var/lib/apt/lists/*
chown -R dev:dev /opt/ms-playwright
playwright --version

# sideshow publishes HTML, diffs, and diagrams to a live surface the user watches
# in a browser. pi drives it through the extension installed below; every other
# agent needs the CLI, so install it globally and put `sideshow` on PATH. Which
# surface it talks to is per-user, not per-image: the CLI reads SIDESHOW_URL
# (and SIDESHOW_TOKEN for a deployed instance) at call time, and reaching a
# surface outside the VM needs that host in `firewall.allow`.
npm install -g sideshow
sideshow --version

# Agent skills baked into the image. The host's own skills are mounted read-only
# at VM creation, but a host that keeps none still gets these. From the pstack
# plugin: `unslop` strips the tells that mark text as model-written, and `bro`
# restates the last message without jargon. Each is a single self-contained
# SKILL.md tracked at main rather than pinned, like the npm globals above. The
# layout mirrors the host mount -- <source>/<skill>/ -- so agent-init links both
# roots with one function, and a skill of the same name from the host or from
# inside a sandbox keeps priority. The grep is the build-time smoke test: a 404
# body or a renamed skill fails the build here.
pstack_raw=https://raw.githubusercontent.com/cursor/plugins/main/pstack/skills
for skill in unslop bro; do
    install -d -m 0755 "/opt/sandboxsh/image-skills/pstack/$skill"
    curl -fsSL "$pstack_raw/$skill/SKILL.md" \
        -o "/opt/sandboxsh/image-skills/pstack/$skill/SKILL.md"
    grep -qx "name: $skill" "/opt/sandboxsh/image-skills/pstack/$skill/SKILL.md"
done

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
"$HOME/.local/bin/uv" tool install "plonecli>=7.0.0b14"
pi install npm:pi-subagents
pi install npm:pi-impeccable
pi install npm:sideshow
pi install npm:@narumitw/pi-firecrawl
'

cat > /etc/profile.d/sandboxsh.sh <<'PROFILE'
export SANDBOXSH=1
export DEVCONTAINER=true
export PATH="$HOME/.local/bin:$PATH"
# pnpm 11 stores a SQLite index beside its package content. Keep it off the
# virtiofs/9p project mount, where SQLite WAL/mmap may fail with SQLITE_IOERR.
export PNPM_CONFIG_STORE_DIR="$HOME/.local/share/pnpm/store"
export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright
# Firecrawl's Pi extension reads its credential from the environment. Keep the
# key out of the image: a user may add it at runtime to Pi's credential state,
# which is shared between VMs when agent_credentials is enabled and VM-local
# otherwise.
firecrawl_key_file="$HOME/.pi/firecrawl-api-key"
if [ -r "$firecrawl_key_file" ]; then
    export FIRECRAWL_API_KEY="$(cat "$firecrawl_key_file")"
fi
unset firecrawl_key_file
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
