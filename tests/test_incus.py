import json
import os
from pathlib import Path

from sandboxsh import security
from sandboxsh.config import load_config
from sandboxsh.errors import CommandError, SandboxshError
from sandboxsh.incus import Incus
from sandboxsh.process import Result


class FakeRunner:
    def __init__(self, responses=None):
        self.commands = []
        self.responses = list(responses or [])

    def run(self, command, **kwargs):
        self.commands.append((list(command), kwargs))
        if self.responses:
            return self.responses.pop(0)
        return Result("", "", 0)


def config(tmp_path: Path):
    path = tmp_path / ".sandboxsh.json"
    path.write_text(json.dumps({"name": "demo", "dirs": ["."]}))
    return load_config(path)


def test_every_incus_command_forces_local_restricted_project() -> None:
    runner = FakeRunner()
    incus = Incus(runner)

    incus.command("info")

    assert runner.commands[0][0][:4] == [
        "incus",
        "--force-local",
        "--project",
        f"user-{os.getuid()}",
    ]
    assert runner.commands[0][1]["env"]["INCUS_CONF"].endswith("/sandboxsh/incus-client")


def test_acl_update_explicitly_scopes_restricted_project(
    tmp_path: Path, monkeypatch
) -> None:
    class RestrictedAclRunner(FakeRunner):
        def run(self, command, **kwargs):
            self.commands.append((list(command), kwargs))
            operation = list(command)[4:]
            if "query" in command and "--project" in command:
                raise CommandError(list(command), 1, "--project cannot be used with query")
            if operation[:4] == ["profile", "device", "get", "default"]:
                return Result("sandboxshbr0\n", "", 0)
            if operation[:3] == ["network", "get", "sandboxshbr0"]:
                return Result("10.10.10.1/24\n", "", 0)
            if operation[:4] == ["network", "acl", "list", "--format=json"]:
                return Result("[]", "", 0)
            if operation[:3] == ["network", "acl", "edit"]:
                raise CommandError(
                    list(command),
                    1,
                    'Error: User does not have permission for project "default"',
                )
            return Result("", "", 0)

    monkeypatch.setattr(security, "DEFAULT_ENDPOINTS", ())
    runner = RestrictedAclRunner()
    incus = Incus(runner)
    project = config(tmp_path)

    incus.apply_acl(project)

    query = next(command for command, _ in runner.commands if "query" in command)
    assert query[-1] == f"/1.0/network-acls/{project.acl_name}?project={incus.project}"
    assert query[:6] == ["incus", "--force-local", "query", "-X", "PUT", "-d"]
    assert "--project" not in query


def test_apply_acl_reuses_stale_acl_when_create_reports_exists(
    tmp_path: Path, monkeypatch
) -> None:
    class StaleAclRunner(FakeRunner):
        def run(self, command, **kwargs):
            self.commands.append((list(command), kwargs))
            operation = list(command)[4:]
            if operation[:4] == ["profile", "device", "get", "default"]:
                return Result("sandboxshbr0\n", "", 0)
            if operation[:3] == ["network", "get", "sandboxshbr0"]:
                return Result("10.10.10.1/24\n", "", 0)
            if operation[:4] == ["network", "acl", "list", "--format=json"]:
                return Result("[]", "", 0)
            if operation[:3] == ["network", "acl", "create"]:
                error = 'Error: The network ACL already exists'
                if kwargs.get("check", True):
                    raise CommandError(list(command), 1, error)
                return Result("", error, 1)
            return Result("", "", 0)

    monkeypatch.setattr(security, "DEFAULT_ENDPOINTS", ())
    runner = StaleAclRunner()
    incus = Incus(runner)

    incus.apply_acl(config(tmp_path))

    assert any("query" in command for command, _ in runner.commands)


def test_delete_acl_explicitly_scopes_restricted_project(tmp_path: Path) -> None:
    class RestrictedDeleteRunner(FakeRunner):
        def run(self, command, **kwargs):
            self.commands.append((list(command), kwargs))
            operation = list(command)[4:]
            if operation[:3] == ["network", "acl", "delete"]:
                return Result(
                    "",
                    'Error: User does not have permission for project "default"',
                    1,
                )
            return Result("", "", 0)

    runner = RestrictedDeleteRunner()
    incus = Incus(runner)
    project = config(tmp_path)

    incus.delete_acl(project)

    query = next(command for command, _ in runner.commands if "query" in command)
    assert query[:5] == ["incus", "--force-local", "query", "-X", "DELETE"]
    assert query[-1] == f"/1.0/network-acls/{project.acl_name}?project={incus.project}"
    assert "--project" not in query


def test_delete_acl_is_idempotent_without_listing(tmp_path: Path) -> None:
    runner = FakeRunner([Result("", "Error: Network ACL not found", 1)])
    incus = Incus(runner)

    incus.delete_acl(config(tmp_path))

    assert runner.commands[0][0][:5] == [
        "incus",
        "--force-local",
        "query",
        "-X",
        "DELETE",
    ]
    assert runner.commands[0][1]["check"] is False


def test_delete_acl_preserves_unexpected_errors(tmp_path: Path) -> None:
    runner = FakeRunner([Result("", "permission denied", 1)])
    incus = Incus(runner)

    try:
        incus.delete_acl(config(tmp_path))
    except SandboxshError as exc:
        assert "permission denied" in str(exc)
    else:
        raise AssertionError("unexpected ACL deletion error was suppressed")


def test_guest_ip_uses_eth0_not_docker0(tmp_path: Path) -> None:
    payload = [
        {
            "name": config(tmp_path).instance_name,
            "state": {
                "network": {
                    "docker0": {
                        "addresses": [
                            {"family": "inet", "scope": "global", "address": "172.17.0.1"}
                        ]
                    },
                    "eth0": {
                        "addresses": [
                            {"family": "inet", "scope": "global", "address": "10.25.0.42"}
                        ]
                    },
                }
            },
        }
    ]
    runner = FakeRunner([Result(json.dumps(payload), "", 0)])

    assert Incus(runner).guest_ip(config(tmp_path)) == "10.25.0.42"


def test_stop_is_idempotent_for_stopped_instance(tmp_path: Path) -> None:
    project = config(tmp_path)
    payload = json.dumps([{"name": project.instance_name, "status": "Stopped"}])
    runner = FakeRunner([Result(payload, "", 0)])
    incus = Incus(runner)

    incus.stop(project)

    assert len(runner.commands) == 1
    assert "stop" not in runner.commands[0][0]


def test_lifecycle_query_errors_do_not_look_like_absence(tmp_path: Path) -> None:
    runner = FakeRunner([Result("", "permission denied", 1)])
    incus = Incus(runner)

    try:
        incus.stop(config(tmp_path))
    except SandboxshError as exc:
        assert "permission denied" in str(exc)
    else:
        raise AssertionError("query error was treated as an absent instance")
