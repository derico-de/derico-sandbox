import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from sandboxsh.cli import cli
from sandboxsh.config import load_config
from sandboxsh.incus import Incus
from sandboxsh.process import Result


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
    for command in (
        "init",
        "up",
        "down",
        "recreate",
        "destroy",
        "exec",
        "image",
        "setup",
        "update",
    ):
        assert command in result.output


def test_setup_prepares_project_local_network(monkeypatch) -> None:
    monkeypatch.setattr("sandboxsh.cli.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(Incus, "setup_project_network", lambda self: "incusbr-test")

    result = CliRunner().invoke(cli, ["setup"])

    assert result.exit_code == 0, result.output
    assert "project-local managed network incusbr-test" in result.output


@pytest.mark.parametrize(
    ("arguments", "source_override", "expected_source"),
    (
        (
            ["update"],
            None,
            "git+https://github.com/derico-de/derico-sandbox.git@main",
        ),
        (
            ["update", "--ref", "v1.2.3"],
            "git+https://example.invalid/fork.git@old",
            "git+https://github.com/derico-de/derico-sandbox.git@v1.2.3",
        ),
    ),
)
def test_update_reinstalls_requested_github_revision(
    monkeypatch, arguments: list[str], source_override: str | None, expected_source: str
) -> None:
    class FakeRunner:
        def __init__(self) -> None:
            self.commands = []

        def run(self, command, **kwargs):
            self.commands.append((list(command), kwargs))
            return Result("", "", 0)

    runner = FakeRunner()
    monkeypatch.setattr("sandboxsh.cli.Runner", lambda: runner)
    monkeypatch.setattr("sandboxsh.cli.shutil.which", lambda command: f"/usr/bin/{command}")
    if source_override is None:
        monkeypatch.delenv("SANDBOXSH_INSTALL_SOURCE", raising=False)
    else:
        monkeypatch.setenv("SANDBOXSH_INSTALL_SOURCE", source_override)

    result = CliRunner().invoke(cli, arguments)

    assert result.exit_code == 0, result.output
    assert runner.commands == [
        (
            [
                "pipx",
                "install",
                "--force",
                expected_source,
            ],
            {"capture": False},
        )
    ]
    assert "Updated sandboxsh from" in result.output


def test_image_delete_allows_cleanup_before_network_migration(monkeypatch) -> None:
    calls = {}

    def verify(self, *, require_project_network: bool = True):
        calls["require_project_network"] = require_project_network
        return self.project

    def command(self, *args, **kwargs):
        calls["command"] = args
        return Result("", "", 0)

    monkeypatch.setattr(Incus, "verify_host_access", verify)
    monkeypatch.setattr(Incus, "image_exists", lambda self, image: True)
    monkeypatch.setattr(Incus, "command", command)

    result = CliRunner().invoke(cli, ["image", "delete", "-y"])

    assert result.exit_code == 0, result.output
    assert calls == {
        "require_project_network": False,
        "command": ("image", "delete", "sandboxsh/base"),
    }


def test_destroy_allows_cleanup_before_network_migration(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / ".sandboxsh.json"
    path.write_text(json.dumps({"name": "legacy", "dirs": ["."]}))
    calls = {}

    def verify(self, *, require_project_network: bool = True):
        calls["require_project_network"] = require_project_network
        return self.project

    def destroy(self, project, *, cleanup_acl: bool = True):
        calls["cleanup_acl"] = cleanup_acl

    monkeypatch.setattr(Incus, "verify_host_access", verify)
    monkeypatch.setattr(Incus, "project_has_local_networking", lambda self: False)
    monkeypatch.setattr(Incus, "destroy", destroy)

    result = CliRunner().invoke(cli, ["--config", str(path), "destroy", "-y"])

    assert result.exit_code == 0, result.output
    assert calls == {"require_project_network": False, "cleanup_acl": False}


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
