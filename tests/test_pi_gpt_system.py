import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
GUEST = ROOT / "guest"
MARKER = "<!-- pi-gpt-codex-operating-profile -->"


def run_node(extension: Path, assertions: str) -> None:
    if shutil.which("node") is None:
        pytest.skip("Node.js is required to load the Pi extension")
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=(
            'import assert from "node:assert/strict";\n'
            f"import register from {json.dumps(extension.as_uri())};\n"
            "const handlers = new Map();\n"
            "register({ on: (event, handler) => handlers.set(event, handler) });\n"
            "const statuses = new Map([['personal', 'keep me']]);\n"
            "const ctx = { hasUI: true, model: undefined,\n"
            "  ui: { setStatus: (key, value) => statuses.set(key, value) } };\n"
            "const event = { systemPrompt: 'Pi tools, project rules, and user instructions' };\n"
            f"{assertions}\n"
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "", "Extensions must not pollute JSON/RPC/print output"


@pytest.mark.parametrize(
    "model",
    [
        {"id": "gpt-4o", "provider": "openai"},
        {"id": "gpt-5"},
        {"id": "gpt-5.2-codex"},
        {"id": "gpt-5.3-codex-spark"},
        {"id": "gpt-5.5"},
        {"id": "gpt-5.6-sol"},
        {"id": "gpt-6.1-sol"},
        {"id": "openai/gpt-5.6", "provider": "openrouter"},
        {"id": "azure:gpt-5.2"},
        {"id": "chatgpt-4o-latest"},
        {"id": "codex-mini-latest"},
        {"id": "deployment", "name": "GPT-5.6"},
        {"id": "gpt5p6"},
        {"id": "gpt-daybreak-blue-latest"},
    ],
)
def test_gpt_models_receive_profile_without_losing_base_prompt(model: dict) -> None:
    run_node(
        GUEST / "gpt-system-indicator.js",
        f"ctx.model = {json.dumps(model)};\n"
        "const result = handlers.get('before_agent_start')(event, ctx);\n"
        "assert.ok(result.systemPrompt.startsWith(event.systemPrompt + '\\n\\n'));\n"
        f"assert.ok(result.systemPrompt.includes({json.dumps(MARKER)}));\n"
        "assert.ok(result.systemPrompt.includes('## Autonomy, scope, and persistence'));\n"
        "assert.ok(result.systemPrompt.includes('## Destructive actions and authority'));\n"
        "assert.ok(result.systemPrompt.includes('## Validation and handoff'));\n"
        "assert.equal(statuses.get('gpt-system'), 'GPT-SYSTEM: Codex profile');\n"
        "assert.equal(statuses.get('personal'), 'keep me');\n"
        "assert.equal(handlers.get('before_agent_start')(result, ctx), undefined);",
    )


@pytest.mark.parametrize(
    "model",
    [
        None,
        {"id": "claude-opus-4-6", "provider": "anthropic"},
        {"id": "mistral-large", "name": "Mistral Large"},
        {"id": "gemini-3-pro", "provider": "google"},
        {"id": "not-gpt-5.6", "name": "Claude via GPT proxy"},
        {"id": "o3", "provider": "openai"},
    ],
)
def test_other_models_are_unchanged(model: dict | None) -> None:
    run_node(
        GUEST / "gpt-system-indicator.js",
        f"ctx.model = {json.dumps(model)};\n"
        "assert.equal(handlers.get('before_agent_start')(event, ctx), undefined);\n"
        "assert.equal(statuses.get('gpt-system'), undefined);",
    )


def test_model_switches_refresh_status_and_gate_each_turn() -> None:
    run_node(
        GUEST / "gpt-system-indicator.js",
        """
ctx.model = { id: 'gpt-5.6' };
handlers.get('session_start')({}, ctx);
assert.equal(statuses.get('gpt-system'), 'GPT-SYSTEM: Codex profile');
assert.ok(handlers.get('before_agent_start')(event, ctx));
handlers.get('model_select')({ model: { id: 'claude-opus-4-6' } }, ctx);
assert.equal(statuses.get('gpt-system'), undefined);
ctx.model = { id: 'claude-opus-4-6' };
assert.equal(handlers.get('before_agent_start')(event, ctx), undefined);
ctx.model = { id: 'gpt-5.2-codex' };
handlers.get('session_switch')({}, ctx);
assert.equal(statuses.get('gpt-system'), 'GPT-SYSTEM: Codex profile');
assert.ok(handlers.get('before_agent_start')(event, ctx));
""",
    )


def test_print_mode_injects_without_ui_or_stdout() -> None:
    run_node(
        GUEST / "gpt-system-indicator.js",
        """
ctx.hasUI = false;
ctx.model = { id: 'gpt-5.6' };
ctx.ui = { setStatus() { throw new Error('unexpected UI call'); } };
handlers.get('session_start')({}, ctx);
handlers.get('model_select')({ model: ctx.model }, ctx);
assert.ok(handlers.get('before_agent_start')(event, ctx).systemPrompt);
""",
    )


@pytest.mark.parametrize("shared", [False, True])
def test_guest_boot_refreshes_profile_in_both_state_modes(tmp_path: Path, shared: bool) -> None:
    source = (GUEST / "agent-init.sh").read_text()
    start = source.index("configure_pi_gpt_system() {")
    end = source.index("\n}\n", start) + len("\n}\n")
    script = tmp_path / "configure.sh"
    script.write_text(
        "set -eu\n"
        "install() {\n"
        '  if [ "$1" = "-d" ]; then mkdir -p "${@: -1}"; return; fi\n'
        '  while [ $# -gt 2 ]; do shift; done; cp "$1" "$2"\n'
        "}\n"
        f"{source[start:end]}"
        'configure_pi_gpt_system "$1" "$2"\n'
    )
    home = tmp_path / "home"
    home.mkdir()
    agent_state = home / ".pi"
    if shared:
        volume = tmp_path / "agent-creds" / "pi"
        volume.mkdir(parents=True)
        agent_state.symlink_to(volume)
    extensions = agent_state / "agent" / "extensions"
    extensions.mkdir(parents=True)
    (extensions / "personal.ts").write_text("// keep me\n")
    (extensions / "gpt-system-indicator.ts").write_text("// old 5.6 hook\n")
    (extensions / "gpt-system-prompt.md").write_text("old prompt\n")
    for _ in range(2):
        subprocess.run(
            ["bash", str(script), str(extensions), str(GUEST)],
            check=True,
            capture_output=True,
        )
    assert (extensions / "personal.ts").read_text() == "// keep me\n"
    assert (extensions / "gpt-system-indicator.ts").read_bytes() == (
        GUEST / "gpt-system-indicator.js"
    ).read_bytes()
    assert (extensions / "gpt-system-prompt.md").read_bytes() == (
        GUEST / "gpt-system-prompt.md"
    ).read_bytes()
    assert 'configure_pi_gpt_system "$HOME_DEV/.pi/agent/extensions" /opt/sandboxsh/pi' in source
    # The boot-installed TypeScript entry is identical JavaScript; load it as ESM.
    shutil.copyfile(extensions / "gpt-system-indicator.ts", extensions / "test-hook.mjs")
    run_node(
        extensions / "test-hook.mjs",
        "ctx.model = { id: 'gpt-5.6' };\n"
        "assert.ok(handlers.get('before_agent_start')(event, ctx).systemPrompt);",
    )


@pytest.mark.parametrize("custom_dir", [False, True])
def test_host_install_preserves_dotfiles_symlinks(tmp_path: Path, custom_dir: bool) -> None:
    home = tmp_path / "home"
    agent_dir = tmp_path / "custom agent" if custom_dir else home / ".pi" / "agent"
    extensions = agent_dir / "extensions"
    extensions.mkdir(parents=True)
    dotfile = tmp_path / "dotfile.ts"
    dotfile.write_text("// old 5.6 hook\n")
    hook = extensions / "gpt-system-indicator.ts"
    hook.symlink_to(dotfile)
    (extensions / "personal.ts").write_text("// keep me\n")
    env = {**os.environ, "HOME": str(home)}
    env.pop("PI_CODING_AGENT_DIR", None)
    if custom_dir:
        env["PI_CODING_AGENT_DIR"] = str(agent_dir)
    for _ in range(2):
        subprocess.run(
            ["bash", str(ROOT / "install-pi-gpt-system.sh")],
            env=env,
            check=True,
            capture_output=True,
        )
    assert hook.is_symlink()
    assert hook.readlink() == dotfile
    assert dotfile.read_bytes() == (GUEST / "gpt-system-indicator.js").read_bytes()
    assert (extensions / "gpt-system-prompt.md").read_bytes() == (
        GUEST / "gpt-system-prompt.md"
    ).read_bytes()
    assert (extensions / "personal.ts").read_text() == "// keep me\n"
    shutil.copyfile(dotfile, dotfile.with_suffix(".mjs"))
    run_node(
        dotfile.with_suffix(".mjs"),
        "ctx.model = { id: 'gpt-5.6' };\n"
        "assert.ok(handlers.get('before_agent_start')(event, ctx).systemPrompt);",
    )


def test_host_hook_loads_through_pi_extension_loader(tmp_path: Path) -> None:
    loader = (
        Path.home()
        / ".local/lib/node_modules/@earendil-works/pi-coding-agent/dist/core/extensions/loader.js"
    )
    if not loader.is_file() or shutil.which("node") is None:
        pytest.skip("Installed Pi and Node.js are required for the loader integration test")
    dotfiles = tmp_path / "dotfiles"
    dotfiles.mkdir()
    shutil.copyfile(GUEST / "gpt-system-indicator.js", dotfiles / "gpt-system-indicator.ts")
    shutil.copyfile(GUEST / "gpt-system-prompt.md", dotfiles / "gpt-system-prompt.md")
    agent_dir = tmp_path / "agent"
    hook = agent_dir / "extensions" / "gpt-system-indicator.ts"
    hook.parent.mkdir(parents=True)
    hook.symlink_to(dotfiles / "gpt-system-indicator.ts")
    subprocess.run(
        ["bash", str(ROOT / "install-pi-gpt-system.sh")],
        env={**os.environ, "PI_CODING_AGENT_DIR": str(agent_dir)},
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["node", "--input-type=module"],
        input=(
            'import assert from "node:assert/strict";\n'
            f"import {{ loadExtensions }} from {json.dumps(loader.as_uri())};\n"
            f"const loaded = await loadExtensions([{json.dumps(str(hook))}], "
            f"{json.dumps(str(tmp_path))});\n"
            "assert.deepEqual(loaded.errors, []);\n"
            "assert.equal(loaded.extensions.length, 1);\n"
            "const handler = loaded.extensions[0].handlers.get('before_agent_start')[0];\n"
            "const result = await handler({ systemPrompt: 'BASE' }, "
            "{ hasUI: false, model: { id: 'gpt-5.6' } });\n"
            "assert.ok(result.systemPrompt.startsWith('BASE\\n\\n'));\n"
            f"assert.ok(result.systemPrompt.includes({json.dumps(MARKER)}));\n"
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_finalize_bundles_both_profile_files() -> None:
    from sandboxsh.imagebuild import HELPER_FILES

    for name in ("gpt-system-indicator.js", "gpt-system-prompt.md"):
        assert name in HELPER_FILES
    finalize = (GUEST / "stages" / "90-finalize.sh").read_text()
    assert "install -d -m 0755 /opt/sandboxsh/pi" in finalize
    assert (
        "install -m 0644 /root/gpt-system-indicator.js "
        "/root/gpt-system-prompt.md /opt/sandboxsh/pi/"
    ) in finalize
