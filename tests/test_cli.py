import json
import os
from pathlib import Path

from click.testing import CliRunner

from sandboxsh.cli import cli
from sandboxsh.config import load_config
from sandboxsh.incus import Incus


def test_init_writes_secure_defaults(tmp_path: Path) -> None:
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(cli, ["init", "--name", "My App", "--no-up"])
        assert result.exit_code == 0, result.output
        data = json.loads(Path(".sandboxsh.json").read_text())

    assert data["name"] == "my-app"
    assert data["dirs"] == ["."]
    assert data["firewall"] == {"enabled": True, "allow": []}
    assert data["agent_credentials"] is True
    assert data["resources"]["disk"] == "40GiB"


def test_help_exposes_expected_lifecycle() -> None:
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "up", "down", "recreate", "destroy", "exec", "image"):
        assert command in result.output


def test_guest_exec_uses_dev_identity_and_configured_workdir(tmp_path: Path) -> None:
    path = tmp_path / ".sandboxsh.json"
    path.write_text(
        json.dumps(
            {
                "name": "demo",
                "dirs": ["."],
                "workdir": "/workspaces/custom",
            }
        )
    )
    config = load_config(path)

    argv = Incus().exec_argv(config, ("docker", "compose", "ps"))

    assert argv[:6] == [
        "incus",
        "--force-local",
        "--project",
        f"user-{os.getuid()}",
        "exec",
        config.instance_name,
    ]
    assert "runuser" in argv
    assert "dev" in argv
    assert argv[-4:] == ["/workspaces/custom", "docker", "compose", "ps"]
