from __future__ import annotations

import ipaddress
import json
import os
import socket
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import click

from .config import FirewallEntry, Mount, PortMapping, ProjectConfig, parse_firewall_entry
from .errors import ConfigError, SandboxshError

# These endpoints are part of the trusted base policy. Project configuration can
# add endpoints, but additions require approval stored outside the mounted repo.
DEFAULT_ENDPOINTS: tuple[FirewallEntry, ...] = (
    FirewallEntry("api.anthropic.com", (443,)),
    FirewallEntry("statsig.anthropic.com", (443,)),
    FirewallEntry("api.openai.com", (443,)),
    FirewallEntry("auth.openai.com", (443,)),
    FirewallEntry("chatgpt.com", (443,)),
    FirewallEntry("api.mistral.ai", (443,)),
    FirewallEntry("api.firecrawl.dev", (443,)),
    FirewallEntry("github.com", (22, 443)),
    FirewallEntry("api.github.com", (443,)),
    FirewallEntry("codeload.github.com", (443,)),
    FirewallEntry("objects.githubusercontent.com", (443,)),
    FirewallEntry("raw.githubusercontent.com", (443,)),
    FirewallEntry("registry.npmjs.org", (443,)),
    FirewallEntry("pypi.org", (443,)),
    FirewallEntry("files.pythonhosted.org", (443,)),
    FirewallEntry("astral.sh", (443,)),
    FirewallEntry("deb.debian.org", (80, 443)),
    FirewallEntry("security.debian.org", (80, 443)),
    FirewallEntry("deb.nodesource.com", (443,)),
    FirewallEntry("dl.google.com", (443,)),
    # Playwright browser builds; the image bakes in Chromium, and a project
    # pinned to another Playwright version may use either download mirror.
    FirewallEntry("cdn.playwright.dev", (443,)),
    FirewallEntry("playwright.download.prss.microsoft.com", (443,)),
    FirewallEntry("download.docker.com", (443,)),
    FirewallEntry("registry-1.docker.io", (443,)),
    FirewallEntry("auth.docker.io", (443,)),
    FirewallEntry("production.cloudflare.docker.com", (443,)),
    # Docker Hub serves blobs from CloudFront as well; pulls fetch the
    # manifest via registry-1 and then hit whichever CDN the blob URL names.
    FirewallEntry("production.cloudfront.docker.com", (443,)),
    FirewallEntry("impeccable.style", (443,)),
)


def state_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sandboxsh"


def _endpoints_path() -> Path:
    return state_home() / "endpoints.json"


def host_endpoints() -> tuple[FirewallEntry, ...]:
    """Host-wide endpoints every sandbox may reach, e.g. tailnet services.

    The file lives outside every mounted project, so like DEFAULT_ENDPOINTS it
    is trusted host policy and needs no per-project approval. Entries use the
    same shape as firewall.allow in .sandboxsh.json.
    """
    path = _endpoints_path()
    if not path.exists():
        return ()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxshError(f"cannot read host endpoints {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SandboxshError(f"unsupported host endpoints format: {path}")
    allow = data.get("allow", [])
    if not isinstance(allow, list):
        raise SandboxshError(f'"allow" must be an array in {path}')
    try:
        return tuple(
            parse_firewall_entry(value, index, "allow") for index, value in enumerate(allow)
        )
    except ConfigError as exc:
        raise SandboxshError(f"invalid host endpoint in {path}: {exc}") from exc


def _approval_path() -> Path:
    return state_home() / "approvals.json"


def _load_approvals() -> dict:
    path = _approval_path()
    if not path.exists():
        return {"version": 1, "projects": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxshError(f"cannot read approval ledger {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SandboxshError(f"unsupported approval ledger format: {path}")
    data.setdefault("projects", {})
    return data


def _save_approvals(data: dict) -> None:
    path = _approval_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def _sensitive_paths() -> tuple[Path, ...]:
    home = Path.home().resolve()
    return tuple(
        (home / item).resolve()
        for item in (
            ".ssh",
            ".gnupg",
            ".aws",
            ".kube",
            ".docker",
            ".claude",
            ".pi",
            ".vibe",
            ".config/incus",
            ".config/gcloud",
            ".config/sandboxsh",
            ".cache/sandboxsh",
            ".local/share/keyrings",
            ".local/share/pipx",
            ".local/pipx",
        )
    ) + (Path("/var/lib/incus"), Path("/run/incus"), Path("/var/run"))


def _overlaps(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def assert_mount_safe(mount: Mount) -> None:
    if mount.source == Path("/"):
        raise SandboxshError("refusing to mount the host root filesystem")
    if os.environ.get("SANDBOXSH_ALLOW_SENSITIVE_MOUNTS") == "1":
        return
    for sensitive in _sensitive_paths():
        if _overlaps(mount.source, sensitive):
            raise SandboxshError(
                f"refusing sensitive host mount {mount.source}; "
                "set SANDBOXSH_ALLOW_SENSITIVE_MOUNTS=1 on the host for an explicit override"
            )


def _firewall_key(entry: FirewallEntry) -> str:
    private = "private" if entry.allow_private else "public"
    return f"{entry.protocol}|{entry.host}|{','.join(str(port) for port in entry.ports)}|{private}"


def ensure_project_approvals(config: ProjectConfig, *, prompt: bool) -> None:
    """Approve project-controlled expansion of host and network authority.

    The ledger is outside the mounted project, so an agent can edit
    .sandboxsh.json but cannot silently gain another host path or endpoint.
    """
    for mount in config.mounts:
        assert_mount_safe(mount)

    ledger = _load_approvals()
    project_key = str(config.path)
    project = ledger["projects"].setdefault(project_key, {"mounts": [], "firewall": []})
    changed = False

    for mount in config.mounts:
        if mount.inside_project or mount.approval_key in project["mounts"]:
            continue
        if not prompt:
            raise SandboxshError(
                f"external mount is not approved: {mount.source} -> {mount.target}; "
                "run `sandboxsh approve` from an interactive host terminal"
            )
        mode = "read-only" if mount.readonly else "READ-WRITE"
        if not click.confirm(
            f"Allow project {config.name!r} to expose host path\n"
            f"  {mount.source}\ninside the VM at {mount.target} ({mode})?",
            default=False,
        ):
            raise SandboxshError("mount approval denied")
        project["mounts"].append(mount.approval_key)
        changed = True

    for entry in config.firewall_allow:
        key = _firewall_key(entry)
        if key in project["firewall"]:
            continue
        if not prompt:
            raise SandboxshError(
                f"network endpoint is not approved: {entry.host}:{entry.ports}; "
                "run `sandboxsh approve` from an interactive host terminal"
            )
        private_note = " including private addresses" if entry.allow_private else ""
        if not click.confirm(
            f"Allow project {config.name!r} network access to "
            f"{entry.host} ({entry.protocol}/{','.join(map(str, entry.ports))})"
            f"{private_note}?",
            default=False,
        ):
            raise SandboxshError("network approval denied")
        project["firewall"].append(key)
        changed = True

    if changed:
        _save_approvals(ledger)


def revoke_project_approvals(config: ProjectConfig) -> None:
    ledger = _load_approvals()
    if ledger["projects"].pop(str(config.path), None) is not None:
        _save_approvals(ledger)


def approved_publications(
    config: ProjectConfig, *, prompt: bool
) -> tuple[tuple[PortMapping, ...], tuple[PortMapping, ...]]:
    """Split requested tailnet publications into approved and pending.

    Publishing moves a port from "reachable by the host that owns this VM" to
    "reachable by every node on the tailnet", and the request comes from the
    guest-writable .sandboxsh.json. It therefore needs the same host-side
    approval as a mount or an egress endpoint.

    Unlike those, an unapproved publication is returned as pending rather than
    raised: the guest still works without it, so a non-interactive `up` should
    keep the sandbox host-local instead of failing outright.
    """
    requested = config.publishable
    if not requested:
        return (), ()

    ledger = _load_approvals()
    project_key = str(config.path)
    project = ledger["projects"].setdefault(project_key, {"mounts": [], "firewall": []})
    recorded = project.setdefault("publish", [])

    approved: list[PortMapping] = []
    pending: list[PortMapping] = []
    changed = False
    for mapping in requested:
        if mapping.approval_key in recorded:
            approved.append(mapping)
            continue
        if not prompt:
            pending.append(mapping)
            continue
        if not click.confirm(
            f"Publish project {config.name!r} guest port {mapping.guest} to the whole "
            f"tailnet on host port {mapping.host}?",
            default=False,
        ):
            pending.append(mapping)
            continue
        recorded.append(mapping.approval_key)
        approved.append(mapping)
        changed = True

    if changed:
        _save_approvals(ledger)
    return tuple(approved), tuple(pending)


def _registry_path() -> Path:
    return state_home() / "published-ports.json"


def _load_registry() -> dict:
    path = _registry_path()
    if not path.exists():
        return {"version": 1, "ports": {}}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SandboxshError(f"cannot read published port registry {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SandboxshError(f"unsupported published port registry format: {path}")
    data.setdefault("ports", {})
    return data


def _save_registry(data: dict) -> None:
    path = _registry_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def claim_host_ports(config: ProjectConfig, mappings: tuple[PortMapping, ...]) -> None:
    """Reserve host ports for this project, refusing another project's port.

    A tailnet node has one port 8080. Without this, the second project to start
    would bind nothing and its `url` output would quietly point at the first
    project's service.
    """
    registry = _load_registry()
    ports = registry["ports"]
    project_key = str(config.path)

    for mapping in mappings:
        owner = ports.get(str(mapping.host))
        if owner is not None and owner.get("project") != project_key:
            raise SandboxshError(
                f"host port {mapping.host} is already published by {owner['project']} "
                f"(guest port {owner.get('guest')}). Pick another host port for this "
                f'project, e.g. {{"guest": {mapping.guest}, "host": {mapping.host + 10000}}} '
                'in "ports".'
            )

    wanted = {str(mapping.host): mapping for mapping in mappings}
    stale = [
        port
        for port, owner in ports.items()
        if owner.get("project") == project_key and port not in wanted
    ]
    for port in stale:
        del ports[port]
    for port, mapping in wanted.items():
        ports[port] = {
            "project": project_key,
            "instance": config.instance_name,
            "guest": mapping.guest,
        }
    _save_registry(registry)


def release_host_ports(config: ProjectConfig) -> None:
    registry = _load_registry()
    ports = registry["ports"]
    project_key = str(config.path)
    owned = [port for port, owner in ports.items() if owner.get("project") == project_key]
    if not owned:
        return
    for port in owned:
        del ports[port]
    _save_registry(registry)


def _hostname(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_network(candidate, strict=False)
        return candidate
    except ValueError:
        pass
    if (
        not candidate
        or "*" in candidate
        or "://" in candidate
        or "@" in candidate
        or "/" in candidate
        or any(character.isspace() for character in candidate)
    ):
        raise SandboxshError(
            f"firewall host must be a plain hostname, IP, or CIDR without wildcards: {value!r}"
        )
    return candidate


def resolve_host(value: str) -> tuple[str, ...]:
    host = _hostname(value)
    try:
        network = ipaddress.ip_network(host, strict=False)
        return (str(network),)
    except ValueError:
        pass

    try:
        records = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise SandboxshError(f"cannot resolve allowlisted host {host}: {exc}") from exc
    addresses = {record[4][0] for record in records}
    if not addresses:
        raise SandboxshError(f"allowlisted host resolved to no addresses: {host}")
    return tuple(sorted(addresses, key=lambda item: (":" in item, item)))


@dataclass(frozen=True)
class AclPolicy:
    document: dict
    resolutions: dict[str, tuple[str, ...]]
    unresolved_defaults: tuple[str, ...]


def _validate_destination(address: str, *, allow_private: bool, host: str) -> None:
    try:
        network = ipaddress.ip_network(address, strict=False)
    except ValueError as exc:
        raise SandboxshError(f"invalid resolved address for {host}: {address}") from exc
    if (
        network.is_loopback
        or network.is_link_local
        or network.is_multicast
        or network.is_unspecified
        or network.is_reserved
    ):
        raise SandboxshError(f"refusing special-use destination for {host}: {address}")
    if not allow_private and not network.is_global:
        raise SandboxshError(
            f"{host} resolved to non-public destination {address}; set allow_private=true "
            "and approve it from the host if this is intentional"
        )


def build_acl_policy(
    config: ProjectConfig,
    *,
    bridge_gateway: str | None = None,
) -> AclPolicy:
    if not config.firewall_enabled and os.environ.get("SANDBOXSH_ALLOW_OPEN_NETWORK") != "1":
        raise SandboxshError(
            "firewall.enabled=false is blocked for YOLO safety; set "
            "SANDBOXSH_ALLOW_OPEN_NETWORK=1 in the host shell to override"
        )

    if not config.firewall_enabled:
        return AclPolicy(
            document={
                "description": f"sandboxsh open-network policy for {config.name}",
                "config": {},
                "ingress": [],
                "egress": [{"action": "allow", "state": "enabled"}],
            },
            resolutions={},
            unresolved_defaults=(),
        )

    grouped: dict[tuple[str, tuple[int, ...]], set[str]] = defaultdict(set)
    resolutions: dict[str, tuple[str, ...]] = {}
    unresolved_defaults = []
    for entry in (*DEFAULT_ENDPOINTS, *host_endpoints()):
        try:
            addresses = resolve_host(entry.host)
        except SandboxshError:
            # Omitting an unavailable built-in or host-wide endpoint keeps the
            # ACL fail-closed without blocking every `up` on the machine (a
            # tailnet name resolves only while tailscale is up). Explicit
            # project endpoints remain strict below because the user asked for
            # those destinations and may depend on them.
            unresolved_defaults.append(entry.host)
            continue
        for address in addresses:
            _validate_destination(address, allow_private=entry.allow_private, host=entry.host)
        resolutions[entry.host] = addresses
        grouped[(entry.protocol, entry.ports)].update(addresses)

    for entry in config.firewall_allow:
        addresses = resolve_host(entry.host)
        for address in addresses:
            _validate_destination(address, allow_private=entry.allow_private, host=entry.host)
        resolutions[entry.host] = addresses
        grouped[(entry.protocol, entry.ports)].update(addresses)

    egress = []
    for (protocol, ports), addresses in sorted(grouped.items()):
        egress.append(
            {
                "action": "allow",
                "state": "enabled",
                "description": "sandboxsh resolved allowlist",
                "destination": ",".join(sorted(addresses)),
                "protocol": protocol,
                "destination_port": ",".join(str(port) for port in ports),
            }
        )

    ingress = []
    if config.guest_ports:
        rule = {
            "action": "allow",
            "state": "enabled",
            "description": "host access to declared development ports",
            "protocol": "tcp",
            "destination_port": ",".join(str(port) for port in config.guest_ports),
        }
        if bridge_gateway:
            rule["source"] = bridge_gateway
        ingress.append(rule)

    return AclPolicy(
        document={
            "description": f"Managed by sandboxsh for {config.instance_name}",
            "config": {},
            "ingress": ingress,
            "egress": egress,
        },
        resolutions=resolutions,
        unresolved_defaults=tuple(unresolved_defaults),
    )
