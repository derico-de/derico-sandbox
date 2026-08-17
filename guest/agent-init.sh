#!/usr/bin/env bash
# Attach shared agent state when enabled and enforce YOLO settings in both modes.
set -euo pipefail

HOME_DEV=/home/dev
if mountpoint -q /agent-creds 2>/dev/null; then
    exec 9>/agent-creds/.sandboxsh-init.lock
    flock 9

    for agent in claude pi vibe; do
        target="/agent-creds/$agent"
        install -d -m 0700 -o dev -g dev "$target"
        if [ -d "/opt/sandboxsh/agent-seed/$agent" ]; then
            cp -an "/opt/sandboxsh/agent-seed/$agent/." "$target/"
        fi
        chown dev:dev "$target"
        rm -rf "$HOME_DEV/.$agent"
        ln -s "$target" "$HOME_DEV/.$agent"
    done
    chown -h dev:dev "$HOME_DEV/.claude" "$HOME_DEV/.pi" "$HOME_DEV/.vibe"
    settings=/agent-creds/claude/settings.json
    mode=shared
else
    # No shared volume: keep agent logins/configuration on this VM's root disk,
    # but retain the exact same YOLO permission setup.
    for agent in claude pi vibe; do
        install -d -m 0700 -o dev -g dev "$HOME_DEV/.$agent"
    done
    settings="$HOME_DEV/.claude/settings.json"
    mode=project-local
fi

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

# Skills shared read-only from the host are linked into each agent's skill
# location, one symlink per skill: ~/.claude/skills for Claude Code,
# ~/.agents/skills (the cross-agent standard directory pi reads), and
# ~/.vibe/skills for Mistral Vibe. In shared mode the .claude/.vibe paths are
# symlinks onto /agent-creds, so the links land on the shared volume. An
# existing real directory (a skill installed inside a sandbox) keeps priority;
# on a host name collision the alphabetically first source wins.
host_skills=/opt/sandboxsh/host-skills
link_host_skills() {
    target_dir="$1"
    install -d -m 0755 -o dev -g dev "$target_dir"
    for link in "$target_dir"/*; do
        [ -L "$link" ] || continue
        case "$(readlink "$link")" in
            "$host_skills"/*) [ -e "$link" ] || rm -f "$link" ;;
        esac
    done
    for skill in "$host_skills"/*/*/; do
        [ -d "$skill" ] || continue
        name="$(basename "$skill")"
        target="$target_dir/$name"
        [ -e "$target" ] && continue
        ln -sn "${skill%/}" "$target"
        chown -h dev:dev "$target"
    done
}
link_host_skills "$HOME_DEV/.claude/skills"
link_host_skills "$HOME_DEV/.agents/skills"
link_host_skills "$HOME_DEV/.vibe/skills"

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
