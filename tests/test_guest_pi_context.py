import json
import shutil
import subprocess
from pathlib import Path

import pytest

AGENT_INIT = Path(__file__).parents[1] / "guest" / "agent-init.sh"


@pytest.fixture
def extension(tmp_path: Path) -> Path:
    source = AGENT_INIT.read_text()
    start = source.index("configure_pi_context_status() {")
    end = source.index('\nconfigure_pi_context_status "$HOME_DEV', start)
    script = tmp_path / "configure.sh"
    script.write_text(
        "set -eu\n"
        "install() {\n"
        '  if [ "$1" = "-d" ]; then mkdir -p "${@: -1}"; return; fi\n'
        '  while [ $# -gt 2 ]; do shift; done; cp "$1" "$2"\n'
        "}\n"
        f"{source[start:end]}\n"
        'configure_pi_context_status "$1"\n'
    )
    directory = tmp_path / "agent" / "extensions"
    directory.mkdir(parents=True)
    unrelated = directory / "personal.ts"
    unrelated.write_text("// personal extension\n")
    for _ in range(2):
        subprocess.run(["bash", str(script), str(directory)], check=True, capture_output=True)
    assert unrelated.read_text() == "// personal extension\n"
    assert 'configure_pi_context_status "$HOME_DEV/.pi/agent/extensions"' in source
    return directory / "sandboxsh-context.js"


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
            "const ctx = { hasUI: true, getContextUsage: () => usage,\n"
            "  ui: { setStatus: (key, value) => statuses.set(key, value) } };\n"
            "let usage;\n"
            f"{assertions}\n"
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"tokens": 12_345, "contextWindow": 272_000}, "ctx 12,345/272,000 tokens"),
        ({"tokens": 0, "contextWindow": 200_000}, "ctx 0/200,000 tokens"),
        ({"tokens": None, "contextWindow": 1_000_000}, "ctx ?/1,000,000 tokens"),
        (None, None),
    ],
)
def test_pi_context_displays_full_counts_or_unknown(
    extension: Path, usage: dict | None, expected: str | None
) -> None:
    run_node(
        extension,
        f"usage = {json.dumps(usage)};\n"
        "handlers.get('session_start')({}, ctx);\n"
        f"assert.equal(statuses.get('sandboxsh-context'), {json.dumps(expected)} ?? undefined);\n"
        "assert.equal(statuses.get('personal'), 'keep me');",
    )


def test_pi_context_refreshes_after_usage_and_session_changes(extension: Path) -> None:
    run_node(
        extension,
        """
const events = ['session_start', 'session_switch', 'session_fork', 'session_tree',
                'session_compact', 'model_select', 'message_end', 'agent_end'];
let tokens = 1000;
for (const event of events) {
    usage = { tokens: ++tokens, contextWindow: 200000 };
    handlers.get(event)({}, ctx);
    assert.equal(statuses.get('sandboxsh-context'),
                 `ctx ${tokens.toLocaleString('en-US')}/200,000 tokens`);
}
usage = { tokens: null, contextWindow: 200000 };
handlers.get('session_compact')({}, ctx);
assert.equal(statuses.get('sandboxsh-context'), 'ctx ?/200,000 tokens');
usage = undefined;
handlers.get('model_select')({}, ctx);
assert.equal(statuses.get('sandboxsh-context'), undefined);
""",
    )


def test_pi_context_does_not_use_ui_in_print_mode(extension: Path) -> None:
    run_node(
        extension,
        """
for (const handler of handlers.values()) {
    handler({}, { hasUI: false,
                  getContextUsage() { throw new Error('unexpected context read'); },
                  ui: { setStatus() { throw new Error('unexpected UI call'); } } });
}
""",
    )
