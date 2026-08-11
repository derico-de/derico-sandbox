import json
import os
from pathlib import Path

from sandboxsh.config import load_config
from sandboxsh.errors import SandboxshError
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
