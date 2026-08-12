import json
import os
from pathlib import Path

from sandboxsh import security
from sandboxsh.config import load_config
from sandboxsh.errors import SandboxshError
from sandboxsh.incus import (
    PIN_BEGIN,
    PIN_END,
    Incus,
    acl_project_lookup_is_broken,
    parse_server_version,
)
from sandboxsh.process import Result
from sandboxsh.security import AclPolicy


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


def test_verify_host_access_requires_incus_user_network_project() -> None:
    project = f"user-{os.getuid()}"
    runner = FakeRunner(
        [
            Result(json.dumps([{"name": project}]), "", 0),
            Result("true\n", "", 0),
            Result("true\n", "", 0),
        ]
    )

    try:
        Incus(runner).verify_host_access()
    except SandboxshError as exc:
        assert "features.networks=false" in str(exc)
    else:
        raise AssertionError("unsupported project-local networking was accepted")


def _host_access_responses(version: str | None) -> list[Result]:
    project = f"user-{os.getuid()}"
    responses = [
        Result(json.dumps([{"name": project}]), "", 0),
        Result("true\n", "", 0),
        Result("false\n", "", 0),
    ]
    if version is None:
        responses.append(Result("", "not found", 1))
    else:
        responses.append(Result(json.dumps({"environment": {"server_version": version}}), "", 0))
    return responses


def test_acl_project_lookup_is_broken_before_the_upstream_fixes() -> None:
    broken = ("5.21.2", "6.0.0", "6.0.4", "6.0.5", "6.14.0", "6.21.0")
    fixed = ("6.0.6", "6.0.7", "6.22.0", "6.23.0", "7.0.0")

    for version in broken:
        assert acl_project_lookup_is_broken(parse_server_version(version)), version
    for version in fixed:
        assert not acl_project_lookup_is_broken(parse_server_version(version)), version


def test_verify_host_access_rejects_daemon_that_cannot_apply_the_acl() -> None:
    runner = FakeRunner(_host_access_responses("6.0.4"))

    try:
        Incus(runner).verify_host_access()
    except SandboxshError as exc:
        assert "6.0.4" in str(exc)
        assert "Network ACL not found" in str(exc)
    else:
        raise AssertionError("a daemon that cannot enforce the NIC ACL was accepted")


def test_verify_host_access_accepts_a_patched_daemon() -> None:
    runner = FakeRunner(_host_access_responses("6.0.6"))

    assert Incus(runner).verify_host_access() == f"user-{os.getuid()}"


def test_verify_host_access_tolerates_an_unreadable_daemon_version() -> None:
    runner = FakeRunner(_host_access_responses(None))

    assert Incus(runner).verify_host_access() == f"user-{os.getuid()}"


def test_host_acl_name_is_user_scoped_and_bounded(tmp_path: Path) -> None:
    acl = Incus(FakeRunner())._host_acl_name(config(tmp_path))

    assert acl.startswith(f"acl-u{os.getuid()}-")
    assert len(acl) <= 63


def test_acl_management_uses_admin_default_network_project(
    tmp_path: Path, monkeypatch
) -> None:
    class AclRunner(FakeRunner):
        def run(self, command, **kwargs):
            self.commands.append((list(command), kwargs))
            operation = list(command)[4:]
            if operation[:4] == ["profile", "device", "get", "default"]:
                return Result("incusbr-1000\n", "", 0)
            if operation[:3] == ["network", "get", "incusbr-1000"]:
                return Result("10.10.10.1/24\n", "", 0)
            return Result("", "", 0)

    monkeypatch.setattr(security, "DEFAULT_ENDPOINTS", ())
    runner = AclRunner()
    incus = Incus(runner)
    project = config(tmp_path)

    incus.apply_acl(project)

    acl = incus._host_acl_name(project)
    commands = [command for command, _ in runner.commands]
    assert [
        "sudo",
        "incus",
        "--force-local",
        "--project",
        "default",
        "network",
        "acl",
        "create",
        acl,
    ] in commands
    query = next(command for command in commands if "query" in command)
    assert query[:7] == ["sudo", "incus", "--force-local", "query", "-X", "PUT", "-d"]
    assert query[-1] == f"/1.0/network-acls/{acl}?project=default"


def test_apply_acl_reuses_stale_acl_when_create_reports_exists(
    tmp_path: Path, monkeypatch
) -> None:
    class StaleAclRunner(FakeRunner):
        def run(self, command, **kwargs):
            self.commands.append((list(command), kwargs))
            operation = list(command)[4:]
            if operation[:4] == ["profile", "device", "get", "default"]:
                return Result("incusbr-1000\n", "", 0)
            if operation[:3] == ["network", "get", "incusbr-1000"]:
                return Result("10.10.10.1/24\n", "", 0)
            if command[:3] == ["sudo", "incus", "--force-local"] and "create" in command:
                return Result("", "Error: The network ACL already exists", 1)
            return Result("", "", 0)

    monkeypatch.setattr(security, "DEFAULT_ENDPOINTS", ())
    runner = StaleAclRunner()
    incus = Incus(runner)

    incus.apply_acl(config(tmp_path))

    assert any("query" in command for command, _ in runner.commands)


def test_pin_allowlist_writes_the_host_resolved_addresses() -> None:
    runner = FakeRunner()
    policy = AclPolicy(
        document={},
        resolutions={
            "download.docker.com": ("18.0.0.1", "18.0.0.2", "2600::1"),
            "10.8.0.0/24": ("10.8.0.0/24",),
        },
        unresolved_defaults=(),
    )

    Incus(runner).pin_allowlist(policy, instance="ss-demo")

    command = runner.commands[0][0]
    assert command[4:7] == ["exec", "ss-demo", "--"]
    # IPv6 edges are dropped while a usable IPv4 address exists: a name answered
    # from /etc/hosts never falls back to DNS for the other family.
    assert command[-1].splitlines() == [
        PIN_BEGIN,
        "18.0.0.1 download.docker.com",
        "18.0.0.2 download.docker.com",
        PIN_END,
    ]
    # A stale block must not accumulate across refreshes.
    assert f"sed -i '/{PIN_BEGIN}/,/{PIN_END}/d' /etc/hosts" in command[7 + 2]


def test_pin_allowlist_keeps_ipv6_only_endpoints() -> None:
    runner = FakeRunner()
    policy = AclPolicy(
        document={}, resolutions={"v6.example": ("2600::1",)}, unresolved_defaults=()
    )

    Incus(runner).pin_allowlist(policy, instance="ss-demo")

    assert "2600::1 v6.example" in runner.commands[0][0][-1]


def test_unpin_allowlist_removes_only_the_managed_block() -> None:
    runner = FakeRunner()

    Incus(runner).unpin_allowlist("ss-demo")

    command = runner.commands[0][0]
    assert command[4:] == [
        "exec",
        "ss-demo",
        "--",
        "sed",
        "-i",
        f"/{PIN_BEGIN}/,/{PIN_END}/d",
        "/etc/hosts",
    ]


def test_delete_acl_uses_admin_default_network_project(tmp_path: Path) -> None:
    runner = FakeRunner()
    incus = Incus(runner)
    project = config(tmp_path)

    incus.delete_acl(project)

    query = runner.commands[0][0]
    assert query[:6] == ["sudo", "incus", "--force-local", "query", "-X", "DELETE"]
    assert query[-1] == (
        f"/1.0/network-acls/{incus._host_acl_name(project)}?project=default"
    )


def test_delete_acl_is_idempotent_without_listing(tmp_path: Path) -> None:
    runner = FakeRunner([Result("", "Error: Network ACL not found", 1)])
    incus = Incus(runner)

    incus.delete_acl(config(tmp_path))

    assert runner.commands[0][0][:6] == [
        "sudo",
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
