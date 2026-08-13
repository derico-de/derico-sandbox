from __future__ import annotations

import json
import os
import shutil
import sys
from functools import wraps
from pathlib import Path

import click

from . import __version__
from .config import CONFIG_NAME, ProjectConfig, find_config, load_config, sanitize_name
from .errors import SandboxshError
from .incus import CREDS_VOLUME, DEFAULT_POOL, Incus
from .process import Runner
from .publish import INSTALL_HELPER_HINT, Endpoint, Publisher
from .security import (
    AclPolicy,
    approved_publications,
    build_acl_policy,
    claim_host_ports,
    ensure_project_approvals,
    release_host_ports,
    revoke_project_approvals,
)


def handled(function):
    @wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except SandboxshError as exc:
            raise click.ClickException(str(exc)) from exc

    return wrapper


class Context:
    def __init__(self, config_path: Path | None) -> None:
        self.config_path = config_path
        self.runner = Runner()
        self.incus = Incus(self.runner)
        self.publisher = Publisher(self.runner)

    def config(self) -> ProjectConfig:
        return load_config(self.config_path or find_config())


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    no_args_is_help=False,
)
@click.option(
    "--config",
    "config_path",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help=f"Use a specific {CONFIG_NAME}.",
)
@click.version_option(__version__)
@click.pass_context
def cli(ctx: click.Context, config_path: Path | None) -> None:
    """Persistent Incus VM sandboxes for autonomous development agents."""
    ctx.obj = Context(config_path)
    if ctx.invoked_subcommand is None:
        try:
            _up(ctx.obj, enter_shell=True)
        except SandboxshError as exc:
            raise click.ClickException(str(exc)) from exc


@cli.command("update")
@click.option("--ref", help="Git tag, branch, or commit to install (defaults to main).")
@click.pass_obj
@handled
def update_command(context: Context, ref: str | None) -> None:
    """Update sandboxsh directly from its GitHub repository."""
    if shutil.which("pipx") is None:
        raise SandboxshError("pipx is required to update sandboxsh")
    repository = os.environ.get(
        "SANDBOXSH_REPOSITORY_URL",
        "https://github.com/derico-de/derico-sandbox.git",
    )
    install_ref = ref or os.environ.get("SANDBOXSH_INSTALL_REF", "main")
    source_override = os.environ.get("SANDBOXSH_INSTALL_SOURCE")
    if source_override and ref is None:
        source = source_override
    else:
        source = f"git+{repository}@{install_ref}"
    click.echo(f"Updating sandboxsh from {source}...")
    context.runner.run(["pipx", "install", "--force", source], capture=False)
    click.echo(f"Updated sandboxsh from {source}.")


@cli.command("init")
@click.option("--name", help="Project/VM name (defaults to the directory name).")
@click.option("--no-up", is_flag=True, help="Write configuration without starting the VM.")
@click.pass_obj
@handled
def init_command(context: Context, name: str | None, no_up: bool) -> None:
    """Create .sandboxsh.json in the current project."""
    root = Path.cwd().resolve()
    path = root / CONFIG_NAME
    if path.exists() and not click.confirm(f"Overwrite {path}?", default=False):
        raise SandboxshError("existing configuration kept")
    project_name = sanitize_name(name or root.name)
    document = {
        "name": project_name,
        "workdir": f"/workspaces/{root.name}",
        "dirs": ["."],
        "ports": [],
        "tailscale": {"enabled": True},
        "firewall": {"enabled": True, "allow": []},
        "resources": {"cpus": 4, "memory": "8GiB", "disk": "40GiB"},
        "agent_credentials": True,
    }
    path.write_text(json.dumps(document, indent=2) + "\n")
    click.echo(f"Wrote {path}")
    if not no_up:
        context.config_path = path
        _up(context, enter_shell=True)


@cli.command("approve")
@click.option("--revoke", is_flag=True, help="Revoke all external mount/network approvals.")
@click.pass_obj
@handled
def approve_command(context: Context, revoke: bool) -> None:
    """Review authority requested by project-controlled configuration."""
    config = context.config()
    if revoke:
        revoke_project_approvals(config)
        click.echo(f"Revoked approvals for {config.path}")
        return
    ensure_project_approvals(config, prompt=True)
    click.echo("Requested mounts and network endpoints are approved.")


def _verify_and_approve(context: Context, config: ProjectConfig) -> None:
    context.incus.verify_host_access()
    ensure_project_approvals(config, prompt=True)


def _report_unresolved_defaults(policy: AclPolicy) -> None:
    if policy.unresolved_defaults:
        click.echo(
            "Skipped unavailable built-in endpoint(s): " + ", ".join(policy.unresolved_defaults),
            err=True,
        )


def _interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def _publish(context: Context, config: ProjectConfig) -> tuple[Endpoint, ...]:
    """Bring published tailnet ports in line with the approved configuration."""
    requested = config.publishable
    if not requested:
        context.publisher.clear(config)
        release_host_ports(config)
        return ()
    if not context.publisher.helper_available():
        click.echo(INSTALL_HELPER_HINT, err=True)
        return ()

    approved, pending = approved_publications(config, prompt=_interactive())
    for mapping in pending:
        click.echo(
            f"Not published: guest port {mapping.guest} has no host approval for "
            f"tailnet port {mapping.host}. Run `sandboxsh approve` from an "
            "interactive host terminal to allow it.",
            err=True,
        )
    if not approved:
        context.publisher.clear(config)
        release_host_ports(config)
        return ()

    claim_host_ports(config, approved)
    guest_ip = context.incus.guest_ip(config)
    if not guest_ip:
        raise SandboxshError("VM has no IPv4 address, so its ports cannot be published")
    return context.publisher.sync(
        config,
        guest_ip=guest_ip,
        address=context.publisher.address(config),
        mappings=approved,
    )


def _publish_and_report(context: Context, config: ProjectConfig) -> None:
    """Publish during a lifecycle command, where a running VM outranks a URL.

    Publishing is a convenience layered on top of the sandbox. A tailnet that is
    down, an unapproved port, or a host port another project already owns must
    not leave the caller without the VM they asked for.
    """
    try:
        endpoints = _publish(context, config)
    except SandboxshError as exc:
        click.echo(f"Ports were not published: {exc}", err=True)
        return
    for endpoint in endpoints:
        click.echo(f"published: {endpoint.url} -> guest port {endpoint.mapping.guest}", err=True)


def _withdraw(context: Context, config: ProjectConfig) -> None:
    """Drop published listeners without letting that block stop/delete."""
    try:
        context.publisher.clear(config)
    except SandboxshError as exc:
        click.echo(f"Published ports were not withdrawn: {exc}", err=True)


def _up(context: Context, *, enter_shell: bool) -> None:
    config = context.config()
    _verify_and_approve(context, config)
    if context.incus.exists(config.instance_name):
        context.incus.assert_immutable_config(config)
        attached, removed = context.incus.sync_mounts(config)
        # Tighten changed authority before a persistent guest's startup services
        # can run under stale ACL rules.
        policy = context.incus.apply_acl(config)
        _report_unresolved_defaults(policy)
        context.incus.attach_acl(config)
        context.incus.start(config)
        context.incus.pin_allowlist(policy, instance=config.instance_name)
        for target in attached:
            context.incus.wait_for_mount(config.instance_name, target)
            click.echo(f"mounted: {target}", err=True)
        for target in removed:
            click.echo(f"unmounted: {target}", err=True)
        click.echo(f"Attached to persistent VM {config.instance_name}.", err=True)
    else:
        click.echo(f"Creating persistent VM {config.instance_name}...", err=True)
        policy = context.incus.create_instance(config)
        _report_unresolved_defaults(policy)
        click.echo(
            f"Created VM with {len(policy.document['egress'])} host-enforced egress rules.",
            err=True,
        )
    # After start, because the proxy targets the address the VM just leased.
    _publish_and_report(context, config)
    if enter_shell:
        context.runner.exec(context.incus.shell_argv(config), env=context.incus.environment)


@cli.command("up")
@click.option("--no-shell", is_flag=True, help="Start/create without entering a shell.")
@click.pass_obj
@handled
def up_command(context: Context, no_shell: bool) -> None:
    """Create or start the persistent VM, then enter it."""
    _up(context, enter_shell=not no_shell)


@cli.command("shell")
@click.pass_obj
@handled
def shell_command(context: Context) -> None:
    """Enter the running project VM as the dev user."""
    config = context.config()
    context.incus.verify_host_access()
    if context.incus.instance_status(config.instance_name) != "Running":
        raise SandboxshError("VM is not running; use `sandboxsh up`")
    context.runner.exec(context.incus.shell_argv(config), env=context.incus.environment)


@cli.command("exec", context_settings={"ignore_unknown_options": True})
@click.argument("command", nargs=-1, type=click.UNPROCESSED, required=True)
@click.pass_obj
@handled
def exec_command(context: Context, command: tuple[str, ...]) -> None:
    """Run a command inside the VM, e.g. sandboxsh exec -- docker compose ps."""
    config = context.config()
    context.incus.verify_host_access()
    if context.incus.instance_status(config.instance_name) != "Running":
        raise SandboxshError("VM is not running; use `sandboxsh up`")
    context.runner.exec(
        context.incus.exec_argv(config, command),
        env=context.incus.exec_environment(command),
    )


@cli.command("agent", context_settings={"ignore_unknown_options": True})
@click.argument("agent", type=click.Choice(("claude", "pi")))
@click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
@click.pass_obj
@handled
def agent_command(context: Context, agent: str, arguments: tuple[str, ...]) -> None:
    """Create/start the VM and run Claude or Pi with Herdr status detection."""
    _up(context, enter_shell=False)
    config = context.config()
    command = (agent, *arguments)
    context.runner.exec(
        context.incus.exec_argv(config, command),
        env=context.incus.exec_environment(command),
    )


@cli.command("down")
@click.pass_obj
@handled
def down_command(context: Context) -> None:
    """Stop the VM but preserve its disk and project-local credentials."""
    config = context.config()
    context.incus.verify_host_access()
    # Withdraw the listeners first: a published port whose VM is gone accepts
    # connections and then hangs. The host port stays reserved for this project.
    _withdraw(context, config)
    context.incus.stop(config)
    click.echo(f"Stopped {config.instance_name}; its disk is preserved.")


@cli.command("destroy")
@click.option("-y", "--yes", is_flag=True, help="Do not prompt.")
@click.pass_obj
@handled
def destroy_command(context: Context, yes: bool) -> None:
    """Delete this VM and its project-local state; shared agent credentials remain."""
    config = context.config()
    context.incus.verify_host_access()
    if not yes and not click.confirm(
        f"Delete {config.instance_name} and all VM-local credentials/state?", default=False
    ):
        raise SandboxshError("destroy cancelled")
    _withdraw(context, config)
    release_host_ports(config)
    context.incus.destroy(config)
    click.echo(f"Deleted {config.instance_name}. Shared agent credentials were kept.")


@cli.command("recreate")
@click.option("-y", "--yes", is_flag=True, help="Do not prompt.")
@click.option("--no-shell", is_flag=True, help="Recreate without entering a shell.")
@click.pass_obj
@handled
def recreate_command(context: Context, yes: bool, no_shell: bool) -> None:
    """Replace the VM from the golden image, preserving host files/shared agent creds."""
    config = context.config()
    _verify_and_approve(context, config)
    if context.incus.exists(config.instance_name):
        if not yes and not click.confirm(
            "Delete the VM-local disk and project credentials, then recreate?", default=False
        ):
            raise SandboxshError("recreate cancelled")
        context.incus.destroy(config)
    _up(context, enter_shell=not no_shell)


@cli.command("status")
@click.pass_obj
@handled
def status_command(context: Context) -> None:
    """Show instance state, resources, and reachable development URLs."""
    config = context.config()
    context.incus.verify_host_access()
    status = context.incus.instance_status(config.instance_name)
    click.echo(f"config:   {config.path}")
    click.echo(f"instance: {config.instance_name}")
    click.echo(f"status:   {status or 'absent'}")
    click.echo(
        f"limits:   {config.resources.cpus} CPU, {config.resources.memory} RAM, "
        f"{config.resources.disk} disk"
    )
    if status == "Running":
        ip = context.incus.guest_ip(config)
        click.echo(f"address:  {ip or 'not assigned'}")
        if ip:
            for port in config.guest_ports:
                click.echo(f"url:      http://{ip}:{port}")
        for line in _publication_status(context, config):
            click.echo(line)


def _publication_status(context: Context, config: ProjectConfig) -> list[str]:
    """Describe tailnet publication without making `status` depend on it."""
    if not config.publishable:
        return []
    if not context.publisher.helper_available():
        return ["tailnet:  helper not installed (see `sandboxsh doctor`)"]
    approved, pending = approved_publications(config, prompt=False)
    lines = []
    try:
        address = context.publisher.address(config)
        hostname = context.publisher.hostname()
    except SandboxshError as exc:
        return [f"tailnet:  unavailable ({exc})"]
    authority = hostname or address
    for mapping in approved:
        lines.append(f"tailnet:  http://{authority}:{mapping.host} -> guest {mapping.guest}")
    for mapping in pending:
        lines.append(f"tailnet:  guest {mapping.guest} awaiting `sandboxsh approve`")
    return lines


@cli.command("url")
@click.argument("port", type=click.IntRange(1, 65535))
@click.option("--vm", is_flag=True, help="Print the VM address even when the port is published.")
@click.pass_obj
@handled
def url_command(context: Context, port: int, vm: bool) -> None:
    """Print the reachable URL for a declared guest port."""
    config = context.config()
    context.incus.verify_host_access()
    if port not in config.guest_ports:
        raise SandboxshError(f"port {port} is not declared in .sandboxsh.json")
    if not vm and config.publishable and context.publisher.helper_available():
        approved, _ = approved_publications(config, prompt=False)
        mapping = next((entry for entry in approved if entry.guest == port), None)
        if mapping is not None:
            authority = context.publisher.hostname() or context.publisher.address(config)
            click.echo(f"http://{authority}:{mapping.host}")
            return
    address = context.incus.guest_ip(config)
    if not address:
        raise SandboxshError("VM has no IPv4 address")
    click.echo(f"http://{address}:{port}")


@cli.command("publish")
@click.pass_obj
@handled
def publish_command(context: Context) -> None:
    """Publish approved development ports on this host's tailnet address."""
    config = context.config()
    context.incus.verify_host_access()
    if context.incus.instance_status(config.instance_name) != "Running":
        raise SandboxshError("VM is not running; use `sandboxsh up`")
    endpoints = _publish(context, config)
    if not endpoints:
        click.echo("Nothing is published for this project.")
        return
    for endpoint in endpoints:
        click.echo(f"{endpoint.url} -> guest port {endpoint.mapping.guest}")


@cli.command("unpublish")
@click.pass_obj
@handled
def unpublish_command(context: Context) -> None:
    """Withdraw this project's tailnet listeners and release its host ports."""
    config = context.config()
    if not context.publisher.helper_available():
        raise SandboxshError(INSTALL_HELPER_HINT)
    context.publisher.clear(config)
    release_host_ports(config)
    click.echo(f"Withdrew published ports for {config.instance_name}.")


@cli.command("refresh-firewall")
@click.pass_obj
@handled
def refresh_firewall_command(context: Context) -> None:
    """Re-resolve allowed names and atomically replace the Incus network ACL."""
    config = context.config()
    _verify_and_approve(context, config)
    policy = context.incus.apply_acl(config)
    _report_unresolved_defaults(policy)
    context.incus.attach_acl(config)
    # A running guest keeps dialing the previous snapshot until it is re-pinned.
    if context.incus.instance_status(config.instance_name) == "Running":
        context.incus.pin_allowlist(policy, instance=config.instance_name)
        # Declared ports are refreshed here too, so the listeners follow them.
        _publish_and_report(context, config)
    for host, addresses in sorted(policy.resolutions.items()):
        click.echo(f"{host}: {', '.join(addresses)}")
    click.echo("Host-enforced network ACL refreshed.")


@cli.command("plan")
@click.pass_obj
@handled
def plan_command(context: Context) -> None:
    """Render the effective mounts, limits, and resolved network policy."""
    config = context.config()
    policy = build_acl_policy(config, bridge_gateway="<resolved-at-runtime>")
    document = {
        "config": str(config.path),
        "instance": config.instance_name,
        "image": config.image,
        "persistent": True,
        "resources": {
            "cpus": config.resources.cpus,
            "memory": config.resources.memory,
            "disk": config.resources.disk,
        },
        "mounts": [
            {
                "source": str(mount.source),
                "target": mount.target,
                "readonly": mount.readonly,
                "requiresHostApproval": not mount.inside_project,
            }
            for mount in config.mounts
        ],
        "ports": [
            {
                "guest": mapping.guest,
                "host": mapping.host,
                "tailnet": mapping.tailnet and config.tailscale.enabled,
            }
            for mapping in config.ports
        ],
        "tailnetPublicationRequiresHostApproval": bool(config.publishable),
        "sharedAgentCredentials": config.agent_credentials,
        "guestLocalOtherCredentials": True,
        "customNetworkAuthorityRequiresHostApproval": bool(config.firewall_allow),
        "unresolvedBuiltInEndpoints": list(policy.unresolved_defaults),
        "networkAcl": policy.document,
    }
    click.echo(json.dumps(document, indent=2))


@cli.group("image")
def image_group() -> None:
    """Manage the reusable golden VM image."""


@image_group.command("build")
@click.option("--alias", "image", default="sandboxsh/base", show_default=True)
@click.option("--source", default="images:debian/13/cloud", show_default=True)
@click.pass_obj
@handled
def image_build_command(context: Context, image: str, source: str) -> None:
    """Build and publish the reusable Docker/agent-enabled VM image."""
    context.incus.verify_host_access()
    click.echo(f"Building {image} from {source}; this can take several minutes.")
    policy = context.incus.build_image(image=image, source=source)
    _report_unresolved_defaults(policy)
    click.echo(f"Published {image}.")


@image_group.command("status")
@click.option("--alias", "image", default="sandboxsh/base", show_default=True)
@click.pass_obj
@handled
def image_status_command(context: Context, image: str) -> None:
    """Check whether the reusable image exists."""
    context.incus.verify_host_access()
    if not context.incus.image_exists(image):
        raise SandboxshError(f"image {image!r} is absent")
    click.echo(f"image {image!r} is available")


@cli.group("credentials")
def credentials_group() -> None:
    """Manage the Incus-managed shared agent credential volume."""


@credentials_group.command("reset")
@click.option("-y", "--yes", is_flag=True, help="Do not prompt.")
@click.pass_obj
@handled
def credentials_reset_command(context: Context, yes: bool) -> None:
    """Delete all shared Claude/pi/Vibe credentials after VMs are destroyed."""
    context.incus.verify_host_access()
    if not context.incus.volume_exists(CREDS_VOLUME):
        click.echo("Shared agent credential volume is already absent.")
        return
    if not yes and not click.confirm(
        "Delete shared agent logins, settings, sessions, and packages?", default=False
    ):
        raise SandboxshError("credential reset cancelled")
    result = context.incus.command(
        "storage", "volume", "delete", DEFAULT_POOL, CREDS_VOLUME, check=False
    )
    if result.returncode:
        detail = result.stderr.strip() or "Incus rejected the deletion"
        raise SandboxshError(
            "cannot delete the credential volume (it may still be attached). "
            f"Destroy every sandbox using it, then retry. Incus said: {detail}"
        )
    click.echo("Shared agent credential volume deleted.")


@cli.command("doctor")
@click.pass_obj
@handled
def doctor_command(context: Context) -> None:
    """Validate host dependencies and the restricted Incus trust boundary."""
    failures: list[str] = []
    for command in ("incus", "git", "sudo"):
        if shutil.which(command):
            click.echo(f"PASS {command} is installed")
        else:
            click.echo(f"FAIL {command} is missing")
            failures.append(command)
    if Path("/dev/kvm").exists():
        click.echo("PASS /dev/kvm is available")
    else:
        click.echo("FAIL /dev/kvm is unavailable")
        failures.append("kvm")
    if Path("/sys/module/br_netfilter").exists():
        click.echo("PASS br_netfilter kernel module is loaded")
    else:
        click.echo("FAIL br_netfilter kernel module is not loaded")
        failures.append("br_netfilter")
    try:
        project = context.incus.verify_host_access()
        click.echo(f"PASS restricted Incus user project: {project}")
    except SandboxshError as exc:
        click.echo(f"FAIL {exc}")
        failures.append("incus-access")
    else:
        try:
            network = context.incus.default_network()
            remedy = context.incus.blocked_forwarding_remedy(network)
        except SandboxshError as exc:
            click.echo(f"FAIL {exc}")
            failures.append("bridge-forwarding")
        else:
            if remedy is None:
                click.echo(f"PASS host forwards traffic from bridge {network}")
            else:
                click.echo(f"FAIL {remedy}")
                failures.append("bridge-forwarding")
    # Tailnet publishing is optional, so its absence is reported without
    # failing a host that only ever reaches sandboxes at their VM address.
    if not context.publisher.helper_available():
        click.echo(f"WARN {INSTALL_HELPER_HINT}")
    elif shutil.which("tailscale") is None:
        click.echo(
            "WARN the publishing helper is installed but tailscale is not; "
            'publish only with an explicit "tailscale": {"address": "..."}'
        )
    else:
        status = context.runner.run(["tailscale", "status", "--json"], check=False)
        if status.returncode:
            click.echo("WARN tailscale is installed but not connected; run `tailscale up`")
        else:
            click.echo("PASS tailnet publishing is available")
    if failures:
        raise SandboxshError(f"doctor found {len(failures)} problem(s)")
    click.echo("Host is ready for sandboxsh.")


if __name__ == "__main__":
    cli()
