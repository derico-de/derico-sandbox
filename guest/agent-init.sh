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

# Claude's in-tool sandbox and permission prompts are intentionally disabled:
# the hardware VM and host-enforced network ACL are the security boundary.
tmp="$(mktemp)"
if [ -f "$settings" ] && jq empty "$settings" >/dev/null 2>&1; then
    jq '.sandbox={"enabled":false,"failIfUnavailable":false}
        | .permissions.defaultMode="bypassPermissions"' "$settings" > "$tmp"
else
    printf '%s\n' '{"sandbox":{"enabled":false,"failIfUnavailable":false},"permissions":{"defaultMode":"bypassPermissions"}}' > "$tmp"
fi
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

echo "sandboxsh: $mode agent configuration initialized."
