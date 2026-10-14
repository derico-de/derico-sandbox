#!/usr/bin/env bash
# Install the same model-gated Pi profile used by sandboxsh guests.
set -euo pipefail

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/guest"
agent_dir="${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}"
extensions="$agent_dir/extensions"
mkdir -p "$extensions"
# cp follows an existing dotfiles symlink, preserving the user's managed layout.
cp "$source_dir/gpt-system-indicator.js" "$extensions/gpt-system-indicator.ts"
hook_dir="$(dirname -- "$(realpath -- "$extensions/gpt-system-indicator.ts")")"
cp "$source_dir/gpt-system-prompt.md" "$hook_dir/gpt-system-prompt.md"
if [ "$hook_dir" != "$(cd -- "$extensions" && pwd -P)" ]; then
    # import.meta.url may resolve the symlink target; keep both paths usable.
    if [ ! -e "$extensions/gpt-system-prompt.md" ] && [ ! -L "$extensions/gpt-system-prompt.md" ]; then
        ln -s "$hook_dir/gpt-system-prompt.md" "$extensions/gpt-system-prompt.md"
    else
        cp "$source_dir/gpt-system-prompt.md" "$extensions/gpt-system-prompt.md"
    fi
fi
printf 'Installed Codex-derived GPT profile in %s. Run /reload in existing Pi sessions.\n' "$extensions"
