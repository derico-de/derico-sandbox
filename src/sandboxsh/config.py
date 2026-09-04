from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

CONFIG_NAME = ".sandboxsh.json"
_NAME_RE = re.compile(r"[^a-zA-Z0-9.-]+")
_SIZE_RE = re.compile(r"^[1-9][0-9]*(?:[KMGTPE]i?B?|[kmgtpe])?$")


def parse_size(value: str) -> int:
    """Bytes for a size string in the form `_SIZE_RE` accepts.

    Incus reads `GiB` as binary and `GB`/`G` as decimal; the same rule here
    keeps a comparison between two configured sizes consistent with what Incus
    would allocate for each of them.
    """
    match = re.fullmatch(r"([1-9][0-9]*)([KMGTPEkmgtpe]?)(i?)(B?)", value)
    if match is None:
        raise ConfigError(f"invalid size value: {value}")
    number, unit, binary, _ = match.groups()
    base = 1024 if binary else 1000
    exponent = "kmgtpe".index(unit.lower()) + 1 if unit else 0
    return int(number) * base**exponent


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _safe_text(value: str, label: str) -> str:
    unsafe_categories = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
    if _CONTROL_RE.search(value) or any(
        unicodedata.category(character) in unsafe_categories for character in value
    ):
        raise ConfigError(f"{label} contains control or formatting characters")
    return value


def _reject_unknown(mapping: dict[str, Any], allowed: set[str], label: str) -> None:
    unknown = sorted(set(mapping) - allowed)
    if unknown:
        raise ConfigError(f"unknown {label} field(s): {', '.join(unknown)}")


def sanitize_name(value: str) -> str:
    value = _safe_text(value, "name")
    value = _NAME_RE.sub("-", value.strip()).strip("-.").lower()
    if not value:
        raise ConfigError("name must contain at least one letter or number")
    return value[:40]


def find_config(start: Path | None = None) -> Path:
    current = (start or Path.cwd()).resolve()
    if current.is_file():
        current = current.parent
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    raise ConfigError(f"no {CONFIG_NAME} found here or in a parent directory")


@dataclass(frozen=True)
class Mount:
    source: Path
    target: str
    readonly: bool
    inside_project: bool

    @property
    def approval_key(self) -> str:
        mode = "ro" if self.readonly else "rw"
        return f"{self.source}|{self.target}|{mode}"


@dataclass(frozen=True)
class FirewallEntry:
    host: str
    ports: tuple[int, ...] = (443,)
    protocol: str = "tcp"
    allow_private: bool = False


@dataclass(frozen=True)
class PortMapping:
    """One declared development port, and how it is reached from outside.

    `guest` is what the service binds inside the VM. `host` is the port the
    tailnet node listens on; they differ only when two projects would otherwise
    claim the same host port.
    """

    guest: int
    host: int
    tailnet: bool = True

    @property
    def approval_key(self) -> str:
        return f"{self.guest}|{self.host}"


@dataclass(frozen=True)
class TailscaleSettings:
    enabled: bool = True
    address: str | None = None


@dataclass(frozen=True)
class Resources:
    cpus: int = 4
    memory: str = "8GiB"
    disk: str = "40GiB"


@dataclass(frozen=True)
class ProjectConfig:
    path: Path
    name: str
    workdir: str
    mounts: tuple[Mount, ...]
    ports: tuple[PortMapping, ...]
    firewall_enabled: bool
    firewall_allow: tuple[FirewallEntry, ...]
    resources: Resources
    image: str = "sandboxsh/base"
    agent_credentials: bool = True
    tailscale: TailscaleSettings = field(default_factory=TailscaleSettings)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def root(self) -> Path:
        return self.path.parent

    @property
    def guest_ports(self) -> tuple[int, ...]:
        """Ports the host-enforced ACL opens for inbound traffic."""
        return tuple(sorted({mapping.guest for mapping in self.ports}))

    @property
    def publishable(self) -> tuple[PortMapping, ...]:
        """Mappings this project asks to expose on the tailnet.

        Asking is not authority: each mapping is still published only after the
        trusted host approves it, because .sandboxsh.json is guest-writable.
        """
        if not self.tailscale.enabled:
            return ()
        return tuple(mapping for mapping in self.ports if mapping.tailnet)

    @property
    def instance_name(self) -> str:
        # Do not let an agent-controlled name edit accumulate persistent VMs.
        digest = hashlib.sha256(str(self.path).encode()).hexdigest()[:8]
        # Incus instance names reject dots, which sanitize_name keeps.
        label = sanitize_name(self.path.parent.name).replace(".", "-").strip("-")
        return f"ss-{label}-{digest}"[:63]

    def _immutable_document(self) -> dict[str, Any]:
        # Mounts are absent on purpose: they sync onto the existing VM under
        # the same host approval as at create time, so changing them must not
        # cost the VM-local disk.
        return {
            "image": self.image,
            "resources": {
                "cpus": self.resources.cpus,
                "memory": self.resources.memory,
                "disk": self.resources.disk,
            },
            "agent_credentials": self.agent_credentials,
        }

    @property
    def immutable_fingerprint(self) -> str:
        return _fingerprint(self._immutable_document())

    def legacy_immutable_fingerprint(self, mounts: Iterable[tuple[str, str, bool]]) -> str:
        """The retired format that froze mounts, kept for in-place upgrade.

        The caller supplies the mount rows the old fingerprint captured —
        recovered from the VM's devices, not this config, because the config
        may have changed since the VM was stamped.
        """
        document = self._immutable_document()
        document["mounts"] = [[source, target, readonly] for source, target, readonly in mounts]
        return _fingerprint(document)

    @property
    def acl_name(self) -> str:
        return f"acl-{self.instance_name}"[:63]


def _fingerprint(document: dict[str, Any]) -> str:
    serialized = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode()).hexdigest()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _parse_port(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{label} must be an integer port")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{label} must be an integer port") from exc
    if not 1 <= port <= 65535:
        raise ConfigError(f"{label} must be between 1 and 65535")
    return port


def _parse_port_mapping(value: Any, index: int) -> PortMapping:
    if isinstance(value, dict):
        _reject_unknown(value, {"guest", "host", "tailnet"}, f"ports[{index}]")
        if "guest" not in value:
            raise ConfigError(f'ports[{index}] must set "guest"')
        guest = _parse_port(value["guest"], f"ports[{index}].guest")
        host = _parse_port(value.get("host", guest), f"ports[{index}].host")
        tailnet = value.get("tailnet", True)
        if not isinstance(tailnet, bool):
            raise ConfigError(f"ports[{index}].tailnet must be true or false")
        return PortMapping(guest=guest, host=host, tailnet=tailnet)
    port = _parse_port(value, f"ports[{index}]")
    return PortMapping(guest=port, host=port)


def _parse_ports(data: dict[str, Any]) -> tuple[PortMapping, ...]:
    entries = data.get("ports", [])
    if not isinstance(entries, list):
        raise ConfigError('"ports" must be an array')
    mappings = tuple(_parse_port_mapping(value, index) for index, value in enumerate(entries))

    seen_guest: set[int] = set()
    seen_host: set[int] = set()
    for mapping in mappings:
        if mapping.guest in seen_guest:
            raise ConfigError(f'duplicate guest port in "ports": {mapping.guest}')
        seen_guest.add(mapping.guest)
        if not mapping.tailnet:
            continue
        # Two mappings on one host port would race for the same listener.
        if mapping.host in seen_host:
            raise ConfigError(f'duplicate published host port in "ports": {mapping.host}')
        seen_host.add(mapping.host)
    return tuple(sorted(mappings, key=lambda mapping: mapping.guest))


def _parse_tailscale(data: dict[str, Any]) -> TailscaleSettings:
    value = data.get("tailscale", {})
    if isinstance(value, bool):
        return TailscaleSettings(enabled=value)
    if not isinstance(value, dict):
        raise ConfigError('"tailscale" must be an object or boolean')
    _reject_unknown(value, {"enabled", "address"}, "tailscale")
    enabled = value.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("tailscale.enabled must be true or false")
    address = value.get("address")
    if address is not None:
        if not isinstance(address, str):
            raise ConfigError("tailscale.address must be a string")
        _safe_text(address, "tailscale.address")
        try:
            parsed = ipaddress.ip_address(address.strip())
        except ValueError as exc:
            raise ConfigError(f"tailscale.address must be an IP address: {address}") from exc
        address = str(parsed)
    return TailscaleSettings(enabled=enabled, address=address)


def _parse_mounts(data: dict[str, Any], root: Path) -> tuple[Mount, ...]:
    entries = data.get("dirs", ["."])
    if not isinstance(entries, list) or not entries:
        raise ConfigError('"dirs" must be a non-empty array')

    mounts: list[Mount] = []
    used_targets: set[str] = set()
    for index, entry in enumerate(entries):
        if isinstance(entry, str):
            source_text, readonly, target_text = entry, False, None
        elif isinstance(entry, dict):
            _reject_unknown(entry, {"path", "ro", "target"}, f"dirs[{index}]")
            source_text = entry.get("path")
            readonly = entry.get("ro", False)
            target_text = entry.get("target")
            if not isinstance(source_text, str) or not source_text:
                raise ConfigError(f"dirs[{index}].path must be a non-empty string")
            if not isinstance(readonly, bool):
                raise ConfigError(f"dirs[{index}].ro must be true or false")
            if target_text is not None and not isinstance(target_text, str):
                raise ConfigError(f"dirs[{index}].target must be a string")
        else:
            raise ConfigError(f"dirs[{index}] must be a string or object")

        _safe_text(source_text, f"dirs[{index}].path")
        unresolved = Path(source_text).expanduser()
        source = (
            (root / unresolved).resolve() if not unresolved.is_absolute() else unresolved.resolve()
        )
        _safe_text(str(source), f"dirs[{index}] resolved path")
        if not source.exists():
            raise ConfigError(f"mount source does not exist: {source}")

        if target_text:
            _safe_text(target_text, f"dirs[{index}].target")
            if not target_text.startswith("/") or ".." in Path(target_text).parts:
                raise ConfigError(
                    f"dirs[{index}].target must be an absolute guest path without '..'"
                )
            target = target_text.rstrip("/") or "/"
        else:
            base = source.name or "workspace"
            target = f"/workspaces/{base}"
            suffix = 2
            while target in used_targets:
                target = f"/workspaces/{base}-{suffix}"
                suffix += 1
        if target in used_targets:
            raise ConfigError(f"duplicate guest mount target: {target}")
        used_targets.add(target)
        mounts.append(
            Mount(
                source=source,
                target=target,
                readonly=readonly,
                inside_project=_is_relative_to(source, root),
            )
        )

    root_mount = next((mount for mount in mounts if mount.source == root), None)
    if root_mount is None:
        raise ConfigError('"dirs" must include "." so the project root is mounted')
    if root_mount.readonly:
        raise ConfigError("the project root mount must be read-write")
    return tuple(mounts)


def parse_firewall_entry(value: Any, index: int, context: str = "firewall.allow") -> FirewallEntry:
    label = f"{context}[{index}]"
    if isinstance(value, str):
        host = value
        ports: Any = [443]
        protocol = "tcp"
        allow_private = False
    elif isinstance(value, dict):
        _reject_unknown(value, {"host", "ports", "protocol", "allow_private"}, label)
        host = value.get("host")
        ports = value.get("ports", [443])
        protocol = value.get("protocol", "tcp")
        allow_private = value.get("allow_private", False)
    else:
        raise ConfigError(f"{label} must be a hostname string or object")
    if not isinstance(host, str) or not host.strip():
        raise ConfigError(f"{label}.host must be a non-empty string")
    _safe_text(host, f"{label}.host")
    if protocol not in {"tcp", "udp"}:
        raise ConfigError(f"{label}.protocol must be tcp or udp")
    if not isinstance(allow_private, bool):
        raise ConfigError(f"{label}.allow_private must be true or false")
    if not isinstance(ports, list) or not ports:
        raise ConfigError(f"{label}.ports must be a non-empty array")
    parsed_ports = {_parse_port(port, f"{label}.ports") for port in ports}
    return FirewallEntry(
        host=host.strip(),
        ports=tuple(sorted(parsed_ports)),
        protocol=protocol,
        allow_private=allow_private,
    )


def load_config(path: Path | None = None) -> ProjectConfig:
    config_path = (path or find_config()).resolve()
    try:
        data = json.loads(config_path.read_text())
    except OSError as exc:
        raise ConfigError(f"cannot read configuration {config_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError("configuration root must be an object")
    _reject_unknown(
        data,
        {
            "name",
            "workdir",
            "dirs",
            "ports",
            "firewall",
            "resources",
            "limits",
            "agent_credentials",
            "image",
            "tailscale",
        },
        "configuration",
    )

    name_value = data.get("name")
    if not isinstance(name_value, str) or not name_value.strip():
        raise ConfigError('missing required string field "name"')
    name = sanitize_name(name_value)
    root = config_path.parent.resolve()
    mounts = _parse_mounts(data, root)

    workdir_value = data.get("workdir")
    if workdir_value is None:
        workdir = next(mount.target for mount in mounts if mount.source == root)
    elif not isinstance(workdir_value, str) or not workdir_value.startswith("/"):
        raise ConfigError('"workdir" must be an absolute path inside the VM')
    else:
        workdir = _safe_text(workdir_value, "workdir")

    ports = _parse_ports(data)
    tailscale = _parse_tailscale(data)

    firewall = data.get("firewall", {})
    if not isinstance(firewall, dict):
        raise ConfigError('"firewall" must be an object')
    _reject_unknown(firewall, {"enabled", "allow"}, "firewall")
    enabled = firewall.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("firewall.enabled must be true or false")
    allow_value = firewall.get("allow", [])
    if not isinstance(allow_value, list):
        raise ConfigError("firewall.allow must be an array")
    allow = tuple(parse_firewall_entry(value, index) for index, value in enumerate(allow_value))

    if "resources" in data and "limits" in data:
        raise ConfigError('use either "resources" or legacy "limits", not both')
    limits = data.get("resources", data.get("limits", {}))
    if not isinstance(limits, dict):
        raise ConfigError('"resources" must be an object')
    _reject_unknown(limits, {"cpus", "memory", "disk"}, "resources")
    cpus = limits.get("cpus", 4)
    if isinstance(cpus, bool) or not isinstance(cpus, int) or not 1 <= cpus <= 128:
        raise ConfigError("resources.cpus must be an integer between 1 and 128")
    memory = str(limits.get("memory", "8GiB"))
    disk = str(limits.get("disk", "40GiB"))
    if not _SIZE_RE.match(memory):
        raise ConfigError(f"invalid resources.memory value: {memory}")
    if not _SIZE_RE.match(disk):
        raise ConfigError(f"invalid resources.disk value: {disk}")

    image = data.get("image", "sandboxsh/base")
    if not isinstance(image, str) or not image:
        raise ConfigError('"image" must be a non-empty string')
    image = _safe_text(image, "image")
    agent_credentials = data.get("agent_credentials", True)
    if not isinstance(agent_credentials, bool):
        raise ConfigError("agent_credentials must be true or false")

    return ProjectConfig(
        path=config_path,
        name=name,
        workdir=workdir,
        mounts=mounts,
        ports=ports,
        firewall_enabled=enabled,
        firewall_allow=allow,
        resources=Resources(cpus=cpus, memory=memory, disk=disk),
        image=image,
        agent_credentials=agent_credentials,
        tailscale=tailscale,
        raw=data,
    )
