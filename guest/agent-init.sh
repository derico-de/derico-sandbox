#!/usr/bin/env bash
# Attach shared agent state when enabled and enforce YOLO settings in both modes.
set -euo pipefail

HOME_DEV=/home/dev

# The image seeds an agent's installed packages into the shared volume once per
# image build, recorded in a stamp beside them. Repeating the copy on every boot
# walks tens of thousands of pnpm files over virtiofs for nothing, and the lock
# the caller takes does not help: flock on that mount stays inside this VM,
# so two sandboxes starting at once both copy. The loser then sees
# "File exists" for entries cp had just found missing, which used to abort
# the whole boot. That race is benign -- the other run writes the same seed --
# so only report errors that are something else.
seed_agent_state() {
    seed="$1"
    target="$2"
    seed_id="$3"
    stamp="$target/.sandboxsh-seed"
    if [ ! -d "$seed" ]; then
        return 0
    fi
    if [ "$(cat "$stamp" 2>/dev/null || true)" = "$seed_id" ]; then
        return 0
    fi
    unexpected="$(cp -a --update=none "$seed/." "$target/" 2>&1 >/dev/null |
        grep -v ': File exists$' || true)"
    if [ -n "$unexpected" ]; then
        printf '%s\n' "$unexpected" >&2
        return 1
    fi
    printf '%s\n' "$seed_id" > "$stamp"
    chown dev:dev "$stamp"
}

if mountpoint -q /agent-creds 2>/dev/null; then
    exec 9>/agent-creds/.sandboxsh-init.lock
    flock 9

    image_seed_id="$(cat /opt/sandboxsh/agent-seed/.seed-id 2>/dev/null || true)"
    for agent in claude pi vibe; do
        target="/agent-creds/$agent"
        install -d -m 0700 -o dev -g dev "$target"
        seed_agent_state "/opt/sandboxsh/agent-seed/$agent" "$target" "$image_seed_id"
        chown dev:dev "$target"
        rm -rf "$HOME_DEV/.$agent"
        ln -s "$target" "$HOME_DEV/.$agent"
    done
    chown -h dev:dev "$HOME_DEV/.claude" "$HOME_DEV/.pi" "$HOME_DEV/.vibe"
    settings=/agent-creds/claude/settings.json
    # CLAUDE_CONFIG_DIR points at the shared volume, so .claude.json lives there.
    claude_config=/agent-creds/claude/.claude.json
    mode=shared
else
    # No shared volume: keep agent logins/configuration on this VM's root disk,
    # but retain the exact same YOLO permission setup.
    for agent in claude pi vibe; do
        install -d -m 0700 -o dev -g dev "$HOME_DEV/.$agent"
    done
    settings="$HOME_DEV/.claude/settings.json"
    # Without the shared volume CLAUDE_CONFIG_DIR is unset, so Claude Code reads
    # its user configuration from the home directory instead.
    claude_config="$HOME_DEV/.claude.json"
    mode=project-local
fi

# Pi's package source syntax remains npm:<name>, but npmCommand selects pnpm for
# registry lookups and installs. Enforce it at boot so an existing shared
# configuration also picks up the image's package-manager policy.
configure_pi_package_manager() {
    local pi_settings="$1"
    local pi_existing='{}'
    local pi_tmp
    install -d -m 0700 -o dev -g dev "$(dirname "$pi_settings")"
    if [ -f "$pi_settings" ] && jq empty "$pi_settings" >/dev/null 2>&1; then
        pi_existing="$(cat "$pi_settings")"
    fi
    pi_tmp="$(mktemp)"
    printf '%s' "$pi_existing" | jq '.npmCommand=["pnpm"]' > "$pi_tmp"
    install -m 0600 -o dev -g dev "$pi_tmp" "$pi_settings"
    rm -f "$pi_tmp"
}
configure_pi_package_manager "$HOME_DEV/.pi/agent/settings.json"

# The status line lives next to settings.json: in shared mode both are on the
# shared volume, so every sandbox sees the same script at the same path.
statusline="$(dirname "$settings")/statusline.sh"
tmp="$(mktemp)"
cat > "$tmp" <<'STATUSLINE'
#!/bin/sh
# Claude Code status line: model, workspace, context, and plan-limit usage.
input=$(cat)
field() { printf '%s' "$input" | jq -r "$1"; }
model=$(field '.model.display_name // "Claude"')
current_dir=$(field '.workspace.current_dir // "."')
branch=$(git -C "$current_dir" branch --show-current 2>/dev/null || true)
pct=$(field '.context_window.used_percentage // empty' | cut -d. -f1)
used=$(field '.context_window.total_input_tokens // empty')
size=$(field '.context_window.context_window_size // empty')
five_hour_pct=$(field '.rate_limits.five_hour.used_percentage // empty' | cut -d. -f1)
week_pct=$(field '.rate_limits.seven_day.used_percentage // empty' | cut -d. -f1)
line="[$model] $(basename "$current_dir")"
[ -n "$branch" ] && line="$line ($branch)"
if [ -n "$pct" ]; then
    ctx="ctx ${pct}%"
    [ -n "$used" ] && [ -n "$size" ] && ctx="$ctx ($((used / 1000))k/$((size / 1000))k)"
    line="$line | $ctx"
fi
[ -n "$five_hour_pct" ] && line="$line | 5h ${five_hour_pct}%"
[ -n "$week_pct" ] && line="$line | week ${week_pct}%"
printf '%s\n' "$line"
STATUSLINE
install -m 0755 -o dev -g dev "$tmp" "$statusline"

# Claude's in-tool sandbox and permission prompts are intentionally disabled:
# the hardware VM and host-enforced network ACL are the security boundary.
# The status line is a default, not policy: an existing statusLine wins.
existing='{}'
if [ -f "$settings" ] && jq empty "$settings" >/dev/null 2>&1; then
    existing="$(cat "$settings")"
fi
printf '%s' "$existing" | jq --arg statusline "$statusline" '
    .sandbox={"enabled":false,"failIfUnavailable":false}
    | .permissions.defaultMode="bypassPermissions"
    | .statusLine //= {"type":"command","command":$statusline}' > "$tmp"
install -m 0600 -o dev -g dev "$tmp" "$settings"
rm -f "$tmp"

# Chrome DevTools MCP gives Claude Code a real browser to drive and inspect.
# Chrome runs headless because the VM has no display, and each server gets an
# isolated profile so several agents in one VM do not collide on a single Chrome
# user-data directory. Google's telemetry and update checks are off: the ACL
# blocks those endpoints anyway, and waiting on them only costs startup time.
# MCP servers are configured in .claude.json -- settings.json ignores them --
# and an entry configured inside the sandbox keeps priority.
register_chrome_devtools_mcp() {
    mcp_config="$1"
    command -v chrome-devtools-mcp >/dev/null 2>&1 || return 0
    command -v google-chrome >/dev/null 2>&1 || return 0
    mcp_existing='{}'
    if [ -f "$mcp_config" ] && jq empty "$mcp_config" >/dev/null 2>&1; then
        mcp_existing="$(cat "$mcp_config")"
    fi
    mcp_tmp="$(mktemp)"
    printf '%s' "$mcp_existing" | jq '
        .mcpServers["chrome-devtools"] //= {
            "command": "chrome-devtools-mcp",
            "args": [
                "--headless",
                "--isolated",
                "--no-usage-statistics",
                "--no-performance-crux"
            ],
            "env": {"CHROME_DEVTOOLS_MCP_NO_UPDATE_CHECKS": "1"}
        }' > "$mcp_tmp"
    install -m 0600 -o dev -g dev "$mcp_tmp" "$mcp_config"
    rm -f "$mcp_tmp"
}
register_chrome_devtools_mcp "$claude_config"

# Playwright MCP is the second browser: it drives Playwright's own Chromium
# through accessibility snapshots rather than the DevTools protocol, which suits
# filling forms and walking end-to-end flows, and it is the same engine a
# project's Playwright tests use. Headless and isolated for the reasons above.
register_playwright_mcp() {
    mcp_config="$1"
    command -v playwright-mcp >/dev/null 2>&1 || return 0
    mcp_existing='{}'
    if [ -f "$mcp_config" ] && jq empty "$mcp_config" >/dev/null 2>&1; then
        mcp_existing="$(cat "$mcp_config")"
    fi
    mcp_tmp="$(mktemp)"
    printf '%s' "$mcp_existing" | jq '
        .mcpServers["playwright"] //= {
            "command": "playwright-mcp",
            "args": ["--headless", "--isolated"]
        }' > "$mcp_tmp"
    install -m 0600 -o dev -g dev "$mcp_tmp" "$mcp_config"
    rm -f "$mcp_tmp"
}
register_playwright_mcp "$claude_config"

# Skills are linked into each agent's skill location, one symlink per skill:
# ~/.claude/skills for Claude Code, ~/.agents/skills (the cross-agent standard
# directory pi reads), and ~/.vibe/skills for Mistral Vibe. In shared mode the
# .claude/.vibe paths are symlinks onto /agent-creds, so the links land on the
# shared volume. Two roots hold <source>/<skill>/ trees: the host's skills,
# mounted read-only at VM creation, and the ones baked into the image. An
# existing real directory (a skill installed inside a sandbox) keeps priority,
# then the host, then the image; on a name collision within one root the
# alphabetically first source wins.
host_skills=/opt/sandboxsh/host-skills
image_skills=/opt/sandboxsh/image-skills
clean_skill_links() {
    target_dir="$1"
    shift
    install -d -m 0755 -o dev -g dev "$target_dir"
    for link in "$target_dir"/*; do
        [ -L "$link" ] || continue
        link_target="$(readlink "$link")"
        for root in "$@"; do
            case "$link_target" in
                "$root"/*)
                    [ -e "$link" ] || rm -f "$link"
                    break
                    ;;
            esac
        done
    done
}
link_skills() {
    root="$1"
    target_dir="$2"
    install -d -m 0755 -o dev -g dev "$target_dir"
    for skill in "$root"/*/*/; do
        [ -d "$skill" ] || continue
        name="$(basename "$skill")"
        target="$target_dir/$name"
        if [ -e "$target" ] || [ -L "$target" ]; then
            continue
        fi
        ln -sn "${skill%/}" "$target"
        chown -h dev:dev "$target"
    done
}
for skills_dir in "$HOME_DEV/.claude/skills" "$HOME_DEV/.agents/skills" "$HOME_DEV/.vibe/skills"; do
    clean_skill_links "$skills_dir" "$host_skills" "$image_skills"
    link_skills "$host_skills" "$skills_dir"
    link_skills "$image_skills" "$skills_dir"
done

# The host's user-level AGENTS.md is shared read-only under the same reasoning,
# so agents in the VM follow the same rules as on the host. Pi and other agents
# read ~/.agents/AGENTS.md, Claude Code reads ~/.claude/CLAUDE.md; both become
# symlinks onto the read-only mount. A real file written inside the sandbox
# keeps priority, and a link left behind by a removed host file is cleaned up.
host_instructions=/opt/sandboxsh/host-instructions/AGENTS.md
link_host_instructions() {
    target="$1"
    if [ -L "$target" ]; then
        case "$(readlink "$target")" in
            "$host_instructions") [ -e "$target" ] || rm -f "$target" ;;
        esac
    fi
    [ -f "$host_instructions" ] || return 0
    if [ -e "$target" ] && [ ! -L "$target" ]; then
        return 0
    fi
    ln -sfn "$host_instructions" "$target"
    chown -h dev:dev "$target"
}
link_host_instructions "$HOME_DEV/.agents/AGENTS.md"
link_host_instructions "$HOME_DEV/.claude/CLAUDE.md"

echo "sandboxsh: $mode agent configuration initialized."
