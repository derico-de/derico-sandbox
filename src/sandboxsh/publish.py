from __future__ import annotations

import ipaddress
import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import PortMapping, ProjectConfig
from .errors import SandboxshError
from .process import Runner

# A fixed, auditable host operation, mirroring `sandboxsh-bridge-forward`. The
# CLI never writes unit files or calls systemctl itself, so the privileged
# surface stays two verbs (`sync`, `clear`) with validated arguments.
HELPER = Path("/usr/local/sbin/sandboxsh-publish-port")

INSTALL_HELPER_HINT = (
    f"the host helper {HELPER} is missing, so declared ports stay reachable only "
    "at the VM address. Install it by re-running the installer:\n"
    "  curl -fsSL https://raw.githubusercontent.com/derico-de/"
    "derico-sandbox/main/install.sh | bash -s -- --publish-helper --no-deps"
)


@dataclass(frozen=True)
class Endpoint:
    mapping: PortMapping
    address: str
    hostname: str | None

    @property
    def authority(self) -> str:
        return f"{self.hostname or self.address}:{self.mapping.host}"

    @property
    def url(self) -> str:
        return f"http://{self.authority}"


class Publisher:
    """Publish declared guest ports on the host's tailnet address.

    The listener runs on the host and dials the VM over the Incus bridge, so the
    connection reaching the guest is sourced from the bridge gateway -- exactly
    what the ACL's ingress rule already allows. Publishing therefore needs no
    change to the host-enforced network policy.
    """

    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or Runner()

    def helper_available(self) -> bool:
        return HELPER.exists()

    def _tailscale(self) -> str:
        binary = shutil.which("tailscale")
        if binary is None:
            raise SandboxshError(
                "tailscale is not installed on this host, so there is no tailnet "
                'address to publish on. Install Tailscale, or set "tailscale": '
                '{"address": "..."} to bind a specific address.'
            )
        return binary

    def address(self, config: ProjectConfig) -> str:
        if config.tailscale.address:
            return config.tailscale.address
        result = self.runner.run([self._tailscale(), "ip", "-4"], check=False)
        for line in result.stdout.splitlines():
            candidate = line.strip()
            if not candidate:
                continue
            try:
                return str(ipaddress.IPv4Address(candidate))
            except ValueError:
                continue
        raise SandboxshError(
            "this host has no IPv4 tailnet address; run `tailscale up` first, or "
            'set "tailscale": {"address": "..."} to bind a specific address.'
        )

    def hostname(self) -> str | None:
        """The node's MagicDNS name, for URLs a human can retype."""
        binary = shutil.which("tailscale")
        if binary is None:
            return None
        result = self.runner.run([binary, "status", "--json"], check=False)
        if result.returncode:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        name = (payload.get("Self") or {}).get("DNSName")
        if not isinstance(name, str) or not name.strip("."):
            return None
        # MagicDNS resolves the short name inside the tailnet.
        return name.rstrip(".").split(".")[0]

    def sync(
        self,
        config: ProjectConfig,
        *,
        guest_ip: str,
        address: str,
        mappings: tuple[PortMapping, ...],
    ) -> tuple[Endpoint, ...]:
        if not self.helper_available():
            raise SandboxshError(INSTALL_HELPER_HINT)
        if not mappings:
            self.clear(config)
            return ()
        arguments = [f"{mapping.host}:{mapping.guest}" for mapping in mappings]
        result = self.runner.run(
            [
                "sudo",
                str(HELPER),
                "sync",
                config.instance_name,
                address,
                guest_ip,
                *arguments,
            ],
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise SandboxshError(f"cannot publish ports on the tailnet: {detail}")
        hostname = self.hostname()
        return tuple(
            Endpoint(mapping=mapping, address=address, hostname=hostname) for mapping in mappings
        )

    def clear(self, config: ProjectConfig) -> None:
        if not self.helper_available():
            return
        result = self.runner.run(
            ["sudo", str(HELPER), "clear", config.instance_name],
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
            raise SandboxshError(f"cannot withdraw published ports: {detail}")
