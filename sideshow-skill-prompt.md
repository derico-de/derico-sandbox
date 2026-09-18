Update my host-level sideshow skill so agents delegate sideshow page authoring
to a sub-agent instead of doing it in their main context.

## Where

The skill file is `~/.agents/skills/sideshow/SKILL.md` (32 lines, a bootstrap
that tells the agent to fetch `sideshow agent-howto`). Confirm that path first;
if it is not there, check `~/.claude/skills/sideshow/SKILL.md` and
`$CLAUDE_CONFIG_DIR/skills/sideshow/SKILL.md`. Edit the one that exists. If more
than one exists, edit all of them identically. Do not touch the sideshow npm
package or its bundled `skills/sideshow/SKILL.md`.

## What

Append the section below verbatim at the end of the file, after the paragraph
that starts "Inside a sandboxsh VM". Keep the frontmatter and everything above
it unchanged. Do not reflow or rewrite existing lines.

```markdown

## Keep the page out of the main context

Publishing costs 10–20k tokens per post (howto, design guide, the HTML you
write) and each `update` adds more. The main agent only needs the session id,
the post id, and the user's comments back, so delegate the authoring:

- Use a **fork** (Claude Code: `subagent_type: "fork"`) when the page depends
  on the conversation, such as a review of the current diff or a design you
  just discussed. It inherits the context; its tool output stays out of yours.
- Use a **fresh sub-agent with a precise brief** when the content is readable
  from disk: a module diagram, a chart from a data file. State the title,
  the session title, which surfaces to use, and what the page must show.
- Do it inline for a single small Mermaid or markdown card; delegation costs
  more than it saves there.

The sub-agent fetches the howto and the guide itself, publishes, and reports
back only the session id, the post id, and any `userFeedback` it received.

Iterate by continuing the same sub-agent (Claude Code: SendMessage) so it
still knows the HTML it wrote and can `sideshow update <id>` instead of
publishing a near-duplicate. The main agent keeps the checkpoint drain
(`sideshow wait --session <id> --timeout 1`) at turn boundaries, because the
sub-agent is gone by the time the user comments.
```

## Check

Print the final file. It must still start with the `---` frontmatter block,
the new section must be the last thing in it, and there must be exactly one
blank line between the old last paragraph and the new heading.

Do not commit anything and do not restart the sideshow server; this file is
read by the agents, not by the server.
