from __future__ import annotations

import grp
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
        created = self.command(
            "network", "acl", "create", config.acl_name, check=False
        )
        if created.returncode and not self._already_exists(created):
            raise self._probe_error(f"create network ACL {config.acl_name}", created)
        # `incus network acl edit` can lose the selected project when it sends
        # its update, causing restricted users to be checked against `default`.
        # Scope the API request explicitly so the update stays in the per-user
        # project selected by the restricted incus-user socket.
        endpoint = (
            f"/1.0/network-acls/{quote(config.acl_name, safe='')}?"
            + urlencode({"project": self.project})
        )
        # Incus rejects the global --project option for raw queries, so this
        # intentionally bypasses command() while preserving the forced local
        # socket, isolated client configuration, and explicit project in the URL.
        self.runner.run(
            [
                "incus",
                "--force-local",
                "query",
                "-X",
                "PUT",
                "-d",
                json.dumps(policy.document),
                endpoint,
            ],
            env=self.environment,
        )
        return policy

    def attach_acl(self, config: ProjectConfig, *, instance: str | None = None) -> None:
        target = instance or config.instance_name
        settings = {
            "security.acls": config.acl_name,
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
        deleted = self.command(
            "network", "acl", "delete", config.acl_name, check=False
        )
        if deleted.returncode and not self._not_found(deleted):
            raise self._probe_error(f"delete network ACL {config.acl_name}", deleted)

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
        except Exception:
            self.command("delete", config.instance_name, "--force", check=False)
            self.delete_acl(config)
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
        try:
            # Supply-chain scripts run only after the same host-enforced ACL used
            # for project VMs is attached to the stopped builder.
            policy = self.apply_acl(build_config)
            self.attach_acl(build_config, instance=builder)
            self.command("start", builder)
            self.wait_for_agent(builder, timeout=600)
            self.command("exec", builder, "--", "cloud-init", "status", "--wait", check=False)
            guest_dir = Path(__file__).parent / "guest"
            if not guest_dir.is_dir():
                # Editable source install; wheel installs use the packaged path.
                guest_dir = Path(__file__).parents[2] / "guest"
            for filename in ("provision.sh", "agent-init.sh", "instance-init.sh"):
                source_path = guest_dir / filename
                if not source_path.is_file():
                    raise SandboxshError(f"packaged guest script is missing: {source_path}")
                self.command("file", "push", str(source_path), f"{builder}/root/{filename}")
            self.command(
                "exec",
                builder,
                "--",
                "bash",
                "/root/provision.sh",
                str(os.getuid()),
                str(os.getgid()),
            )
            self.command("exec", builder, "--", "cloud-init", "clean", "--logs", "--machine-id")
            self.command("stop", builder, "--force")
            self.command("publish", builder, "--alias", image, "--reuse")
        finally:
            self.command("delete", builder, "--force", check=False)
            self.delete_acl(build_config)
        return policy
