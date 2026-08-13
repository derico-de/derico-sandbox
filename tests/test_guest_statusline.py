import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required in the guest")


def statusline_script() -> str:
    agent_init = (Path(__file__).parents[1] / "guest" / "agent-init.sh").read_text()
    marker = "cat > \"$tmp\" <<'STATUSLINE'\n"
    return agent_init.split(marker, 1)[1].split("\nSTATUSLINE", 1)[0]


def run_statusline(tmp_path: Path, payload: dict) -> str:
    script = tmp_path / "statusline.sh"
    script.write_text(statusline_script())
    result = subprocess.run(
        ["sh", str(script)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return result.stdout.strip()


def test_statusline_shows_context_and_subscriber_plan_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    workspace.mkdir()
    payload = {
        "model": {"display_name": "Fable 5"},
        "workspace": {"current_dir": str(workspace)},
        "context_window": {
            "used_percentage": 18.9,
            "total_input_tokens": 36_000,
            "context_window_size": 200_000,
        },
        "rate_limits": {
            "five_hour": {"used_percentage": 23.5},
            "seven_day": {"used_percentage": 41.2},
        },
    }

    output = run_statusline(tmp_path, payload)

    assert output == "[Fable 5] demo | ctx 18% (36k/200k) | 5h 23% | week 41%"


def test_statusline_omits_unavailable_plan_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    workspace.mkdir()
    payload = {
        "model": {"display_name": "Claude"},
        "workspace": {"current_dir": str(workspace)},
        "context_window": {"used_percentage": 4},
    }

    output = run_statusline(tmp_path, payload)

    assert output == "[Claude] demo | ctx 4%"


def test_statusline_handles_independently_available_plan_limits(tmp_path: Path) -> None:
    workspace = tmp_path / "demo"
    workspace.mkdir()
    payload = {
        "model": {"display_name": "Fable 5"},
        "workspace": {"current_dir": str(workspace)},
        "rate_limits": {"seven_day": {"used_percentage": 9.8}},
    }

    output = run_statusline(tmp_path, payload)

    assert output == "[Fable 5] demo | week 9%"
