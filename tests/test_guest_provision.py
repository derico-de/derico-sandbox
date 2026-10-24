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


def test_uv_installer_uses_the_requested_version() -> None:
    body = non_root_provisioning_body()

    assert "https://astral.sh/uv/0.13.4/install.sh" in body
    assert body.index("sh /tmp/uv-install.sh") < body.index("uv tool install ")


@pytest.mark.parametrize("package", ["pip==26.2.2", "sigstore==4.5.1"])
def test_python_tools_are_isolated_with_updated_certifi(package: str) -> None:
    body = non_root_provisioning_body()

    assert f'uv tool install "{package}" --with "certifi==2026.10.19"' in body
    assert "--break-system-packages" not in body


def test_pixi_uses_the_requested_version_and_the_existing_user_path() -> None:
    body = non_root_provisioning_body()

    assert "https://raw.githubusercontent.com/prefix-dev/pixi/v0.82.1/install/install.sh" in body
    assert (
        'PIXI_VERSION=0.82.1 PIXI_BIN_DIR="$HOME/.local/bin" PIXI_NO_PATH_UPDATE=1 \\\n'
        "    sh /tmp/pixi-install.sh"
    ) in body


@pytest.mark.parametrize("command", ["uv", "pip", "pip3", "sigstore", "pixi"])
def test_updated_tools_are_smoke_checked_as_the_developer(command: str) -> None:
    body = non_root_provisioning_body()

    assert f"\n{command} --version\n" in body
    assert body.index(f"\n{command} --version\n") > body.index("sh /tmp/pixi-install.sh")


def test_pi_extensions_may_run_build_scripts_before_the_first_install() -> None:
    body = non_root_provisioning_body()

    workspace = body.split(
        'cat > "$HOME/.pi/agent/npm/pnpm-workspace.yaml" <<PI_WORKSPACE\n', 1
    )[1].split("\nPI_WORKSPACE", 1)[0]
    assert workspace == "dangerouslyAllowAllBuilds: true"
    assert body.index("PI_WORKSPACE") < body.index("pi install ")


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


def test_powerman_loopback_entry_survives_cloud_init_and_repeated_provisioning(
    tmp_path: Path,
) -> None:
    hosts = tmp_path / "hosts"
    template = tmp_path / "hosts.debian.tmpl"
    hosts.write_text("127.0.0.1 localhost\n")
    template.write_text("127.0.1.1 {{fqdn}} {{hostname}}\n127.0.0.1 localhost\n")
    block = FINALIZE.read_text().split("for hosts_file in ", 1)[1].split("\ndone\n", 1)[0]
    script = "set -euo pipefail\nfor hosts_file in " + block + "\ndone\n"
    script = script.replace("/etc/hosts", str(hosts)).replace(
        "/etc/cloud/templates/hosts.debian.tmpl", str(template)
    )

    for _ in range(2):
        result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    assert hosts.read_text().splitlines().count("127.0.0.1 powerman") == 1
    assert template.read_text().splitlines().count("127.0.0.1 powerman") == 1
    assert "127.0.0.1 localhost" in hosts.read_text()
    assert "127.0.1.1 {{fqdn}} {{hostname}}" in template.read_text()


def test_finalize_fetches_nothing_from_the_network() -> None:
    text = FINALIZE.read_text()

    assert "curl " not in text
    assert "apt-get install" not in text
    assert "pnpm_global" not in text
    assert "pi install" not in text
    assert "uv tool install" not in text


def test_an_oom_killed_guest_process_does_not_take_the_agent_sessions_down() -> None:
    text = FINALIZE.read_text()

    dropin = text.split(
        "cat > /etc/systemd/system/incus-agent.service.d/sandboxsh-oom.conf <<'DROPIN'\n", 1
    )[1].split("\nDROPIN\n", 1)[0]
    assert dropin.splitlines() == ["[Service]", "OOMPolicy=continue", "OOMScoreAdjust=-1000"]


def test_chrome_answers_to_the_debian_chromium_names() -> None:
    text = (STAGES / "40-browser.sh").read_text()

    chrome_install = text.index("apt-get install -y --no-install-recommends google-chrome-stable")
    for name in ("chromium", "chromium-browser"):
        link = f"ln -sfn /usr/bin/google-chrome /usr/bin/{name}"
        assert text.index(link) > chrome_install
