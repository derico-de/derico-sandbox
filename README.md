# sandboxsh

`sandboxsh` creates one **persistent Incus virtual machine per project** for
running coding agents in YOLO/auto-approve mode. It keeps the small per-project
configuration and shared golden-image workflow of `devshell`, while moving the
trust boundary from a Docker container to a hardware-virtualized VM.

Docker Engine and `docker compose` run **inside the VM** against the guest
kernel. Neither the host Docker socket nor the Incus control socket is exposed.

## Security model

- The host user connects through Incus' restricted `incus-user` service and its
  automatically-created `user-<uid>` project. The user must be in `incus`, **not
  `incus-admin`**; `incus-admin` is host-root-equivalent.
- The project checkout is shared read-write using an Incus VM `disk` device
  (virtiofs or 9p, depending on host support). YOLO can intentionally modify or
  delete this checkout. Keep it under Git and commit/snapshot before risky work.
- Additional host mounts and project-added network endpoints require approval in
  `~/.config/sandboxsh/approvals.json`, outside the agent-writable checkout.
  Sensitive mounts such as `~/.ssh`, `~/.aws`, `~/.config/incus`, and host runtime
  sockets are rejected by default.
- Egress is enforced outside guest control with an Incus network ACL attached to
  the VM NIC. The ACL defaults to reject and permits only resolved built-in agent,
  source-control, package, and Docker-registry endpoints plus approved project
  additions. Guest root and Docker cannot disable this ACL.
- Only declared development ports accept host-to-VM traffic. They are reached at
  the VM's private address, avoiding host-port collisions between projects.
- CPU, memory, and root-disk limits are enforced by Incus.
- Claude, pi, and Vibe state is placed on one Incus-managed filesystem volume and
  shared across project VMs. This provides login-once behavior, but it deliberately
  means compromise of one project can read the shared agent credentials.
- SSH, cloud, application, and other credentials remain on each persistent VM's
  own disk unless you explicitly expose them.

See [SECURITY.md](SECURITY.md) for the threat model and residual risks.

## Install

Host requirements: Linux, KVM, systemd, current Incus, Python 3.11+, and `pipx`.
On a Debian/Ubuntu-style host, run the installer directly from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/derico-de/derico-sandbox/main/install.sh | bash
# Log out/in if it added you to the `incus` group.
sandboxsh doctor
sandboxsh image build       # one reusable image; takes several minutes
```

Alternatively, clone the repository and run `./install.sh`. Pass `--no-deps` to
the streamed installer with `bash -s -- --no-deps`. The installer uses the local
checkout when available and otherwise installs the Python package from GitHub.
Set `SANDBOXSH_INSTALL_REF` to pin that package install to a tag or commit.
Update an existing pipx installation later with:

```bash
sandboxsh update
```

Use `sandboxsh update --ref <tag-or-commit>` to install a specific revision.

The installer never adds you to `incus-admin`. A fresh Incus daemon is minimally
initialized, then `incus-user` creates the restricted `user-<uid>` project and a
per-user managed bridge when you first use it.

The default golden image is built from `images:debian/13/cloud`. Override it if
needed:

```bash
sandboxsh image build --source images:debian/12/cloud --alias sandboxsh/base
```

## First project

```bash
cd ~/work/acme
sandboxsh init --name acme
```

This writes `.sandboxsh.json`, creates the persistent VM, mounts the checkout,
and enters a shell as `dev`.

Later:

```bash
sandboxsh                   # same as `sandboxsh up`: create/start and enter
sandboxsh up                # explicit form
sandboxsh shell             # enter an already-running VM
sandboxsh exec -- docker compose up -d
sandboxsh status
sandboxsh down              # stop; preserve disk and VM-local credentials
sandboxsh recreate          # replace VM-local disk from golden image
sandboxsh destroy           # delete VM-local disk; keep shared agent credentials
```

Inside the VM:

```bash
docker compose version
docker compose up -d
claude
pi
vibe
```

## Configuration

```json
{
  "name": "acme",
  "workdir": "/workspaces/acme",
  "dirs": [
    ".",
    {"path": "../shared-reference", "ro": true, "target": "/reference"}
  ],
  "ports": [3000, 8080],
  "firewall": {
    "enabled": true,
    "allow": [
      "docs.example.com",
      {"host": "registry.example.com", "ports": [443, 5000]},
      {"host": "db.internal.example", "ports": [5432], "allow_private": true}
    ]
  },
  "resources": {
    "cpus": 4,
    "memory": "8GiB",
    "disk": "40GiB"
  },
  "agent_credentials": true,
  "image": "sandboxsh/base"
}
```

### Mounts

- `"."` is mandatory and read-write.
- Relative paths resolve from the directory containing `.sandboxsh.json`.
- A string is read-write; `{ "path": ..., "ro": true }` is read-only.
- Default guest targets are `/workspaces/<basename>`. An explicit target must be
  an absolute guest path.
- Sources outside the project require `sandboxsh approve`. Changing read-only to
  read-write requires a new approval.
- The restricted Incus user service allows host-path devices only below the host
  user's home directory by default.

### Network

The built-in allowlist covers agent APIs, GitHub, npm/PyPI, Debian/Node/Docker
package sources, and the Docker Hub registry. Project additions require approval:

```bash
sandboxsh approve
sandboxsh refresh-firewall
```

Hostnames are resolved on the **host** and their current IPv4/IPv6 addresses are
written into the ACL. Run `refresh-firewall` after CDN rotation. The golden-image
builder is protected by the same ACL; add an exceptional build-only hostname with
`SANDBOXSH_BUILD_ALLOW=host1,host2 sandboxsh image build`. Wildcards are
not accepted. Private/RFC1918 destinations are rejected unless the endpoint sets
`"allow_private": true`; that expanded authority is highlighted during approval.
Loopback, link-local/metadata, multicast, unspecified, and reserved destinations
are always rejected. A built-in endpoint that is temporarily unresolvable is
omitted from the ACL (fail-closed) and reported by the CLI; an unresolvable
project-added endpoint remains an error.

`firewall.enabled=false` is rejected unless the trusted host shell explicitly
sets `SANDBOXSH_ALLOW_OPEN_NETWORK=1`.

To open a development server, declare its guest port and bind the service to
`0.0.0.0` inside the VM:

```json
{"ports": [3000]}
```

```bash
sandboxsh refresh-firewall  # applies firewall and port changes live
sandboxsh url 3000
```

### Shared and project-local credentials

The custom Incus storage volume `sandboxsh-agent-creds` is attached at
`/agent-creds`. At startup, the following directories are linked into it:

- `~/.claude` → `/agent-creds/claude`
- `~/.pi` → `/agent-creds/pi`
- `~/.vibe` → `/agent-creds/vibe`

Everything else in `/home/dev` stays on that project's VM disk. Reset the shared
agent state only after destroying every VM that has it attached:

```bash
sandboxsh credentials reset
```

Set `"agent_credentials": false` for a project that must not see the shared
agent volume; its agent state then stays VM-local while the same YOLO permission
settings are applied.

Changes to mounts, image, resources, or `agent_credentials` are immutable for an
existing VM. `sandboxsh up` detects their fingerprint and requires
`sandboxsh recreate`. Firewall destinations and declared ports are updated by
`refresh-firewall` or the next `up`; `workdir` changes affect the next shell.

## Golden image contents

The build installs:

- Docker Engine, BuildKit, and Compose v2
- Git, curl, jq, ripgrep, fd, build-essential, PostgreSQL client, Vim/Neovim
- Node.js 24 and pnpm
- Python 3, uv, ruff, pytest, tox/tox-uv, and Invoke
- Claude Code, pi (plus subagents/Impeccable/sideshow), and Mistral Vibe
- a default git pre-push hook that requires `SANDBOXSH_ALLOW_PUSH=1`

Rebuild the alias after changing anything under `guest/`:

```bash
sandboxsh image build
sandboxsh recreate
```

## Validation

Local unit tests do not require Incus:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
pytest
ruff check .
```

Host integration checks:

```bash
sandboxsh doctor
sandboxsh image status
./e2e-test.sh
```

The opt-in E2E test creates a temporary persistent VM below the host home, runs a
real Compose workload, checks live host-project writes, verifies socket isolation,
tests allow/deny egress and declared ingress, then destroys the VM.

## Project layout

- `src/sandboxsh/` — Click CLI, configuration, approvals, ACL and Incus client
- `guest/` — golden-image and per-instance provisioning scripts
- `install.sh` — host bootstrap using the restricted Incus user service
- `docs/architecture.md` — design and control-plane details
- `tests/` — configuration, security-policy, and CLI tests
