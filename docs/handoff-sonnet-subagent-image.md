# Handoff: build Sonnet subagent routing into the sandboxsh image

Date: 2026-09-30
Previous session's working directory: `/workspaces/derico-sandbox` (inside a sandboxsh sandbox, shared credentials volume mounted at `/agent-creds`)

## Goal of the next session

Make every sandbox created by this repo route Claude Code subagents to Sonnet by default, without hand-made files on the credentials volume. The user decided to build it into the image after the previous session asked whether the shared volume was enough.

Three pieces, currently hand-made on `/agent-creds/claude` and verified working in Claude Code 2.1.285:

1. `CLAUDE_CODE_SUBAGENT_MODEL=sonnet` in the `env` block of the user `settings.json`.
2. A user-level `Explore` agent override: `/agent-creds/claude/agents/Explore.md` (`model: sonnet`, `omitClaudeMd: true`, built-in disallowed tools, the built-in system prompt).
3. The skill `/agent-creds/claude/skills/sonnet-delegation/SKILL.md` with the decision rules.

Copy the file bodies from those two paths; do not rewrite them.

Routing decision: Sonnet is the default, but per-call overrides remain available.
Hooks are deferred. Do not force routing, rewrite Agent inputs, or set `model`
in the delegation skill's frontmatter.

## Read first

- `/workspaces/derico-sandbox/docs/research/sonnet-delegation.md`: precedence order (per-call `model`, then frontmatter, then env var, then main model), sources. Untracked.
- `/workspaces/derico-sandbox/guest/agent-init.sh`: the boot script to extend.
- `/workspaces/derico-sandbox/tests/test_guest_agent_init.py`: the test style. Each test extracts one shell function from `agent-init.sh` by name (`agent_init_function`), stubs `install`/`chown`, runs it with `bash`, and asserts on files. Follow that pattern.
- `README.md` sections "Skills in the image" and "Host agent instructions" (around lines 416 to 460) for the documentation style and the priority rules to mirror.

## Verified in the previous session

- Routing works: general-purpose and Explore without `model` run on Sonnet 5.5, `model: haiku` gives Haiku 4.5, `model: fable` gives Fable 5.1. The Explore override is read-only and loads no CLAUDE.md.
- The skill is listed and its body loads. Whether it triggers unprompted is still unproven.
- `agent-init.sh` already merges `settings.json` with `jq` and keeps unknown keys (lines 157 to 166), so the `env` block survives a restart. `seed_agent_state` copies with `--update=none`, so it never overwrites files on the volume.
- Skills are linked per boot from two roots, `/opt/sandboxsh/host-skills/<source>/<skill>/` and `/opt/sandboxsh/image-skills/<source>/<skill>/` (`link_skills`, lines 222 to 269). A real directory in the sandbox wins, then host, then image.
- Host `settings.json` and host `agents/` are not shared into sandboxes; only host `skills/` directories and `~/.agents/AGENTS.md` are (read-only mounts, `src/sandboxsh/incus.py:74` onwards).
- A host snippet that installs all three pieces on the host was given to the user in chat. Once it has run, the skill also reaches sandboxes through the host skills mount and outranks the image copy by name.

## How files reach the image

- Stage scripts live in `guest/stages/`; `50-agents.sh` installs the image skills by `curl` from GitHub into `/opt/sandboxsh/image-skills/pstack/<skill>/`. There is no generic "copy repo file into image" step: `imagebuild.py` pushes only the stage script plus `HELPER_FILES = ("agent-init.sh", "instance-init.sh")` to `/root/` on the build worker (`src/sandboxsh/imagebuild.py:48`, `:932`), and `90-finalize.sh` installs those two under `/usr/local/sbin/`. Helper file contents are part of the finalize cache key.
- `90-finalize.sh` also copies `/home/dev/.claude` into `/opt/sandboxsh/agent-seed/claude`, which `agent-init.sh` seeds into an empty credentials volume once per image build.

So the options for shipping `Explore.md` and `SKILL.md` are: heredocs inside a stage script or inside `agent-init.sh`, or adding them to `HELPER_FILES` and pushing them in the finalize stage. Pick the one that matches the codebase; check `tests/test_imagebuild.py` (`:57`, `:167`, `:450`) before touching `HELPER_FILES`, since the file list is asserted there.

## Design points to settle

- **Env var as a default, not policy.** Use `.env.CLAUDE_CODE_SUBAGENT_MODEL //= "sonnet"` in the existing `jq` merge, like `statusLine //=`, so a value set in the sandbox or on the volume wins. Note `.env` may be absent or non-object in an existing file.
- **Explore override placement.** `$(dirname "$settings")/agents/Explore.md`: on the volume in shared mode, in `~/.claude/agents/` in project-local mode. Decide between installing a file only when absent (a sandbox file keeps priority, but image updates never propagate) and a symlink onto an image path (matches the skills pattern; updates propagate; a real file wins). Confirm first that Claude Code reads an agent definition through a symlink; the `claude-code-guide` agent type can answer that.
- **Skill placement.** Put it under `/opt/sandboxsh/image-skills/<source>/sonnet-delegation/` so the existing `link_skills` handles it and a host skill of the same name wins. Choose a `<source>` name for skills authored in this repo rather than fetched from pstack.
- **The current volume.** It holds a real `skills/sonnet-delegation/` directory and a real `agents/Explore.md`. After the image ships them, a real directory outranks the image link and would go stale. Decide whether to delete the hand-made copies on `/agent-creds/claude` once the new image is verified; ask the user before deleting.
- **Opt-out.** Someone who wants subagents on the main model sets the env var to `inherit` in their settings; with `//=` that sticks. Document it.
- **Image rebuild and recreate.** Changing `agent-init.sh` or a stage invalidates the build cache; existing VMs need the usual `sandboxsh` rebuild/recreate flow described in the README. Do not run builds unless the user asks.

## Tests to write

Real tests in `tests/test_guest_agent_init.py`, same extraction style:

- The `jq` merge sets `env.CLAUDE_CODE_SUBAGENT_MODEL` to `sonnet` on a settings file without `env`, keeps other keys, and leaves an existing value (for example `inherit`) alone. The merge is currently inline, not a function; factor it into a function so it can be extracted, and keep the existing `sandbox`/`permissions`/`statusLine` behaviour.
- The Explore override is installed (or linked) when absent and a file written inside the sandbox keeps priority.
- If a stage script or `HELPER_FILES` changes, extend `tests/test_imagebuild.py` accordingly.

Run with `uv run pytest tests/test_guest_agent_init.py` (project uses `uv`; `jq` must be on PATH or the marked tests skip).

## User rules that matter here

From the user's global `CLAUDE.md`:

- Commit only when asked. No mention of any AI agent in commit messages, no co-author trailers, commit as `MrTango`.
- No custom verification scripts; write real tests.
- Do not start dev servers or long builds unless told to.
- Keep doc strings short; README in Markdown; PR descriptions short and to the point.

## Suggested skills

- `tdd`: write the `agent-init.sh` tests first, in the existing extraction style.
- `sonnet-delegation`: invoke once so the rules are in context; the file copy and README edits are inline work, not delegation candidates.
- `code-review`: before handing the branch back, review against the README's priority rules (sandbox file, then host, then image).

The `claude-code-guide` agent type (not a skill) answers whether agent definitions in `agents/` are read through symlinks, and any other question about agent frontmatter or `settings.json` `env` handling.
