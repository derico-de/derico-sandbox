import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from sandboxsh.cli import cli
from sandboxsh.config import load_config
from sandboxsh.incus import PNPM_STORE_DIR, Incus
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
        "agent",
        "image",
        "update",
    ):
        assert command in result.output


def test_doctor_requires_bridge_netfilter(monkeypatch) -> None:
    original_exists = Path.exists

    def exists(path: Path) -> bool:
        if path == Path("/dev/kvm"):
            return True
        if path == Path("/sys/module/br_netfilter"):
            return False
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr("sandboxsh.cli.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(Incus, "verify_host_access", lambda self: self.project)
    monkeypatch.setattr(Incus, "default_network", lambda self: "incusbr-1000")
    monkeypatch.setattr(Incus, "blocked_forwarding_remedy", lambda self, network: None)

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert result.exit_code == 1
    assert "FAIL br_netfilter kernel module is not loaded" in result.output
    assert "PASS host forwards traffic from bridge incusbr-1000" in result.output


def test_doctor_reports_a_container_runtime_blocking_the_bridge(monkeypatch) -> None:
    monkeypatch.setattr(Path, "exists", lambda path: True)
    monkeypatch.setattr("sandboxsh.cli.shutil.which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(Incus, "verify_host_access", lambda self: self.project)
    monkeypatch.setattr(Incus, "default_network", lambda self: "incusbr-1000")
    monkeypatch.setattr(
        Incus,
        "blocked_forwarding_remedy",
        lambda self, network: f"FORWARD policy is DROP and nothing accepts {network}",
    )

    result = CliRunner().invoke(cli, ["doctor"])

    assert result.exit_code == 1
    assert "FAIL FORWARD policy is DROP and nothing accepts incusbr-1000" in result.output


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


def test_guest_shell_keeps_pnpm_store_off_host_mount(tmp_path: Path) -> None:
    path = tmp_path / ".sandboxsh.json"
    path.write_text(json.dumps({"name": "demo", "dirs": ["."]}))
    config = load_config(path)

    argv = Incus().shell_argv(config)

    assert f"PNPM_CONFIG_STORE_DIR={PNPM_STORE_DIR}" in argv


@pytest.mark.parametrize("agent", ("claude", "pi"))
def test_guest_agent_exec_sets_the_host_visible_herdr_hint(monkeypatch, agent: str) -> None:
    monkeypatch.setenv("HERDR_AGENT", "stale-global-value")
    incus = Incus()

    environment = incus.exec_environment((agent,))

    assert environment["HERDR_AGENT"] == agent


def test_ordinary_guest_exec_clears_a_stale_herdr_hint(monkeypatch) -> None:
    monkeypatch.setenv("HERDR_AGENT", "pi")
    incus = Incus()

    environment = incus.exec_environment(("docker", "compose", "ps"))

    assert "HERDR_AGENT" not in environment


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
    assert f"PNPM_CONFIG_STORE_DIR={PNPM_STORE_DIR}" in argv
    assert argv[-4:] == ["/workspaces/custom", "docker", "compose", "ps"]


class RecordingRunner:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def run(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        return Result("", "", 0)

    def exec(self, command, **kwargs):  # pragma: no cover - not expected in these tests
        raise AssertionError("unexpected exec")


def publishing_project(tmp_path: Path, monkeypatch, **updates) -> Path:
    from sandboxsh import publish, security
    from sandboxsh.incus import Incus as RealIncus

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "state"))
    data = {
        "name": "demo",
        "dirs": ["."],
        "ports": [8080, {"guest": 8085, "host": 18085}],
        "tailscale": {"address": "100.64.0.1"},
    }
    data.update(updates)
    path = tmp_path / ".sandboxsh.json"
    path.write_text(json.dumps(data))

    monkeypatch.setattr(RealIncus, "verify_host_access", lambda self: self.project)
    monkeypatch.setattr(RealIncus, "instance_status", lambda self, instance: "Running")
    monkeypatch.setattr(RealIncus, "guest_ip", lambda self, config: "10.138.35.7")
    # No tailscale binary, so URLs fall back to the configured address.
    monkeypatch.setattr(publish.shutil, "which", lambda command: None)
    monkeypatch.setattr(security.click, "confirm", lambda *args, **kwargs: True)
    monkeypatch.setattr("sandboxsh.cli._interactive", lambda: True)
    return path


def test_publish_maps_approved_ports_onto_the_tailnet_address(tmp_path: Path, monkeypatch) -> None:
    from sandboxsh.publish import Publisher

    path = publishing_project(tmp_path, monkeypatch)
    runner = RecordingRunner()
    monkeypatch.setattr("sandboxsh.cli.Runner", lambda: runner)
    monkeypatch.setattr(Publisher, "helper_available", lambda self: True)

    result = CliRunner().invoke(cli, ["--config", str(path), "publish"])

    assert result.exit_code == 0, result.output
    sync = next(command for command in runner.commands if "sync" in command)
    assert sync[:3] == ["sudo", "/usr/local/sbin/sandboxsh-publish-port", "sync"]
    # listen address, then guest address, then <hostport>:<guestport>.
    assert sync[4:] == ["100.64.0.1", "10.138.35.7", "8080:8080", "18085:8085"]
    assert "http://100.64.0.1:8080 -> guest port 8080" in result.output
    assert "http://100.64.0.1:18085 -> guest port 8085" in result.output


def test_publishing_is_skipped_without_the_host_helper(tmp_path: Path, monkeypatch) -> None:
    from sandboxsh.publish import Publisher

    path = publishing_project(tmp_path, monkeypatch)
    runner = RecordingRunner()
    monkeypatch.setattr("sandboxsh.cli.Runner", lambda: runner)
    monkeypatch.setattr(Publisher, "helper_available", lambda self: False)

    result = CliRunner().invoke(cli, ["--config", str(path), "publish"])

    assert result.exit_code == 0, result.output
    assert not any("sandboxsh-publish-port" in " ".join(command) for command in runner.commands)
    assert "helper" in result.output
    assert "Nothing is published" in result.output


def test_url_prefers_the_published_port_over_the_vm_address(tmp_path: Path, monkeypatch) -> None:
    from sandboxsh.publish import Publisher

    path = publishing_project(tmp_path, monkeypatch)
    runner = RecordingRunner()
    monkeypatch.setattr("sandboxsh.cli.Runner", lambda: runner)
    monkeypatch.setattr(Publisher, "helper_available", lambda self: True)
    CliRunner().invoke(cli, ["--config", str(path), "publish"])

    published = CliRunner().invoke(cli, ["--config", str(path), "url", "8085"])
    assert published.output.strip() == "http://100.64.0.1:18085"

    direct = CliRunner().invoke(cli, ["--config", str(path), "url", "8085", "--vm"])
    assert direct.output.strip() == "http://10.138.35.7:8085"


def test_a_port_kept_off_the_tailnet_is_never_published(tmp_path: Path, monkeypatch) -> None:
    from sandboxsh.publish import Publisher

    path = publishing_project(tmp_path, monkeypatch, ports=[{"guest": 5432, "tailnet": False}])
    runner = RecordingRunner()
    monkeypatch.setattr("sandboxsh.cli.Runner", lambda: runner)
    monkeypatch.setattr(Publisher, "helper_available", lambda self: True)

    result = CliRunner().invoke(cli, ["--config", str(path), "publish"])

    assert result.exit_code == 0, result.output
    assert not any("sync" in command for command in runner.commands)
    assert "Nothing is published" in result.output


def host_with_sudo(monkeypatch) -> None:
    """Undo publishing_project's blanket `which` stub for sudo alone."""
    monkeypatch.setattr(
        "sandboxsh.cli.shutil.which",
        lambda command: "/usr/bin/sudo" if command == "sudo" else None,
    )


class SudoRunner(RecordingRunner):
    """A host whose sudo always asks for a password."""

    def run(self, command, **kwargs):
        self.commands.append([str(part) for part in command])
        if command[:2] == ["sudo", "-n"]:
            return Result("", "sudo: a password is required\n", 1)
        return Result("", "", 0)


def test_publish_asks_for_the_host_password_before_the_privileged_helper(
    tmp_path: Path, monkeypatch
) -> None:
    from sandboxsh.publish import Publisher

    path = publishing_project(tmp_path, monkeypatch)
    runner = SudoRunner()
    monkeypatch.setattr("sandboxsh.cli.Runner", lambda: runner)
    monkeypatch.setattr(Publisher, "helper_available", lambda self: True)
    host_with_sudo(monkeypatch)

    result = CliRunner().invoke(cli, ["--config", str(path), "publish"])

    assert result.exit_code == 0, result.output
    validate = runner.commands.index(["sudo", "-v"])
    helper = next(
        index
        for index, command in enumerate(runner.commands)
        if "sandboxsh-publish-port" in " ".join(command)
    )
    assert validate < helper
    assert "Host password required to publish ports" in result.output


def test_a_refused_host_password_fails_before_any_privileged_work(
    tmp_path: Path, monkeypatch
) -> None:
    from sandboxsh.publish import Publisher

    path = publishing_project(tmp_path, monkeypatch)

    class RefusingRunner(SudoRunner):
        def run(self, command, **kwargs):
            super().run(command, **kwargs)
            return Result("", "", 1)

    runner = RefusingRunner()
    monkeypatch.setattr("sandboxsh.cli.Runner", lambda: runner)
    monkeypatch.setattr(Publisher, "helper_available", lambda self: True)
    host_with_sudo(monkeypatch)

    result = CliRunner().invoke(cli, ["--config", str(path), "publish"])

    assert result.exit_code != 0
    assert "host sudo is required to publish ports" in result.output
    assert not any("sandboxsh-publish-port" in " ".join(command) for command in runner.commands)


def test_cached_sudo_credentials_are_never_reprompted(tmp_path: Path, monkeypatch) -> None:
    from sandboxsh.publish import Publisher

    path = publishing_project(tmp_path, monkeypatch)
    runner = RecordingRunner()  # Every sudo probe succeeds, as with a warm timestamp.
    monkeypatch.setattr("sandboxsh.cli.Runner", lambda: runner)
    monkeypatch.setattr(Publisher, "helper_available", lambda self: True)
    host_with_sudo(monkeypatch)

    result = CliRunner().invoke(cli, ["--config", str(path), "publish"])

    assert result.exit_code == 0, result.output
    assert ["sudo", "-v"] not in runner.commands
    assert "Host password required" not in result.output
