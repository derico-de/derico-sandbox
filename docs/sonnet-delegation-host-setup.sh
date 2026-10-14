#!/usr/bin/env bash
# Route Claude Code subagents to Sonnet on the host: the same three pieces
# the sandboxes carry on /agent-creds/claude. Needs jq and Claude Code
# 2.1.284 or later (the `sonnet` alias means Sonnet 5.5 from that version).
# Overwrites agents/Explore.md and skills/sonnet-delegation/SKILL.md;
# settings.json is backed up to settings.json.bak first.
set -eu
dir="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
mkdir -p "$dir/agents" "$dir/skills/sonnet-delegation"

# 1. Subagents default to Sonnet
[ -f "$dir/settings.json" ] || echo '{}' > "$dir/settings.json"
cp "$dir/settings.json" "$dir/settings.json.bak"
jq '.env.CLAUDE_CODE_SUBAGENT_MODEL = "sonnet"' "$dir/settings.json.bak" > "$dir/settings.json"

# 2. Explore override on Sonnet
cat > "$dir/agents/Explore.md" <<'EOF'
---
name: Explore
description: Read-only search agent for broad fan-out searches — when answering means sweeping many files, directories, or naming conventions and you only need the conclusion, not the file dumps. It reads excerpts rather than whole files, so it locates code; it doesn't review or audit it. Specify search breadth: "medium" for moderate exploration, "very thorough" for multiple locations and naming conventions.
model: sonnet
disallowedTools: Agent, Artifact, ArtifactComments, ArtifactData, ArtifactCheck, ExitPlanMode, Edit, Write, NotebookEdit
omitClaudeMd: true
---

You are a file search specialist for Claude Code, Anthropic's official CLI for Claude. You excel at thoroughly navigating and exploring codebases.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===
This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your role is EXCLUSIVELY to search and analyze existing code. You do NOT have access to file editing tools - attempting to edit files will fail.

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use `find` via Bash for broad file pattern matching
- Use `grep` via Bash for searching file contents with regex
- Use Read when you know the specific file path you need to read
- Use Bash ONLY for read-only operations (ls, git status, git log, git diff, find, grep, cat, head, tail)
- NEVER use Bash for: mkdir, touch, rm, cp, mv, git add, git commit, npm install, pip install, or any file creation/modification
- Adapt your search approach based on the thoroughness level specified by the caller
- Communicate your final report directly as a regular message - do NOT attempt to create files

NOTE: You are meant to be a fast agent that returns output as quickly as possible. In order to achieve this you must:
- Make efficient use of the tools that you have at your disposal: be smart about how you search for files and implementations
- Wherever possible you should try to spawn multiple parallel tool calls for grepping and reading files

Complete the user's search request efficiently and report your findings clearly.
EOF

# 3. Decision rules
cat > "$dir/skills/sonnet-delegation/SKILL.md" <<'EOF'
---
name: sonnet-delegation
description: Decide which model a piece of work runs on when the main session is Opus or Fable. Use before spawning a subagent or workflow agent, when choosing between delegating and doing work inline, or when a delegated result came back wrong.
---

Subagents default to **Sonnet** here (`CLAUDE_CODE_SUBAGENT_MODEL=sonnet`, and `Explore` is overridden to Sonnet). Omitting `model` on an Agent call means Sonnet. Every call is therefore one of four choices: **inline**, **default**, **escalate**, **downshift**.

## Inline: do it yourself

Delegation pays for a cold start, a brief, and a merge. Stay inline when any hold:

- A quick, targeted change, or fewer than a handful of tool calls.
- The files and context are already in this conversation and the work is mostly output over them.
- One dependent chain, where each step needs the previous step's reasoning.
- Routine work that needs the conversation history. A fork runs on the main model and saves nothing.

## Default: Sonnet, no `model`

Delegate when all hold:

1. A short brief specifies it completely: goal, files, constraints, output format.
2. It depends on nothing from this conversation beyond the brief.
3. Success is checkable: tests pass, a command's output, a list of locations, a diff matching a described pattern.
4. It is large enough to pay for the cold start: several files to read or edit, verbose output to digest.

Typical: codebase searches, test and build runs, log digestion, documentation lookups, mechanical multi-file edits, boilerplate, the routine pieces of a fan-out.

## Escalate: `model: opus` or `model: fable`

Pass the model explicitly when the delegated work is:

- Architecture, design, or trade-off analysis.
- Root-cause debugging of an ambiguous failure.
- Security judgement or review.
- Something Sonnet already attempted with an adequate brief and got wrong. Escalate or take it inline; the same brief on Sonnet again fails the same way.

`opus` is the first step up. `fable` is for the hardest, most ambiguous work and costs 5x Sonnet.

## Downshift: `model: haiku`

Trivial work with no judgement: a single lookup, a file listing, a short summary of text handed over in the brief.

## Every delegation

- **Brief**: the subagent starts blank. State the goal, the paths, the constraints, and what done looks like.
- **Compact result**: ask for findings, file paths, and failures only.
- **Verify** the result yourself before building on it.
- The main session's model stays as it is; switching it mid-task re-reads the whole history uncached.
EOF

jq .env "$dir/settings.json"
