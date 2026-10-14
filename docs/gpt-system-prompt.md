# Codex-derived GPT system profile for Pi

Pi automatically appends `guest/gpt-system-prompt.md` before a GPT model starts
work. `guest/gpt-system-indicator.js` gates injection on the active model ID or
name, including provider-qualified IDs, custom deployments named GPT, and Codex
model IDs. Other model families keep their normal prompt. Switching models
updates the gate on the next user turn and clears the status for non-GPT models.

The profile keeps Pi's assembled prompt, tool instructions, and project rules.
It covers Codex's practical corrections around task completion, scope and
authority, dirty worktrees, tool use, destructive actions, reviews, validation,
and concise handoffs. This is a shared, Pi-adapted profile, not a byte-for-byte
copy or a claim to reproduce every model-specific Codex runtime instruction.
Codex-only tools, approval policies, and context-management protocols are not
installed or advertised. It does not change API reasoning settings.

The status is `GPT-SYSTEM: Codex profile` in interactive Pi. No banner is printed
to stdout, so print, JSON, and RPC modes remain usable. Reading a skill manually
is not required. A missing bundled prompt is an extension load error, rather
than silently claiming the profile is active.

## Host installation

From this repository:

```bash
./install-pi-gpt-system.sh
```

The installer uses `${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}` and replaces the old
`gpt-system-indicator.ts` hook. Existing dotfiles symlinks are preserved, and the
prompt is placed next to the resolved hook, with a link in the Pi extensions
directory when needed. Unrelated extensions and settings are untouched.
Run `/reload` in existing Pi sessions, or start a new session.

## Sandboxsh guests

The finalize stage bundles the hook and prompt under `/opt/sandboxsh/pi`.
Guest agent initialization installs them into Pi's extensions directory on every
boot, refreshing older shared credential state as well as project-local state.
The filenames replace the previous hook rather than installing a second one.
Both resources enter the finalize cache key and are included in wheels.
No network access is needed to load the profile.

For new images and VMs:

```bash
uv run sandboxsh image build
uv run sandboxsh recreate
```

An existing VM does not automatically receive new image contents. It needs a
recreate or an explicit installation of the hook and prompt. Reload any running
Pi session afterward; already-running sessions cannot discover new extensions
until reload.

## Sources and refresh policy

Adapted from OpenAI's public Codex repository at commit
`0c02887a0c4d09a99eb8e078aa12ff3edc9eb331`:

- [Model catalog, including GPT-5.6 operating instructions](https://github.com/openai/codex/blob/0c02887a0c4d09a99eb8e078aa12ff3edc9eb331/codex-rs/models-manager/models.json)
- [GPT-5.2 Codex prompt](https://github.com/openai/codex/blob/0c02887a0c4d09a99eb8e078aa12ff3edc9eb331/codex-rs/core/gpt-5.2-codex_prompt.md)
- [Generic Codex prompt](https://github.com/openai/codex/blob/0c02887a0c4d09a99eb8e078aa12ff3edc9eb331/codex-rs/models-manager/prompt.md)

OpenAI Codex is Apache-2.0 licensed. The local profile paraphrases and adapts its
working guidance. Refresh deliberately: compare the upstream instructions,
update the profile and source revision, run the tests, reinstall on the host,
and rebuild the guest image. Startup does not fetch changing upstream prompts.

## Tests

```bash
uv run --extra test pytest tests/test_pi_gpt_system.py tests/test_imagebuild.py
```

Tests load the actual hook in Node, check model gating, prompt preservation,
idempotence, model switching, and non-interactive output, and exercise host and
guest installation, including symlinks and shared state.
