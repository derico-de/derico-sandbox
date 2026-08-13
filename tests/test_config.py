import json
from pathlib import Path

import pytest

from sandboxsh.config import ConfigError, load_config, sanitize_name


def write_config(root: Path, data: dict) -> Path:
    path = root / ".sandboxsh.json"
    path.write_text(json.dumps(data))
    return path


def test_defaults_mount_project_and_generate_stable_instance_name(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"name": "Demo Project", "dirs": ["."]})

    config = load_config(path)

    assert config.name == "demo-project"
    assert config.mounts[0].source == tmp_path.resolve()
    assert config.mounts[0].target == f"/workspaces/{tmp_path.name}"
    assert config.mounts[0].readonly is False
    assert config.workdir == config.mounts[0].target
    assert config.instance_name.startswith(f"ss-{sanitize_name(tmp_path.name)}-")
    assert config.firewall_enabled is True
    assert config.resources.disk == "40GiB"


def test_instance_name_replaces_dots_from_directory_name(tmp_path: Path) -> None:
    project = tmp_path / "derico.de"
    project.mkdir()
    path = write_config(project, {"name": "derico.de", "dirs": ["."]})

    config = load_config(path)

    assert config.instance_name.startswith("ss-derico-de-")
    assert "." not in config.instance_name


def test_external_and_readonly_mounts_are_resolved(tmp_path: Path) -> None:
    project = tmp_path / "project"
    reference = tmp_path / "reference"
    project.mkdir()
    reference.mkdir()
    path = write_config(
        project,
        {
            "name": "demo",
            "dirs": [".", {"path": "../reference", "ro": True, "target": "/reference"}],
        },
    )

    config = load_config(path)

    external = config.mounts[1]
    assert external.source == reference.resolve()
    assert external.target == "/reference"
    assert external.readonly is True
    assert external.inside_project is False


def test_project_root_is_required_and_writable(tmp_path: Path) -> None:
    child = tmp_path / "child"
    child.mkdir()
    path = write_config(tmp_path, {"name": "demo", "dirs": ["child"]})
    with pytest.raises(ConfigError, match='must include "\\."'):
        load_config(path)

    path = write_config(tmp_path, {"name": "demo", "dirs": [{"path": ".", "ro": True}]})
    with pytest.raises(ConfigError, match="must be read-write"):
        load_config(path)


def test_instance_identity_ignores_agent_controlled_name(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"name": "first", "dirs": ["."]})
    first = load_config(path)
    path = write_config(tmp_path, {"name": "changed", "dirs": ["."]})
    second = load_config(path)
    assert first.instance_name == second.instance_name


def test_immutable_fingerprint_tracks_vm_shape_not_mutable_policy(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"name": "demo", "dirs": ["."], "ports": [3000]})
    first = load_config(path)
    path = write_config(tmp_path, {"name": "demo", "dirs": ["."], "ports": [8080]})
    second = load_config(path)
    assert first.immutable_fingerprint == second.immutable_fingerprint

    path = write_config(
        tmp_path,
        {"name": "demo", "dirs": ["."], "resources": {"cpus": 8}},
    )
    third = load_config(path)
    assert third.immutable_fingerprint != first.immutable_fingerprint


def test_unknown_fields_are_rejected(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"name": "demo", "dirs": ["."], "resource": {}})
    with pytest.raises(ConfigError, match="unknown configuration field"):
        load_config(path)


def test_control_characters_are_rejected_before_approval_prompts(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"name": "demo", "dirs": [{"path": ".", "target": "/work\u001b[2J"}]},
    )
    with pytest.raises(ConfigError, match="control or formatting characters"):
        load_config(path)

    for unsafe in ("\u009b", "\u202e"):
        path = write_config(
            tmp_path,
            {"name": "demo", "dirs": [{"path": ".", "target": f"/work{unsafe}spoof"}]},
        )
        with pytest.raises(ConfigError, match="control or formatting characters"):
            load_config(path)


def test_firewall_and_ports_are_validated(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "name": "demo",
            "dirs": ["."],
            "ports": [3000, "8080"],
            "firewall": {
                "allow": [
                    "example.test",
                    {"host": "registry.test", "ports": [443, 5000], "protocol": "tcp"},
                ]
            },
        },
    )

    config = load_config(path)

    assert config.guest_ports == (3000, 8080)
    assert config.firewall_allow[1].ports == (443, 5000)

    path = write_config(tmp_path, {"name": "demo", "dirs": ["."], "ports": [70000]})
    with pytest.raises(ConfigError, match="between 1 and 65535"):
        load_config(path)


def test_ports_default_to_the_same_host_port_on_the_tailnet(tmp_path: Path) -> None:
    path = write_config(tmp_path, {"name": "demo", "dirs": ["."], "ports": [8080]})

    config = load_config(path)

    assert config.ports[0].guest == 8080
    assert config.ports[0].host == 8080
    assert config.publishable == config.ports


def test_a_port_can_be_remapped_or_kept_off_the_tailnet(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {
            "name": "demo",
            "dirs": ["."],
            "ports": [
                {"guest": 8080, "host": 18080},
                {"guest": 5432, "tailnet": False},
            ],
        },
    )

    config = load_config(path)

    assert config.guest_ports == (5432, 8080)
    published = config.publishable
    assert [(mapping.guest, mapping.host) for mapping in published] == [(8080, 18080)]


def test_disabling_tailscale_keeps_declared_ports_vm_local(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"name": "demo", "dirs": ["."], "ports": [8080], "tailscale": {"enabled": False}},
    )

    config = load_config(path)

    # The ACL still opens the port for the host that owns the VM.
    assert config.guest_ports == (8080,)
    assert config.publishable == ()


def test_conflicting_port_declarations_are_rejected(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"name": "demo", "dirs": ["."], "ports": [8080, {"guest": 8080, "host": 9090}]},
    )
    with pytest.raises(ConfigError, match="duplicate guest port"):
        load_config(path)

    path = write_config(
        tmp_path,
        {
            "name": "demo",
            "dirs": ["."],
            "ports": [{"guest": 8080, "host": 80}, {"guest": 81, "host": 80}],
        },
    )
    with pytest.raises(ConfigError, match="duplicate published host port"):
        load_config(path)

    path = write_config(
        tmp_path,
        {"name": "demo", "dirs": ["."], "ports": [{"guest": 8080, "listen": "0.0.0.0"}]},
    )
    with pytest.raises(ConfigError, match="unknown ports\\[0\\] field"):
        load_config(path)


def test_tailscale_address_must_be_an_ip(tmp_path: Path) -> None:
    path = write_config(
        tmp_path,
        {"name": "demo", "dirs": ["."], "tailscale": {"address": "powerman"}},
    )
    with pytest.raises(ConfigError, match="must be an IP address"):
        load_config(path)
