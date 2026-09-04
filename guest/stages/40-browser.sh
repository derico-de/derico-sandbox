#!/usr/bin/env bash
# Stage 40: Google Chrome, the Chrome DevTools MCP server, Playwright with its
# Chromium build, and Yazi.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export NODE_OPTIONS=--dns-result-order=ipv4first
ARCH="$(dpkg --print-architecture)"

# Keep image-wide packages outside root's home while exposing their command
# shims through /usr/local/bin. Runtime installs by dev use its own PNPM_HOME.
pnpm_global() {
    PNPM_HOME=/usr/local PATH="/usr/local/bin:$PATH" \
        pnpm --global-dir /opt/pnpm/global --store-dir /opt/pnpm/store \
        add --global "$@"
}

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
    pnpm_global chrome-devtools-mcp
    google-chrome --version
else
    printf 'sandboxsh: skipping Google Chrome on %s (amd64 only)\n' "$ARCH" >&2
fi

# Playwright: the CLI, the MCP server, and a browser build agents and project
# test suites can share. Browsers are downloaded once into a system-wide
# PLAYWRIGHT_BROWSERS_PATH instead of a per-user cache, and dev owns it so a
# project pinned to another Playwright version can add its revision beside the
# baked-in one from the allowlisted Playwright CDN.
pnpm_global playwright @playwright/mcp
install -d -m 0755 -o dev -g dev /opt/ms-playwright
PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright playwright install --with-deps chromium
rm -rf /var/lib/apt/lists/*
apt-get clean
chown -R dev:dev /opt/ms-playwright
playwright --version
