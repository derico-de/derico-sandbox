from __future__ import annotations

import json
import os
import shutil
from functools import wraps
from pathlib import Path

import click

from . import __version__
from .config import CONFIG_NAME, ProjectConfig, find_config, load_config, sanitize_name
from .errors import SandboxshError
from .incus import CREDS_VOLUME, DEFAULT_POOL, Incus
from .process import Runner
from .security import (
    AclPolicy,
    build_acl_policy,
    ensure_project_approvals,
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
            "Skipped unavailable built-in endpoint(s): "
            + ", ".join(policy.unresolved_defaults),
            err=True,
        )


def _up(context: Context, *, enter_shell: bool) -> None:
    config = context.config()
    _verify_and_approve(context, config)
    if context.incus.exists(config.instance_name):
        context.incus.assert_immutable_config(config)
        # Tighten changed authority before a persistent guest's startup services
        # can run under stale ACL rules.
        policy = context.incus.apply_acl(config)
        _report_unresolved_defaults(policy)
        context.incus.attach_acl(config)
        context.incus.start(config)
        click.echo(f"Attached to persistent VM {config.instance_name}.", err=True)
    else:
        click.echo(f"Creating persistent VM {config.instance_name}...", err=True)
        policy = context.incus.create_instance(config)
        _report_unresolved_defaults(policy)
        click.echo(
            f"Created VM with {len(policy.document['egress'])} host-enforced egress rules.",
            err=True,
        )
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
    context.runner.exec(context.incus.exec_argv(config, command), env=context.incus.environment)


@cli.command("down")
@click.pass_obj
@handled
def down_command(context: Context) -> None:
    """Stop the VM but preserve its disk and project-local credentials."""
    config = context.config()
    context.incus.verify_host_access()
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
            for port in config.ports:
                click.echo(f"url:      http://{ip}:{port}")


@cli.command("url")
@click.argument("port", type=click.IntRange(1, 65535))
@click.pass_obj
@handled
def url_command(context: Context, port: int) -> None:
    """Print the host-reachable URL for a declared guest port."""
    config = context.config()
    context.incus.verify_host_access()
    if port not in config.ports:
        raise SandboxshError(f"port {port} is not declared in .sandboxsh.json")
    address = context.incus.guest_ip(config)
    if not address:
        raise SandboxshError("VM has no IPv4 address")
    click.echo(f"http://{address}:{port}")


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
    try:
        project = context.incus.verify_host_access()
        click.echo(f"PASS restricted Incus user project: {project}")
    except SandboxshError as exc:
        click.echo(f"FAIL {exc}")
        failures.append("incus-access")
    if failures:
        raise SandboxshError(f"doctor found {len(failures)} problem(s)")
    click.echo("Host is ready for sandboxsh.")


if __name__ == "__main__":
    cli()
