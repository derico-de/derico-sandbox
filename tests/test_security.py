import json
from pathlib import Path

import pytest

from sandboxsh import security
from sandboxsh.config import FirewallEntry, load_config
from sandboxsh.errors import SandboxshError


def project_config(root: Path, **updates):
    data = {"name": "demo", "dirs": ["."], "ports": [3000]}
    data.update(updates)
    path = root / ".sandboxsh.json"
    path.write_text(json.dumps(data))
    return load_config(path)


def test_acl_is_default_deny_with_only_resolved_destinations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = project_config(
        tmp_path,
        firewall={"enabled": True, "allow": [{"host": "custom.test", "ports": [8443]}]},
    )
    monkeypatch.setattr(security, "DEFAULT_ENDPOINTS", ())
    monkeypatch.setattr(security, "resolve_host", lambda host: ("93.184.216.34",))

    policy = security.build_acl_policy(config, bridge_gateway="10.10.10.1")

    assert policy.document["config"] == {}
    assert policy.document["egress"] == [
        {
            "action": "allow",
            "state": "enabled",
            "description": "sandboxsh resolved allowlist",
            "destination": "93.184.216.34",
            "protocol": "tcp",
            "destination_port": "8443",
        }
    ]
    assert policy.document["ingress"][0]["source"] == "10.10.10.1"
    assert policy.document["ingress"][0]["destination_port"] == "3000"


def test_unresolvable_default_endpoint_is_omitted_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = project_config(tmp_path)
    monkeypatch.setattr(
        security,
        "DEFAULT_ENDPOINTS",
        (
            FirewallEntry("statsig.anthropic.com", (443,)),
            FirewallEntry("api.anthropic.com", (443,)),
        ),
    )

    def resolve(host: str) -> tuple[str, ...]:
        if host == "statsig.anthropic.com":
            raise SandboxshError("cannot resolve allowlisted host statsig.anthropic.com")
        return ("93.184.216.34",)

    monkeypatch.setattr(security, "resolve_host", resolve)

    policy = security.build_acl_policy(config)

    assert policy.resolutions == {"api.anthropic.com": ("93.184.216.34",)}
    assert policy.unresolved_defaults == ("statsig.anthropic.com",)
    assert policy.document["egress"][0]["destination"] == "93.184.216.34"


def test_unresolvable_project_endpoint_remains_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = project_config(tmp_path, firewall={"allow": ["required.test"]})
    monkeypatch.setattr(security, "DEFAULT_ENDPOINTS", ())

    def fail_resolution(host: str) -> tuple[str, ...]:
        raise SandboxshError(f"cannot resolve allowlisted host {host}")

    monkeypatch.setattr(security, "resolve_host", fail_resolution)

    with pytest.raises(SandboxshError, match="cannot resolve allowlisted host required.test"):
        security.build_acl_policy(config)


def test_private_destination_needs_explicit_private_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(security, "DEFAULT_ENDPOINTS", ())
    monkeypatch.setattr(security, "resolve_host", lambda host: ("10.20.30.40",))
    config = project_config(tmp_path, firewall={"allow": ["internal.test"]})
    with pytest.raises(SandboxshError, match="allow_private=true"):
        security.build_acl_policy(config)

    config = project_config(
        tmp_path,
        firewall={"allow": [{"host": "internal.test", "allow_private": True}]},
    )
    policy = security.build_acl_policy(config)
    assert policy.document["egress"][0]["destination"] == "10.20.30.40"


def test_open_network_needs_host_environment_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = project_config(tmp_path, firewall={"enabled": False})
    monkeypatch.delenv("SANDBOXSH_ALLOW_OPEN_NETWORK", raising=False)
    with pytest.raises(SandboxshError, match="blocked for YOLO safety"):
        security.build_acl_policy(config)

    monkeypatch.setenv("SANDBOXSH_ALLOW_OPEN_NETWORK", "1")
    policy = security.build_acl_policy(config)
    assert policy.document["egress"] == [{"action": "allow", "state": "enabled"}]


def test_external_authority_requires_host_side_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    external = tmp_path / "external"
    config_home = tmp_path / "config"
    project.mkdir()
    external.mkdir()
    path = project / ".sandboxsh.json"
    path.write_text(
        json.dumps(
            {
                "name": "demo",
                "dirs": [".", {"path": "../external", "ro": True}],
                "firewall": {"allow": ["custom.test"]},
            }
        )
    )
    config = load_config(path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))

    with pytest.raises(SandboxshError, match="external mount is not approved"):
        security.ensure_project_approvals(config, prompt=False)

    answers = iter([True, True])
    monkeypatch.setattr("click.confirm", lambda *args, **kwargs: next(answers))
    security.ensure_project_approvals(config, prompt=True)
    security.ensure_project_approvals(config, prompt=False)

    ledger = json.loads((config_home / "sandboxsh" / "approvals.json").read_text())
    assert str(path.resolve()) in ledger["projects"]
