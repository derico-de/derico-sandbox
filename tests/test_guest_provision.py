import json
import subprocess
from pathlib import Path

import pytest

STAGES = Path(__file__).parents[1] / "guest" / "stages"
AGENTS = STAGES / "50-agents.sh"
FINALIZE = STAGES / "90-finalize.sh"


def non_root_provisioning_body() -> str:
    """The single-quoted script the agents stage hands to `runuser`."""
    script = AGENTS.read_text()
    return script.split("runuser -u dev -- bash -lc '\n", 1)[1].split("\n'\n", 1)[0]


def login_profile() -> str:
    """The `/etc/profile.d/sandboxsh.sh` heredoc written by the finalize stage."""
    script = FINALIZE.read_text()
    return script.split("cat > /etc/profile.d/sandboxsh.sh <<'PROFILE'\n", 1)[1].split(
        "\nPROFILE\n", 1
    )[0]


@pytest.mark.parametrize("script", sorted(STAGES.glob("*.sh")), ids=lambda path: path.name)
def test_every_stage_script_is_valid_bash(script: Path) -> None:
    result = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", sorted(STAGES.glob("*.sh")), ids=lambda path: path.name)
def test_every_stage_script_fails_fast(script: Path) -> None:
    assert "set -euo pipefail" in script.read_text().splitlines()[:5]


def test_non_root_provisioning_starts_from_the_developer_home() -> None:
    body = non_root_provisioning_body()

    assert body.index('cd "$HOME"') < body.index("pi install npm:pi-subagents")


def test_non_root_provisioning_seeds_valid_pi_settings_without_breaking_shell_quote() -> None:
    body = non_root_provisioning_body()

    # This body is one single-quoted argument to the outer shell. A quote inside
    # it silently changes the command passed to the inner shell while `bash -n`
    # still reports valid syntax.
    assert "'" not in body

    settings = body.split('cat > "$HOME/.pi/agent/settings.json" <<PI_SETTINGS\n', 1)[1].split(
        "\nPI_SETTINGS", 1
    )[0]
    assert json.loads(settings) == {"npmCommand": ["pnpm"]}


def test_the_login_profile_keeps_pnpm_state_off_the_project_mount() -> None:
    profile = login_profile()

    assert 'export PNPM_CONFIG_STORE_DIR="$HOME/.local/share/pnpm/store"' in profile
    assert "export PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright" in profile


def test_only_the_user_stage_reads_the_host_uid_and_gid() -> None:
    for script in sorted(STAGES.glob("*.sh")):
        uses_arguments = "${1:?" in script.read_text()
        assert uses_arguments == (script.name == "30-user.sh"), script.name


def test_stages_that_use_apt_leave_no_package_lists_behind() -> None:
    for script in sorted(STAGES.glob("*.sh")):
        text = script.read_text()
        if "apt-get install" in text:
            assert "rm -rf /var/lib/apt/lists/*" in text, script.name
            assert "apt-get clean" in text, script.name


def test_finalize_fetches_nothing_from_the_network() -> None:
    text = FINALIZE.read_text()

    assert "curl " not in text
    assert "apt-get install" not in text
    assert "pnpm_global" not in text
    assert "pi install" not in text
    assert "uv tool install" not in text
