# Routing work from an Opus/Fable main session to Sonnet subagents

Research date: 2026-09-29. Local install inspected: Claude Code 2.1.281 (`claude --version`), user settings in `/agent-creds/claude/settings.json`.

Conventions in this note:

- **Verified** = read in the cited primary source on the research date.
- **Inferred** = my conclusion from verified facts; marked explicitly.
- **Local** = observed in this installation, not in documentation.

No `docs/research/` convention existed in this repo (`docs/` held only `architecture.md` and two `plan-*.md` files), so this file creates it.

## Summary

1. The latest Sonnet is **Sonnet 5.5**, API ID `claude-sonnet-5-5`, released with Claude Code 2.1.284 on 2026-09-28 ([changelog](https://code.claude.com/docs/en/changelog), [models overview](https://platform.claude.com/docs/en/about-claude/models/overview)). The user's name for it was right; the ID `claude-sonnet-5` known to the session is the previous model, Sonnet 5.
2. **Discrepancy:** the `sonnet` alias resolves to Sonnet 5.5 only from v2.1.284 on the Anthropic API; from v2.1.197 it resolved to Sonnet 5 ([model-config, version history](https://code.claude.com/docs/en/model-config#version-history)). The local install is 2.1.281 (Local), so `sonnet` here still means Sonnet 5 until `claude update` is run (Inferred from the two facts).
3. Subagent model precedence is: per-call `model` parameter, then agent frontmatter `model`, then `CLAUDE_CODE_SUBAGENT_MODEL`, then the main model ([sub-agents](https://code.claude.com/docs/en/sub-agents#choose-a-model)).
4. There is **no settings.json key** for a default subagent model; the mechanism is the environment variable, which can be set in the `env` block of a settings file ([sub-agents](https://code.claude.com/docs/en/sub-agents#run-every-subagent-on-one-model)).
5. A skill's `model` field switches the **main** model for one turn; it does not route subagents, and it costs a full cache miss ([skills](https://code.claude.com/docs/en/skills#frontmatter-reference), [prompt-caching](https://code.claude.com/docs/en/prompt-caching#switching-models)). It is the wrong tool for this goal.
6. Hooks can rewrite Agent tool input (`updatedInput`), but only `SessionStart` receives the current `model`; `PreToolUse` does not ([hooks](https://code.claude.com/docs/en/hooks#common-input-fields)). `PostModelSwitch` fires on later changes and its documented example is exactly "on Opus, delegate implementation work to subagents" ([hooks](https://code.claude.com/docs/en/hooks#postmodelswitch)).
7. `opusplan` is the only built-in plan/execute mixed mode. No Fable equivalent is documented.
8. Anthropic's own measurements say delegation to cheaper workers saved money in only two situations: routine work with a long cost tail, and work larger than one context window. Otherwise the main model at lower effort was cheaper ([optimizing for cost and intelligence](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence#orchestrator-strategy-delegate-bulk-work)).
9. Cache reads cost almost the same across tiers ($0.25 Fable 5.1, $0.20 Opus 5.5, $0.20 Sonnet 5.5 per MTok), so the saving from Sonnet comes from output, uncached input, and cache writes ([pricing](https://platform.claude.com/docs/en/about-claude/pricing)).

## 1. Subagent model selection

### Agent tool `model` parameter

- The Agent tool input has four documented fields: `prompt`, `description`, `subagent_type`, `model`. `model` is described as "Optional model alias to override the default" ([hooks, PreToolUse input, Agent](https://code.claude.com/docs/en/hooks#pretooluse-input)).
- Local: the Agent tool schema in this session restricts `model` to the enum `sonnet`, `opus`, `haiku`, `fable`. Full model IDs are not accepted per call. This matches the documented wording "model alias" but the enum itself is not listed in the docs.
- The per-call value persists when the subagent is resumed or sent a follow-up message (since v2.1.211) ([sub-agents](https://code.claude.com/docs/en/sub-agents#choose-a-model)).
- Forks (`subagent_type: fork`) always run on the main session's model ([sub-agents](https://code.claude.com/docs/en/sub-agents#how-forks-differ-from-other-subagents)).

### Frontmatter `model` in `.claude/agents/*.md`

- Accepted values: `sonnet`, `opus`, `haiku`, `fable`, a full model ID such as `claude-opus-5-5`, or `inherit`. It accepts the same values as `--model`. If omitted, the precedence order below applies ([sub-agents](https://code.claude.com/docs/en/sub-agents#choose-a-model)).
- A user or project subagent named `Explore` overrides the built-in one and keeps its own `model` field ([sub-agents](https://code.claude.com/docs/en/sub-agents#built-in-subagents)).

### Precedence

Verified order ([sub-agents](https://code.claude.com/docs/en/sub-agents#choose-a-model)):

1. Per-invocation `model` parameter
2. Frontmatter `model` (`inherit` selects the main model)
3. `CLAUDE_CODE_SUBAGENT_MODEL`
4. Main conversation's model

Caveats, all from the same page:

- A family alias that names the **main model's own family** resolves to the main session's exact model, including a `[1m]` suffix. Passing `model: fable` from a Fable session yields the same Fable model.
- Before v2.1.251 the environment variable came first and overrode everything.
- Values blocked by an `availableModels` allowlist are substituted, with a warning in interactive sessions.
- `/tasks` shows which model each subagent runs on (v2.1.242+).

### Built-in subagents

| Agent | Model | Source |
| :- | :- | :- |
| Explore | Inherits main model, capped at Opus on the Claude API (since v2.1.198; previously always Haiku) | [sub-agents](https://code.claude.com/docs/en/sub-agents#built-in-subagents) |
| Plan | Inherits main model | same |
| general-purpose | `CLAUDE_CODE_SUBAGENT_MODEL` if set, else main model | same |
| statusline-setup | Sonnet | same |
| claude-code-guide | Haiku | same |

Consequence (Inferred): in a Fable session, every Explore call runs on Opus and every general-purpose call on Fable unless something overrides it. This is where most avoidable spend sits.

## 2. Settings and environment variables

| Name | Effect | Source |
| :- | :- | :- |
| `CLAUDE_CODE_SUBAGENT_MODEL` | Default model for subagents, agent-team teammates, and workflow agents not assigned a model another way. Alias or full ID. `inherit` equals unset. An alias here always resolves to the alias target, even if it names the main model's family. Does **not** change Explore or Plan by itself. | [model-config](https://code.claude.com/docs/en/model-config#environment-variables), [sub-agents](https://code.claude.com/docs/en/sub-agents#choose-a-model) |
| `CLAUDE_CODE_SUBAGENT_MODEL_FORCE=1` | Forces one model onto every subagent, ignores frontmatter `model`, and removes Claude's ability to pass a model per call. With only FORCE set, subagents run on the main model. Requires v2.1.257+. Forks and `context: fork` skills with `model: inherit` stay on the main model. | [sub-agents](https://code.claude.com/docs/en/sub-agents#run-every-subagent-on-one-model) |
| `ANTHROPIC_DEFAULT_SONNET_MODEL` | Model that the `sonnet` alias (and the execution phase of `opusplan`) maps to. Must be a full model name. | [model-config](https://code.claude.com/docs/en/model-config#environment-variables) |
| `ANTHROPIC_DEFAULT_OPUS_MODEL`, `_FABLE_MODEL`, `_HAIKU_MODEL` | Same for the other aliases | same |
| `ANTHROPIC_DEFAULT_MODEL` | Start model for new sessions when nothing else selects one. Ignored when set to `default`, `inherit`, `opusplan`, or `haiku`. | [model-config](https://code.claude.com/docs/en/model-config#set-a-default-model-for-new-sessions) |
| `subagentPromptCacheTtl` (settings key) | Cache lifetime for subagents and other non-main requests | [settings-reference](https://code.claude.com/docs/en/settings-reference#subagentpromptcachettl) |
| `Agent(model:opus)` (permission rule) | Deny/ask rule matching Agent calls by literal `model` value. Does not match calls that omit `model`. | [permissions](https://code.claude.com/docs/en/permissions#match-by-input-parameter) |

- No settings.json key named for a default subagent model was found in the settings reference; a search for "subagent" returned only `subagentPromptCacheTtl`, `subagentStatusLine`, and `agent` ([settings-reference](https://code.claude.com/docs/en/settings-reference)). The documented way to persist the variable is the `env` block of a settings file ([sub-agents](https://code.claude.com/docs/en/sub-agents#run-every-subagent-on-one-model)).
- Local: `/agent-creds/claude/settings.json` sets `"model": "claude-fable-5-1[1m]"`, has no `env` block, and no `hooks` block. No `~/.claude/agents/` directory exists.

## 3. Skill frontmatter

### Fields

Verified list ([skills, frontmatter reference](https://code.claude.com/docs/en/skills#frontmatter-reference)): `name`, `description`, `when_to_use`, `argument-hint`, `arguments`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `disallowed-tools`, `model`, `effort`, `context`, `agent`, `background`, `hooks`, `paths`, `shell`, `metadata`, `license`, `compatibility`. Unrecognized fields are ignored silently.

### What `model` on a skill does

- It sets the model "when this skill is active", for the rest of the current turn; the session model resumes on the next prompt. Accepts `/model` values or `inherit` ([skills](https://code.claude.com/docs/en/skills#frontmatter-reference)).
- With `context: fork` it sets the forked subagent's model instead (same source).
- A skill naming a different model makes that turn a model switch: the whole conversation is re-read with no cache hits ([prompt-caching](https://code.claude.com/docs/en/prompt-caching#switching-models)).

Inferred: `model: sonnet` on a guidance skill would downgrade the main conversation for a turn and pay a cache miss on a large context. It must not be set on the delegation skill.

### `context: fork`

- Runs the skill body as the prompt of a fresh subagent of type `agent`. It does **not** see conversation history, despite the name ([skills](https://code.claude.com/docs/en/skills#run-skills-in-a-subagent)).
- Only sensible for skills with an explicit task; a guideline-only skill returns nothing useful (same source).

### Making a skill apply consistently

| Mechanism | Loads | Enforced? | Source |
| :- | :- | :- | :- |
| Skill description | Description in every request (truncated at 1,536 chars, may be dropped when the listing budget overflows); body only when invoked | No, the model decides | [skills](https://code.claude.com/docs/en/skills#skill-descriptions-are-cut-short), [features-overview](https://code.claude.com/docs/en/features-overview#context-cost-by-feature) |
| CLAUDE.md | Full content, every session, every request; also loaded into non-Explore/Plan subagents | No: "context, not enforced configuration" | [memory](https://code.claude.com/docs/en/memory), [sub-agents](https://code.claude.com/docs/en/sub-agents#what-loads-at-startup) |
| Hooks | Run externally; zero context unless they return text | Yes, deterministic | [features-overview](https://code.claude.com/docs/en/features-overview#context-cost-by-feature) |

- Once invoked, a skill body stays in context across turns and survives compaction within a budget (first 5,000 tokens per skill, 25,000 combined) ([skills](https://code.claude.com/docs/en/skills#skill-content-lifecycle)).
- The docs recommend hooks when a skill stops influencing behaviour and determinism is needed (same source).
- Hooks declared in skill frontmatter register only when the skill is invoked, then stay for the session ([hooks](https://code.claude.com/docs/en/hooks#hooks-in-skills-and-agents)). They cannot bootstrap the skill itself (Inferred).

## 4. Mixed-model modes

### `opusplan`

- Uses `opus` in plan mode and `sonnet` for execution ([model-config](https://code.claude.com/docs/en/model-config#opusplan-model-setting)).
- Each plan-mode toggle is a model switch and starts a fresh cache ([prompt-caching](https://code.claude.com/docs/en/prompt-caching#switching-models)).
- The switch boundary is plan mode only; it is not task-aware (same model-config section).

### Fable equivalent

- The alias table lists `opusplan` as the only plan/execute hybrid; no `fableplan` appears ([model-config](https://code.claude.com/docs/en/model-config#model-aliases)). Not finding it is not proof of absence, but nothing documented exists.

### Advisor tool (the inverse pattern)

- Pairs a cheaper main model with a stronger advisor consulted at decision points. Experimental, Anthropic API only ([advisor](https://code.claude.com/docs/en/advisor)).
- The advisor must be at least as capable as the main model. A Fable main model accepts only a Fable advisor, so this does not help an Opus/Fable main session save money (same source).
- The docs list "Sonnet main + Fable advisor" as a way to get Fable guidance without running Fable throughout ([advisor](https://code.claude.com/docs/en/advisor#common-model-pairings)). This is an alternative to the whole skill idea and is noted under open questions.

## 5. Hooks for model routing

### Can a hook modify tool input?

Yes. `PreToolUse` can return `updatedInput` in `hookSpecificOutput`. It replaces the **entire** input object, so unchanged fields must be echoed back ([hooks](https://code.claude.com/docs/en/hooks#pretooluse-decision-control)). `PreToolUse` matches the `Agent` tool by name ([hooks](https://code.claude.com/docs/en/hooks#pretooluse)).

### Does hook input expose the main model?

| Event | Model information | Source |
| :- | :- | :- |
| `SessionStart` | Optional `model` field. May be omitted, for example after `/clear`. | [hooks](https://code.claude.com/docs/en/hooks#sessionstart-input) |
| `PreModelSwitch` / `PostModelSwitch` | `from_model`, `to_model`; matcher compares against the canonical target model name and accepts regex such as `.*opus.*` | [hooks](https://code.claude.com/docs/en/hooks#premodelswitch) |
| `PreToolUse`, `UserPromptSubmit`, all others | None. "Only SessionStart hooks can receive a model field". There is no `$CLAUDE_MODEL` variable. | [hooks](https://code.claude.com/docs/en/hooks#common-input-fields) |

Other relevant facts:

- `PostModelSwitch` also fires for automatic fallback, `opusplan` transitions, and model restore on resume; its stdout or `additionalContext` reaches Claude with the next request ([hooks](https://code.claude.com/docs/en/hooks#postmodelswitch)).
- `SessionStart` fires with `source` of `startup`, `resume`, `clear`, `compact`, or `fork`, and its `additionalContext` is placed before the first prompt ([hooks](https://code.claude.com/docs/en/hooks#sessionstart-input)).
- `UserPromptSubmit` can add context alongside each prompt ([hooks](https://code.claude.com/docs/en/hooks#userpromptsubmit-decision-control)).
- Hook input inside a subagent carries `agent_id` and `agent_type`, so a hook can tell main-thread Agent calls from nested ones ([hooks](https://code.claude.com/docs/en/hooks#common-input-fields)).
- Injected text should be phrased as factual statements; text framed as system commands can trigger prompt-injection defences ([hooks](https://code.claude.com/docs/en/hooks#add-context-for-claude)).
- `PostToolUse` on Agent exposes `resolvedModel`, useful for auditing what actually ran ([hooks](https://code.claude.com/docs/en/hooks#pretooluse-input)).

Inferred: a model-conditional `PreToolUse` rule needs state. `SessionStart` and `PostModelSwitch` hooks would write the current model to a file keyed by `session_id`, and the `PreToolUse` hook would read it. This is buildable from documented parts but is not a documented pattern.

## 6. Which work suits Sonnet

### Prices and speed (Anthropic API list prices)

| Model | Input | Output | 5m cache write | Cache read | Latency |
| :- | :- | :- | :- | :- | :- |
| Fable 5.1 | $10 | $50 | $12.50 | $0.25 | Slower |
| Opus 5.5 | $4 | $20 | $5 | $0.20 | Moderate |
| Sonnet 5.5 | $2 | $10 | $2.50 | $0.20 | Fast |
| Haiku 4.5 | $1 | $5 | $1.25 | $0.10 | Fastest |

Per million tokens. Sources: [pricing](https://platform.claude.com/docs/en/about-claude/pricing), [models overview](https://platform.claude.com/docs/en/about-claude/models/overview). All three top models have a 1M context window (same overview).

Computed from the table: Sonnet 5.5 is 5x cheaper than Fable 5.1 and 2x cheaper than Opus 5.5 on input, output, and cache writes. On cache reads it is 1.25x cheaper than Fable 5.1 and equal to Opus 5.5.

### Official guidance

- "Sonnet handles most coding tasks well and costs less than Opus. Reserve Opus for complex architectural decisions or multi-step reasoning." ([costs](https://code.claude.com/docs/en/costs#choose-the-right-model))
- Fable is suited to ambiguous problems: root-cause investigations, outage debugging, architecture decisions, and long tasks ([model-config](https://code.claude.com/docs/en/model-config#work-with-fable)).
- Selection matrix: Sonnet 5.5 for "code generation, data analysis, content creation, visual understanding, agentic tool use"; Opus 5.5 for large-scale refactoring and complex systems engineering; Fable 5.1 for multi-hour agent sessions and deep research ([choosing a model](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model)).
- "Pick a smaller model when the work is routine. For example, edits you can describe precisely, mechanical changes, or questions about code that's already in context." ([Anthropic blog, linked from model-config](https://claude.com/blog/claude-model-and-effort-level-in-claude-code))
- "On routine work at the same effort level, both models generally will get it right. The larger model consumes more tokens with extra verification steps at a higher per-token price." (same blog)
- If Claude had the context, tried, and still got it wrong, that signals a larger model (same blog).
- Subagents are recommended for verbose operations: running tests, fetching documentation, processing logs ([costs](https://code.claude.com/docs/en/costs#delegate-verbose-operations-to-subagents), [sub-agents](https://code.claude.com/docs/en/sub-agents#isolate-high-volume-operations)).

### Counter-evidence to keep in mind

- Cost per completed task can favour the bigger model: on a SWE-bench Pro subset, Fable 5.1 at `low` effort cost $0.54 per solved task against $0.84 for Sonnet 5 at default, with 11 points more solved ([optimizing](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence#compare-models-on-cost-per-task)).
- "Tuning effort is often a better lever than switching models" ([choosing a model](https://platform.claude.com/docs/en/about-claude/models/choosing-a-model)).
- These figures are for Sonnet 5; no equivalent measurement for Sonnet 5.5 was found in the guide.

### Candidate categories (Inferred from the sources above)

Good for Sonnet:

- Mechanical edits that can be specified precisely: renames, signature changes across files, import fixes, formatting
- Boilerplate and scaffolding from a clear template
- Test and build runs with a summary of failures
- Codebase search, file discovery, reading documentation and reporting facts
- Log and output processing
- Independent, parallel pieces of a fan-out where each piece is routine

Keep on the main model:

- Architecture and design decisions, trade-off analysis
- Root-cause debugging of ambiguous failures
- Anything where a previous Sonnet attempt had the context and still failed
- Work that depends on the conversation so far
- Security-sensitive review and final verification of delegated results
- One dependent chain of steps that fits in the current context

## 7. Costs of delegation

Verified:

- A non-fork subagent starts with a fresh context: own system prompt, the task message, CLAUDE.md hierarchy, git status, preloaded skills. It does not see conversation history or files already read ([sub-agents](https://code.claude.com/docs/en/sub-agents#what-loads-at-startup)).
- "A subagent that isn't a fork starts fresh and may need time to gather context"; the docs advise the main conversation for quick targeted changes, iterative work, and phases that share context ([sub-agents](https://code.claude.com/docs/en/sub-agents#choose-between-subagents-and-main-conversation)).
- Each model has its own cache. A subagent's first request does not read the parent's cache and warms its own. The parent's cache is unaffected by spawning ([prompt-caching](https://code.claude.com/docs/en/prompt-caching#subagents-and-the-cache)).
- Subagents get a five-minute cache TTL even on a subscription unless configured otherwise (same source).
- A fork shares the parent's cache and is cheaper than a fresh subagent for tasks needing the same context, but runs on the main model ([sub-agents](https://code.claude.com/docs/en/sub-agents#how-forks-differ-from-other-subagents)).
- Results return to the main context; many detailed results consume significant context, and every subagent spends tokens of its own ([sub-agents](https://code.claude.com/docs/en/sub-agents#run-parallel-research)).
- Switching the **main** model mid-session re-reads the whole history uncached ([prompt-caching](https://code.claude.com/docs/en/prompt-caching#switching-models)). Delegating to a subagent avoids this.
- Anthropic's measurements: an orchestrator "pays for a plan, a handoff, and a merge that a single model gets for free" when work is one dependent chain or fits in one context. It saved money only for routine work with a long cost tail (about half the cost on average) and for work larger than one context window (about half the cost, 10 to 12 points lower accuracy) ([optimizing](https://platform.claude.com/docs/en/about-claude/models/optimizing-for-cost-and-intelligence#orchestrator-strategy-delegate-bulk-work)).
- Those measurements ran on Claude Managed Agents with Sonnet 5 workers, not in Claude Code (same source, references 4 and 8).

Inferred cost model for one delegation:

- Overhead paid at main-model rates: writing the brief (output tokens) and reading the returned summary.
- Overhead paid at Sonnet rates: cold start (system prompt, CLAUDE.md, tools as cache writes) plus re-reading any files the main session already had in context.
- Saving: the work's output tokens and fresh input at 1/5 (Fable) or 1/2 (Opus) of the price.
- Because cache reads cost nearly the same on all tiers, work that is mostly re-reading an already cached context saves little. Work that generates much output or reads much new material saves the most.
- From an Opus 5.5 main session the margin is only 2x, so the break-even task size is larger than from Fable.

## Design recommendation for the skill

### Mechanisms compared

| Option | Strength | Weakness |
| :- | :- | :- |
| Skill alone | On-demand, low context cost; holds detailed rules | Triggering depends on the description matching; delegation decisions occur continuously, not on a keyword. Cannot condition on the main model. Not enforced. |
| Skill + CLAUDE.md rule | Always loaded; simple | Loaded in every session regardless of model and copied into most subagents. Advisory only. |
| Skill + hooks | `SessionStart` and `PostModelSwitch` can inject the rule only when the model matches Opus or Fable; deterministic delivery; zero cost otherwise | Needs a small script. `model` may be missing from `SessionStart`. Delivery is guaranteed, compliance is not. |
| `PreToolUse` rewrite of `model` | Hard enforcement | No main model in input, needs a state file. Overrides deliberate choices. Blind to task difficulty. |
| `CLAUDE_CODE_SUBAGENT_MODEL=sonnet` | Deterministic, no context cost, one line | Applies whatever the main model is. Does not touch Explore/Plan. Makes Sonnet the default even for hard delegated work unless the main model escalates explicitly. |

### Recommended combination

1. **Update Claude Code to 2.1.284 or later** so `sonnet` means Sonnet 5.5.
2. **Skill** (`sonnet-delegation`) holding the decision rules below. No `model`, no `context: fork`. Description written so it also triggers on its own.
3. **Two hooks in user settings** that inject a short factual pointer when the model matches `opus` or `fable`:
   - `SessionStart`: check the `model` field; covers startup, resume, clear, compact.
   - `PostModelSwitch` with matcher `.*(opus|fable).*`: covers mid-session switches.
   The injected text names the skill and states the two or three core rules, so the behaviour holds even if the skill body is never loaded.
4. **A custom subagent** `sonnet-worker` in `~/.claude/agents/` with `model: sonnet`, plus a user-level `Explore` override with `model: sonnet`. This fixes the largest default cost (Explore on Opus, general-purpose on Fable) without an environment variable.
5. **Not recommended initially:** `PreToolUse` rewriting and `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`. Both remove the main model's ability to keep hard subtasks on a stronger model. Consider plain `CLAUDE_CODE_SUBAGENT_MODEL=sonnet` later if `/tasks` shows the main model still spawning same-tier subagents for routine work.

A real test for the hook script belongs with it (input JSON fixtures for each model and a missing `model` field).

### Draft decision rules for the skill

Applies when the main model is Opus or Fable.

Delegate to Sonnet (`model: sonnet` or `sonnet-worker`) when all hold:

1. The task can be specified completely in a short brief: goal, files, constraints, expected output format.
2. It does not depend on reasoning or decisions from this conversation beyond what the brief states.
3. Success is checkable: tests pass, a command's output, a list of locations, a diff matching a described pattern.
4. It is large enough to pay for a cold start: several files to read or edit, verbose output to digest, or more than a few tool calls.

Typical cases: codebase searches, test and build runs, log digestion, documentation lookups, mechanical multi-file edits, boilerplate, independent pieces of a fan-out.

Keep on the main model when any hold:

1. The task is a quick, targeted change or needs fewer than a handful of tool calls.
2. The needed files and context are already in this conversation and the work is mostly output over that context.
3. It involves architecture, ambiguous debugging, security judgement, or trade-offs.
4. It is one dependent chain where each step needs the previous step's reasoning.
5. Sonnet already attempted it with adequate context and failed.

Other rules:

- If the task needs the conversation history but is routine, prefer doing it inline over a fork; a fork runs on the main model and saves no model cost.
- Ask for a compact result: findings, file paths, and failures only.
- Verify delegated results on the main model before building on them.
- Escalate by re-running on the main model, not by retrying Sonnet with the same brief.
- Do not change the main session's model to save cost mid-task.
- Prefer lowering effort over delegating when the work is a single chain that fits in context.

## Open questions / unverified

- **Agent tool `model` enum.** The values `sonnet`, `opus`, `haiku`, `fable` come from the local tool schema. The docs say only "model alias". Whether any version accepts a full ID per call is unverified.
- **Sonnet 5.5 on 2.1.281.** The docs state the version requirement but not what happens when `claude-sonnet-5-5` is set in frontmatter or `ANTHROPIC_DEFAULT_SONNET_MODEL` on an older client. Not tested, since no configuration was changed.
- **No settings key for the subagent default.** Based on searching the settings reference, not on an explicit statement in the docs.
- **No Fable plan/execute mode.** Absence in the alias table only.
- **Does the model know its own tier?** The rules assume the main model can tell it is Opus or Fable. In this session the model ID is in the system prompt (Local); this is not documented as guaranteed. The hooks remove the dependency.
- **`SessionStart` `model` omission.** Documented as possible, with `/clear` and conversation recovery as examples. The full list of cases is not documented.
- **Billing on the user's plan.** Whether Fable bills to usage credits and how subagent usage counts toward plan limits depends on the plan, which was not inspected ([model-config](https://code.claude.com/docs/en/model-config#fable-and-usage-credits)).
- **Sonnet 5.5 quality figures.** All measured delegation and cost-per-task numbers found are for Sonnet 5. None for Sonnet 5.5.
- **Transfer of Anthropic's orchestrator measurements to Claude Code.** They were run on Claude Managed Agents; applicability to Claude Code subagents is assumed, not measured.
- **Blog post as source.** The model-selection quotes come from claude.com/blog, a first-party Anthropic post linked from the official docs, not from the docs themselves.
- **Alternative not evaluated in depth:** running Sonnet 5.5 as the main model with a Fable advisor, which the docs present as the cheaper way to get Fable-level guidance ([advisor](https://code.claude.com/docs/en/advisor#common-model-pairings)).
