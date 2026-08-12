from __future__ import annotations

import grp
import hashlib
import ipaddress
import json
import os
import shlex
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from .config import FirewallEntry, ProjectConfig, Resources
from .errors import SandboxshError
from .process import Result, Runner
from .security import AclPolicy, build_acl_policy

DEFAULT_POOL = os.environ.get("SANDBOXSH_STORAGE_POOL", "default")
CREDS_VOLUME = "sandboxsh-agent-creds"

# Older daemons validate a bridged NIC's `security.acls` against the network
# project (`default`, because the user project has features.networks=false) but
# look the same ACL up in the *instance* project while starting the NIC. The ACL
# API only ever writes to the network project, so no reachable configuration
# satisfies both and every start fails with "Network ACL not found". Upstream
# fixed the start-time lookup in 6.0.6 (LTS) and 6.22.0.
ACL_PROJECT_FIX_LTS = (6, 0, 6)
ACL_PROJECT_FIX_FEATURE = (6, 22, 0)

PIN_BEGIN = "# BEGIN sandboxsh allowlist"
PIN_END = "# END sandboxsh allowlist"


def parse_server_version(value: str) -> tuple[int, int, int] | None:
    parts = value.strip().split(".")[:3]
    numbers = []
    for part in parts:
        digits = ""
        for character in part:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        numbers.append(int(digits))
    if not numbers:
        return None
    while len(numbers) < 3:
        numbers.append(0)
    return (numbers[0], numbers[1], numbers[2])


def acl_project_lookup_is_broken(version: tuple[int, int, int]) -> bool:
    if version[0] != 6:
        return version[0] < 6
    if version[1] == 0:
        return version < ACL_PROJECT_FIX_LTS
    return version < ACL_PROJECT_FIX_FEATURE


class Incus:
    def __init__(self, runner: Runner | None = None) -> None:
        self.runner = runner or Runner()
        self.project = f"user-{os.getuid()}"
        cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        self.client_config = cache / "sandboxsh" / "incus-client"
        self.client_config.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.client_config.chmod(0o700)
        self.environment = dict(os.environ)
        # An isolated Incus client config has `local` as its default. It cannot be
        # redirected by a user's normal config.yml to an administrator TLS remote.
        self.environment["INCUS_CONF"] = str(self.client_config)
        self.environment["INCUS_REMOTE"] = "local"

    def command(
        self,
        *args: str,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = True,
    ) -> Result:
        # Never inherit a configured TLS/default remote or project. Force the
        # package-provided local user socket and the per-UID restricted project.
        return self.runner.run(
            ["incus", "--force-local", "--project", self.project, *args],
            input_text=input_text,
            check=check,
            capture=capture,
            env=self.environment,
        )

    def _host_acl_name(self, config: ProjectConfig) -> str:
        prefix = f"acl-u{os.getuid()}-"
        digest = hashlib.sha256(config.acl_name.encode()).hexdigest()[:8]
        stem_length = 63 - len(prefix) - len(digest) - 1
        return f"{prefix}{config.acl_name[:stem_length]}-{digest}"

    def _admin_command(
        self,
        *args: str,
        check: bool = True,
    ) -> Result:
        """Run a fixed ACL control-plane operation through trusted host sudo."""
        return self.runner.run(
            ["sudo", "incus", "--force-local", *args],
            check=check,
        )

    def _admin_acl_query(
        self,
        action: str,
        acl: str,
        *,
        data: dict | None = None,
        check: bool = True,
    ) -> Result:
        path = f"/1.0/network-acls/{quote(acl, safe='')}"
        endpoint = f"{path}?{urlencode({'project': 'default'})}"
        command = ["sudo", "incus", "--force-local", "query", "-X", action]
        if data is not None:
            command.extend(("-d", json.dumps(data)))
        command.append(endpoint)
        return self.runner.run(command, check=check)

    def server_version(self) -> tuple[int, int, int] | None:
        result = self.command("query", "/1.0", check=False)
        if result.returncode:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        environment = payload.get("environment")
        if not isinstance(environment, dict):
            return None
        return parse_server_version(str(environment.get("server_version", "")))

    def verify_acl_enforcement(self) -> tuple[int, int, int] | None:
        """Reject daemons that cannot enforce a NIC ACL in the user project."""
        version = self.server_version()
        if version is None or not acl_project_lookup_is_broken(version):
            return version
        rendered = ".".join(str(part) for part in version)
        raise SandboxshError(
            f"Incus {rendered} cannot enforce per-VM network ACLs in the restricted "
            f"user project {self.project!r}: it starts bridged NICs by resolving "
            "`security.acls` in the instance project while ACLs only exist in the "
            "`default` network project, so every VM fails to start with "
            "'Network ACL not found'. Upgrade to Incus "
            f"{'.'.join(str(part) for part in ACL_PROJECT_FIX_LTS)} (LTS) or "
            f"{'.'.join(str(part) for part in ACL_PROJECT_FIX_FEATURE)}+."
        )

    def verify_host_access(self) -> str:
        """Require the restricted incus-user socket, never incus-admin by default."""
        try:
            admin = grp.getgrnam("incus-admin")
        except KeyError:
            admin = None
        is_admin = bool(
            admin and (admin.gr_gid in os.getgroups() or os.environ.get("USER") in admin.gr_mem)
        )
        if is_admin and os.environ.get("SANDBOXSH_ALLOW_ADMIN") != "1":
            raise SandboxshError(
                "current user has incus-admin access, which is host-root-equivalent. "
                "Use the restricted `incus` group/user socket, or set "
                "SANDBOXSH_ALLOW_ADMIN=1 after reviewing the risk."
            )

        expected = self.project
        result = self.command("project", "list", "--format=json")
        try:
            projects = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SandboxshError("Incus returned invalid project data") from exc
        names = {project.get("name") for project in projects}
        admin_override = os.environ.get("SANDBOXSH_ALLOW_ADMIN") == "1"
        if expected not in names or (not admin_override and names != {expected}):
            raise SandboxshError(
                f"Incus access is not confined exclusively to restricted project {expected!r}. "
                "Ensure the user is in `incus` (not `incus-admin`), incus-user.socket is enabled, "
                "and the package-provided local user socket is active."
            )
        restricted = self.command("project", "get", expected, "restricted").stdout.strip()
        if restricted != "true":
            raise SandboxshError(f"Incus project {expected!r} is not restricted")
        network_feature = self.command(
            "project", "get", expected, "features.networks"
        ).stdout.strip()
        if network_feature not in ("", "false"):
            raise SandboxshError(
                f"Incus project {expected!r} must use features.networks=false "
                "for its incus-user managed bridge"
            )
        # The ACL is the whole network boundary, so a daemon that silently cannot
        # apply it is a hard failure rather than a degraded mode.
        self.verify_acl_enforcement()
        return expected

    @staticmethod
    def _not_found(result: Result) -> bool:
        output = f"{result.stdout}\n{result.stderr}".lower()
        return "not found" in output or "doesn't exist" in output

    @staticmethod
    def _already_exists(result: Result) -> bool:
        output = f"{result.stdout}\n{result.stderr}".lower()
        return "already exists" in output

    @staticmethod
    def _probe_error(label: str, result: Result) -> SandboxshError:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown Incus error"
        return SandboxshError(f"cannot query {label}: {detail}")

    def _instance_record(self, instance: str) -> dict | None:
        result = self.command("list", instance, "--format=json", check=False)
        if result.returncode:
            raise self._probe_error(f"instance {instance}", result)
        try:
            entries = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise SandboxshError(f"Incus returned invalid state for {instance}") from exc
        return next((entry for entry in entries if entry.get("name") == instance), None)

    def exists(self, instance: str) -> bool:
        return self._instance_record(instance) is not None

    def image_exists(self, image: str) -> bool:
        result = self.command("image", "info", image, check=False)
        if result.returncode == 0:
            return True
        if self._not_found(result):
            return False
        raise self._probe_error(f"image {image}", result)

    def volume_exists(self, name: str, pool: str = DEFAULT_POOL) -> bool:
        result = self.command("storage", "volume", "show", pool, name, check=False)
        if result.returncode == 0:
            return True
        if self._not_found(result):
            return False
        raise self._probe_error(f"storage volume {pool}/{name}", result)

    def ensure_credentials_volume(self) -> None:
        if not self.volume_exists(CREDS_VOLUME):
            self.command("storage", "volume", "create", DEFAULT_POOL, CREDS_VOLUME)

    def assert_immutable_config(self, config: ProjectConfig) -> None:
        result = self.command(
            "config",
            "get",
            config.instance_name,
            "user.sandboxsh.immutable",
            check=False,
        )
        actual = result.stdout.strip() if result.returncode == 0 else ""
        if actual != config.immutable_fingerprint:
            raise SandboxshError(
                "mount, image, resource, or agent-credential settings changed; "
                "run `sandboxsh recreate` to apply immutable VM configuration"
            )

    def instance_status(self, instance: str) -> str | None:
        record = self._instance_record(instance)
        return record.get("status") if record else None

    def wait_for_agent(self, instance: str, timeout: int = 300) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.command("exec", instance, "--", "true", check=False)
            if result.returncode == 0:
                return
            time.sleep(2)
        raise SandboxshError(f"timed out waiting for the Incus agent in {instance}")

    def wait_for_mount(self, instance: str, path: str, timeout: int = 120) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.command("exec", instance, "--", "mountpoint", "-q", path, check=False)
            if result.returncode == 0:
                return
            time.sleep(2)
        raise SandboxshError(f"timed out waiting for {path} to mount in {instance}")

    def default_network(self) -> str:
        result = self.command("profile", "device", "get", "default", "eth0", "network", check=False)
        network = result.stdout.strip()
        if result.returncode or not network:
            raise SandboxshError("default Incus profile has no managed eth0 network")
        return network

    def blocked_forwarding_remedy(self, network: str) -> str | None:
        """Report a container runtime's FORWARD lockdown swallowing the bridge.

        The VM's traffic is routed off the bridge, so it crosses the host's IPv4
        FORWARD hook. Docker and podman set that policy to DROP and accept only
        their own bridges, which silently blackholes every allowlisted endpoint;
        host-side checks keep working because host traffic never passes FORWARD.
        """
        forward = self.runner.run(["sudo", "-n", "iptables", "-S", "FORWARD"], check=False)
        if forward.returncode:
            # No iptables, or no cached credentials to read it with.
            return None
        if not any(line.strip() == "-P FORWARD DROP" for line in forward.stdout.splitlines()):
            return None
        # Any chain will do -- ufw, firewalld, and a hand-written DOCKER-USER rule
        # all end up as an accept for the bridge somewhere in the filter table.
        existing = self.runner.run(["sudo", "-n", "iptables", "-S"], check=False)
        if existing.returncode == 0 and any(
            f"-i {network} " in f"{line} " and line.endswith("-j ACCEPT")
            for line in existing.stdout.splitlines()
        ):
            return None
        chain = "DOCKER-USER" if "-j DOCKER-USER" in forward.stdout else "FORWARD"
        return (
            f"the host's IPv4 FORWARD policy is DROP and nothing accepts bridge "
            f"{network!r}, so the VM cannot reach any allowlisted endpoint. A "
            "container runtime (Docker/podman) installs that policy. Allow the "
            "bridge, which leaves ACL enforcement in the bridge table untouched:\n"
            f"  sudo iptables -I {chain} -i {network} -j ACCEPT\n"
            f"  sudo iptables -I {chain} -o {network} -m conntrack "
            "--ctstate RELATED,ESTABLISHED -j ACCEPT\n"
            f"On a ufw-managed host, `sudo ufw route allow in on {network}` is "
            "equivalent and is recognised here too. Both are lost on reboot; "
            "`./install.sh --forward-unit` installs a systemd unit that re-adds "
            "the rule at every boot."
        )

    def bridge_gateway(self, network: str) -> str | None:
        value = self.command("network", "get", network, "ipv4.address", check=False)
        if value.returncode or not value.stdout.strip() or value.stdout.strip() == "none":
            return None
        try:
            return str(ipaddress.ip_interface(value.stdout.strip()).ip)
        except ValueError:
            return None

    def apply_acl(self, config: ProjectConfig) -> AclPolicy:
        network = self.default_network()
        gateway = self.bridge_gateway(network)
        if config.ports and gateway is None:
            raise SandboxshError(
                "cannot determine the Incus bridge gateway; refusing an unscoped ingress rule"
            )
        policy = build_acl_policy(config, bridge_gateway=gateway)
        # incus-user owns its bridge in the default network project, so its
        # restricted certificate can attach ACLs but cannot manage them. Keep
        # this privileged surface limited to fixed ACL CRUD operations.
        acl = self._host_acl_name(config)
        created = self._admin_command(
            "--project",
            "default",
            "network",
            "acl",
            "create",
            acl,
            check=False,
        )
        if created.returncode and not self._already_exists(created):
            raise self._probe_error(f"create network ACL {acl}", created)
        self._admin_acl_query("PUT", acl, data=policy.document)
        return policy

    def pin_allowlist(self, policy: AclPolicy, *, instance: str) -> None:
        """Point the guest at the exact addresses the host wrote into the ACL.

        The ACL is a snapshot of what the *host* resolved. A guest that resolves
        the same name itself gets a different subset from any rotating CDN, dials
        an address the ACL never allowed, and hangs until its connect timeout.
        This is availability, not enforcement: guest root can edit /etc/hosts, but
        the ACL still decides what may leave the NIC.
        """
        entries = []
        for host, addresses in sorted(policy.resolutions.items()):
            # Literal IP/CIDR endpoints are already what the guest dials.
            usable = [address for address in addresses if "/" not in address and address != host]
            # A name found in /etc/hosts answers both families from that entry, so
            # keeping IPv4 also stops the guest from stalling on CDN v6 edges when
            # the bridge has no working IPv6 egress.
            preferred = [address for address in usable if ":" not in address] or usable
            entries.extend(f"{address} {host}" for address in preferred)
        document = "\n".join([PIN_BEGIN, *entries, PIN_END]) + "\n"
        script = (
            f"set -e; sed -i '/{PIN_BEGIN}/,/{PIN_END}/d' /etc/hosts; "
            'printf \'%s\' "$1" >> /etc/hosts'
        )
        self.command("exec", instance, "--", "bash", "-c", script, "sandboxsh", document)

    def unpin_allowlist(self, instance: str) -> None:
        self.command(
            "exec",
            instance,
            "--",
            "sed",
            "-i",
            f"/{PIN_BEGIN}/,/{PIN_END}/d",
            "/etc/hosts",
        )

    def attach_acl(self, config: ProjectConfig, *, instance: str | None = None) -> None:
        target = instance or config.instance_name
        settings = {
            "security.acls": self._host_acl_name(config),
            "security.acls.default.ingress.action": "reject",
            "security.acls.default.egress.action": "reject",
            "security.acls.default.ingress.logged": "true",
            "security.acls.default.egress.logged": "true",
            "security.ipv4_filtering": "true",
            "security.ipv6_filtering": "true",
        }
        override = self.command(
            "config",
            "device",
            "override",
            target,
            "eth0",
            *(f"{key}={value}" for key, value in settings.items()),
            check=False,
        )
        if override.returncode == 0:
            return
        # The inherited device is already overridden on subsequent starts. Update
        # each property in place so `sandboxsh up` and firewall refresh are idempotent.
        for key, value in settings.items():
            self.command("config", "device", "set", target, "eth0", key, value)

    def delete_acl(self, config: ProjectConfig) -> None:
        acl = self._host_acl_name(config)
        deleted = self._admin_acl_query("DELETE", acl, check=False)
        if deleted.returncode and not self._not_found(deleted):
            raise self._probe_error(f"delete network ACL {acl}", deleted)

    def create_instance(self, config: ProjectConfig) -> AclPolicy:
        if not self.image_exists(config.image):
            raise SandboxshError(
                f"base image {config.image!r} is missing; run `sandboxsh image build`"
            )
        self.command(
            "init",
            config.image,
            config.instance_name,
            "--vm",
            "--config",
            f"limits.cpu={config.resources.cpus}",
            "--config",
            f"limits.memory={config.resources.memory}",
            "--config",
            "security.secureboot=true",
            "--config",
            f"user.sandboxsh.config={config.path}",
            "--config",
            f"user.sandboxsh.name={config.name}",
            "--config",
            f"user.sandboxsh.immutable={config.immutable_fingerprint}",
            "--device",
            f"root,size={config.resources.disk}",
        )

        try:
            for index, mount in enumerate(config.mounts):
                args = [
                    "config",
                    "device",
                    "add",
                    config.instance_name,
                    f"workspace-{index}",
                    "disk",
                    f"source={mount.source}",
                    f"path={mount.target}",
                ]
                if mount.readonly:
                    args.append("readonly=true")
                self.command(*args)

            if config.agent_credentials:
                self.ensure_credentials_volume()
                self.command(
                    "config",
                    "device",
                    "add",
                    config.instance_name,
                    "agent-creds",
                    "disk",
                    f"pool={DEFAULT_POOL}",
                    f"source={CREDS_VOLUME}",
                    "path=/agent-creds",
                )

            policy = self.apply_acl(config)
            self.attach_acl(config)
            self.command("start", config.instance_name)
            self.wait_for_agent(config.instance_name)
            self.command(
                "exec",
                config.instance_name,
                "--",
                "cloud-init",
                "status",
                "--wait",
                check=False,
            )
            # After cloud-init, which rewrites /etc/hosts on first boot.
            self.pin_allowlist(policy, instance=config.instance_name)
            if config.agent_credentials:
                self.wait_for_mount(config.instance_name, "/agent-creds")
            self.command(
                "exec",
                config.instance_name,
                "--",
                "/usr/local/sbin/sandboxsh-instance-init",
                str(os.getuid()),
                str(os.getgid()),
            )
            self._configure_git(config.instance_name)
            self._verify_guest(config.instance_name)
            return policy
        except Exception as error:
            self.command("delete", config.instance_name, "--force", check=False)
            try:
                self.delete_acl(config)
            except Exception as cleanup_error:
                error.add_note(f"ACL cleanup also failed: {cleanup_error}")
            raise

    def _configure_git(self, instance: str) -> None:
        for key in ("user.name", "user.email", "init.defaultBranch", "pull.rebase"):
            result = self.runner.run(["git", "config", "--global", "--get", key], check=False)
            value = result.stdout.strip()
            if not value:
                continue
            self.command(
                "exec",
                instance,
                "--",
                "runuser",
                "-u",
                "dev",
                "--",
                "git",
                "config",
                "--global",
                key,
                value,
            )

    def _verify_guest(self, instance: str) -> None:
        result = self.command(
            "exec",
            instance,
            "--",
            "runuser",
            "-u",
            "dev",
            "--",
            "docker",
            "compose",
            "version",
            check=False,
        )
        if result.returncode:
            raise SandboxshError("Docker Compose verification failed in the VM")
        socket_check = self.command(
            "exec",
            instance,
            "--",
            "test",
            "!",
            "-S",
            "/var/run/incus/unix.socket",
            check=False,
        )
        if socket_check.returncode:
            raise SandboxshError("host Incus socket is unexpectedly visible in the VM")

    def start(self, config: ProjectConfig) -> None:
        status = self.instance_status(config.instance_name)
        if status is None:
            raise SandboxshError(f"instance disappeared before start: {config.instance_name}")
        if status.lower() != "running":
            self.command("start", config.instance_name)
            self.wait_for_agent(config.instance_name)

    def stop(self, config: ProjectConfig) -> None:
        status = self.instance_status(config.instance_name)
        if status is None or status.lower() == "stopped":
            return
        self.command("stop", config.instance_name, "--force")

    def destroy(self, config: ProjectConfig) -> None:
        if self.exists(config.instance_name):
            self.command("delete", config.instance_name, "--force")
            if self.exists(config.instance_name):
                raise SandboxshError(f"Incus did not delete {config.instance_name}")
        self.delete_acl(config)

    def guest_ip(self, config: ProjectConfig) -> str | None:
        record = self._instance_record(config.instance_name)
        if record is None:
            return None
        try:
            interface = record["state"]["network"]["eth0"]
        except (KeyError, TypeError):
            return None
        for address in interface.get("addresses", []):
            if address.get("family") == "inet" and address.get("scope") == "global":
                return address.get("address")
        return None

    def shell_argv(self, config: ProjectConfig) -> list[str]:
        workdir = shlex.quote(config.workdir)
        return [
            "incus",
            "--force-local",
            "--project",
            self.project,
            "exec",
            config.instance_name,
            "--",
            "runuser",
            "-u",
            "dev",
            "--",
            "bash",
            "-lc",
            f"cd {workdir} && exec bash -l",
        ]

    def exec_argv(self, config: ProjectConfig, command: tuple[str, ...]) -> list[str]:
        if not command:
            raise SandboxshError("exec requires a command after --")
        script = 'cd "$1"; shift; exec "$@"'
        return [
            "incus",
            "--force-local",
            "--project",
            self.project,
            "exec",
            config.instance_name,
            "--",
            "runuser",
            "-u",
            "dev",
            "--",
            "bash",
            "-lc",
            script,
            "sandboxsh",
            config.workdir,
            *command,
        ]

    def build_image(
        self,
        *,
        image: str = "sandboxsh/base",
        source: str = "images:debian/13/cloud",
    ) -> AclPolicy:
        builder = f"sandboxsh-image-builder-{os.getuid()}"
        extra_hosts = tuple(
            FirewallEntry(host.strip())
            for host in os.environ.get("SANDBOXSH_BUILD_ALLOW", "").split(",")
            if host.strip()
        )
        build_config = ProjectConfig(
            path=Path.home() / ".config/sandboxsh/image-builder.json",
            name="image-builder",
            workdir="/root",
            mounts=(),
            ports=(),
            firewall_enabled=True,
            firewall_allow=(
                FirewallEntry("claude.ai"),
                FirewallEntry("pi.dev"),
                FirewallEntry("storage.googleapis.com"),
                *extra_hosts,
            ),
            resources=Resources(cpus=4, memory="8GiB", disk="30GiB"),
            image=source,
            agent_credentials=False,
        )
        self.command("delete", builder, "--force", check=False)
        self.command(
            "init",
            source,
            builder,
            "--vm",
            "--config",
            "limits.cpu=4",
            "--config",
            "limits.memory=8GiB",
            "--device",
            "root,size=30GiB",
        )
        failure: Exception | None = None
        policy: AclPolicy | None = None
        try:
            # Supply-chain scripts run only after the same host-enforced ACL used
            # for project VMs is attached to the stopped builder.
            policy = self.apply_acl(build_config)
            self.attach_acl(build_config, instance=builder)
            self.command("start", builder)
            self.wait_for_agent(builder, timeout=600)
            self.command("exec", builder, "--", "cloud-init", "status", "--wait", check=False)
            self.pin_allowlist(policy, instance=builder)
            guest_dir = Path(__file__).parent / "guest"
            if not guest_dir.is_dir():
                # Editable source install; wheel installs use the packaged path.
                guest_dir = Path(__file__).parents[2] / "guest"
            for filename in ("provision.sh", "agent-init.sh", "instance-init.sh"):
                source_path = guest_dir / filename
                if not source_path.is_file():
                    raise SandboxshError(f"packaged guest script is missing: {source_path}")
                self.command("file", "push", str(source_path), f"{builder}/root/{filename}")
            # Provisioning is the long, supply-chain-facing step. Stream it so a
            # blocked endpoint is visible as the URL that failed, not just the
            # last line of a captured buffer.
            self.command(
                "exec",
                builder,
                "--",
                "bash",
                "/root/provision.sh",
                str(os.getuid()),
                str(os.getgid()),
                capture=False,
            )
            # The pins belong to this build, not to every VM cloned from the image.
            self.unpin_allowlist(builder)
            self.command("exec", builder, "--", "cloud-init", "clean", "--logs", "--machine-id")
            self.command("stop", builder, "--force")
            self.command("publish", builder, "--alias", image, "--reuse")
        except Exception as error:
            failure = error
            raise
        finally:
            # An endpoint the host could not resolve is omitted from the ACL and
            # the pins, which the guest only ever sees as a connect timeout. The
            # success path reports it; the failure path has to as well.
            if failure is not None:
                remedy = self.blocked_forwarding_remedy(self.default_network())
                if remedy is not None:
                    failure.add_note(remedy)
            if failure is not None and policy is not None and policy.unresolved_defaults:
                failure.add_note(
                    "built-in endpoints omitted from the ACL because the host could "
                    "not resolve them: " + ", ".join(policy.unresolved_defaults)
                )
            # A failed supply-chain fetch is only diagnosable from inside the
            # builder, under the ACL that blocked it, so allow keeping both.
            if failure is not None and os.environ.get("SANDBOXSH_KEEP_BUILDER") == "1":
                failure.add_note(
                    f"kept builder {builder} and its ACL for inspection; "
                    f"enter it with `incus --project {self.project} exec {builder} -- bash`, "
                    f"then remove it with `incus --project {self.project} delete {builder} "
                    "--force` and rerun `sandboxsh image build`"
                )
            else:
                self.command("delete", builder, "--force", check=False)
                try:
                    self.delete_acl(build_config)
                except Exception as cleanup_error:
                    if failure is None:
                        raise
                    failure.add_note(f"ACL cleanup also failed: {cleanup_error}")
        return policy
