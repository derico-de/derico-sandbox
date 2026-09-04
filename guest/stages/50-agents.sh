#!/usr/bin/env bash
# Stage 50: the sideshow CLI, image skills, and the agents and Python tooling
# installed as the developer.
set -euo pipefail

export NODE_OPTIONS=--dns-result-order=ipv4first

# Keep image-wide packages outside root's home while exposing their command
# shims through /usr/local/bin. Runtime installs by dev use its own PNPM_HOME.
pnpm_global() {
    PNPM_HOME=/usr/local PATH="/usr/local/bin:$PATH" \
        pnpm --global-dir /opt/pnpm/global --store-dir /opt/pnpm/store \
        add --global "$@"
}

# sideshow publishes HTML, diffs, and diagrams to a live surface the user watches
# in a browser. pi drives it through the extension installed below; every other
# agent needs the CLI, so install it globally and put `sideshow` on PATH. Which
# surface it talks to is per-user, not per-image: the CLI reads SIDESHOW_URL
# (and SIDESHOW_TOKEN for a deployed instance) at call time, and reaching a
# surface outside the VM needs that host in `firewall.allow`.
pnpm_global sideshow
sideshow --version

# Agent skills baked into the image. The host's own skills are mounted read-only
# at VM creation, but a host that keeps none still gets these. From the pstack
# plugin: `unslop` strips the tells that mark text as model-written, and `bro`
# restates the last message without jargon. Each is a single self-contained
# SKILL.md tracked at main rather than pinned, like the global packages above. The
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

# Agent and Python tooling is installed as the non-root developer.
runuser -u dev -- bash -lc '
set -euo pipefail
# runuser preserves the root caller working directory. Corepack searches it
# for package.json before launching pnpm, which the unprivileged user cannot read.
cd "$HOME"
mkdir -p "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
curl -LsSf https://astral.sh/uv/install.sh -o /tmp/uv-install.sh
sh /tmp/uv-install.sh
rm -f /tmp/uv-install.sh
curl -fsSL https://claude.ai/install.sh | bash
curl -fsSL https://pi.dev/install.sh | sh
mkdir -p "$HOME/.pi/agent"
cat > "$HOME/.pi/agent/settings.json" <<PI_SETTINGS
{"npmCommand":["pnpm"]}
PI_SETTINGS
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
