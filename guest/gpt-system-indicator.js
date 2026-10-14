import { readFileSync } from "node:fs";

// Codex-derived guidance is bundled locally: no network access or skill invocation.
const PROFILE = readFileSync(new URL("./gpt-system-prompt.md", import.meta.url), "utf8").trim();
const MARKER = "<!-- pi-gpt-codex-operating-profile -->";

function isGpt(model) {
    const id = model?.id ?? "";
    const name = model?.name ?? "";
    return /(?:^|[/:])(?:gpt(?=[-\d])|chatgpt(?=[-\d])|codex(?:[-\d]|$))/i.test(id)
        || /^(?:gpt(?=[\s-]|\d)|chatgpt(?=[\s-]|$)|codex(?:[\s-]|$))/i.test(name);
}

export default function (pi) {
    const update = (ctx, model = ctx.model) => {
        if (!ctx.hasUI) return;
        ctx.ui.setStatus("gpt-system", isGpt(model) ? "GPT-SYSTEM: Codex profile" : undefined);
    };

    pi.on("session_start", (_event, ctx) => update(ctx));
    pi.on("session_switch", (_event, ctx) => update(ctx));
    pi.on("model_select", (event, ctx) => update(ctx, event.model));
    pi.on("before_agent_start", (event, ctx) => {
        update(ctx);
        if (!isGpt(ctx.model) || event.systemPrompt.includes(MARKER)) return;
        return { systemPrompt: `${event.systemPrompt}\n\n${MARKER}\n${PROFILE}` };
    });
}
