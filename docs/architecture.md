# Architecture

## Components

```text
trusted host                                      untrusted persistent VM
──────────────────────────────────────────────    ─────────────────────────
sandboxsh (Python/Click)                          dev user + guest root
  │ restricted incus user socket                    │ Claude / pi / Vibe
  │                                                 │ Docker + Compose
  ├─ user-<uid> restricted project                  │
  ├─ VM lifecycle / limits                          ├─ guest root disk
  ├─ host-path disk: project ── virtiofs/9p ───────┤  /workspaces/project
  ├─ managed volume: agent creds ──────────────────┤  /agent-creds
  └─ NIC ACL (default reject) ─────────────────────┤  eth0
```

The host-side launcher is the policy decision point. The VM is the execution
point. A control that guest root can change is not counted as a security
boundary.

## Incus access model

Incus packages provide two local groups/sockets:

- `incus-admin` / `unix.socket`: full daemon control and effectively host root;
- `incus` / `user.socket`: the `incus-user` proxy, which creates a restricted
  project named `user-<uid>` and a per-user managed network.

Incus initially creates that project with `features.networks=false`, so its
per-user bridge is owned by the `default` network project and the restricted user
cannot manage the ACLs protecting it. During installation, `sandboxsh setup` uses
one narrowly-scoped, trusted `sudo` migration: it refuses a project containing
resources beyond its default profile, enables `features.networks`, and creates a
same-named managed bridge in
the user project. Routine operations never use administrator access.

Normal lifecycle subprocesses are invoked with
`incus --force-local --project user-<uid>` and an isolated `INCUS_CONF` whose
default is `local`, so a configured TLS remote or different current project
cannot redirect them. Raw ACL requests cannot accept the global project flag, so
they instead include the same project explicitly in the API URL. `sandboxsh`
verifies `restricted=true` and `features.networks=true`. Images, profiles, storage
volumes, networks, and ACLs are then project-local; host disk paths remain limited
to the user's home, and low-level VM settings and dangerous devices stay blocked.

The live workspace requirement is the reason host-path disks are allowed at all.
The launcher adds a second policy layer: project-internal paths are expected;
external paths need an out-of-repository approval.

## VM lifecycle

### Golden image

`sandboxsh image build`:

1. initializes a stopped temporary VM from `images:debian/13/cloud`;
2. attaches a default-deny build ACL before first boot, then waits for
   `incus-agent` and cloud-init;
3. pushes and runs `guest/provision.sh`;
4. installs Docker/Compose, toolchains, agents, and security defaults;
5. removes transient state and runs `cloud-init clean`;
6. stops and publishes the VM as `sandboxsh/base`;
7. deletes the temporary builder.

The published alias is reusable by every project in the user's restricted Incus
project. Rebuilding the alias does not mutate existing persistent VMs; use
`sandboxsh recreate` per project to consume it.

### Project VM

On first `up`, sandboxsh:

1. validates and approves `.sandboxsh.json` authority;
2. initializes a VM from the golden image with CPU/RAM/root-disk limits;
3. attaches only approved host paths;
4. attaches the shared agent volume when enabled;
5. creates/updates the host-enforced network ACL;
6. overrides the inherited NIC with ACL defaults and address filtering;
7. starts the VM and waits for the agent/mounts;
8. aligns the `dev` UID/GID with the host and initializes shared agent state;
9. copies a minimal Git identity and verifies Docker Compose/socket invariants;
10. enters a shell.

`down` stops but preserves the root disk. `destroy` removes it. `recreate`
removes and rebuilds it while leaving the host checkout and shared agent volume
intact.

## Network policy

Incus bridge ACLs operate outside the VM. The generated ACL contains only allow
rules; applying any ACL produces an automatic default reject for unmatched
traffic. The NIC explicitly requests reject defaults and logging.

DNS names are not placed directly in ACLs. The host resolves them at creation or
`refresh-firewall`, then rules contain IP literals. Rules are grouped by protocol
and port set. This is intentionally a snapshot: it is auditable and cannot be
bypassed by unsetting a proxy variable, but CDNs can rotate away from it.

The VM receives a direct NIC ACL rather than only a network-level ACL. Incus
notes that direct NIC ACLs on a bridge can control intra-bridge traffic, whereas
network-level bridge ACLs primarily operate at the bridge/host boundary.

Declared development ports produce ingress allow rules sourced from the managed
bridge gateway. The host reaches the guest directly, so multiple VMs can all use
port 3000 without host port mappings.

## Credential storage

The shared filesystem volume is managed by the Incus storage pool, not a host
home bind mount. `sandboxsh-agent-init` uses `flock` while creating and seeding
agent directories. Home paths are symlinked after the volume is mounted.

This is intentionally a cross-project trust domain. Project-local credentials
remain on each root disk and disappear on `destroy`/`recreate`.

## Why a VM instead of nested Incus containers

Docker Compose needs a Docker daemon, cgroups, OverlayFS, netfilter, and arbitrary
container workloads. An Incus VM provides its own kernel, so Docker works normally
without `security.nesting`, privileged system containers, host module coupling,
or a host Docker socket. Incus describes VM boundaries as enforced using hardware
virtualization features, unlike system containers that share the host kernel.

## Primary references

- [Incus security and daemon access](https://linuxcontainers.org/incus/docs/main/explanation/security/)
- [Incus projects and confined users](https://linuxcontainers.org/incus/docs/main/explanation/projects/)
- [Confine projects to Incus users](https://linuxcontainers.org/incus/docs/main/howto/projects_confine/)
- [Incus VM creation and agent](https://linuxcontainers.org/incus/docs/main/howto/instances_create/)
- [Incus disk devices for VMs](https://linuxcontainers.org/incus/docs/main/reference/devices_disk/)
- [Incus network ACLs](https://linuxcontainers.org/incus/docs/main/howto/network_acls/)
- [Incus cloud-init support](https://linuxcontainers.org/incus/docs/main/cloud-init/)
- [Containers and virtual machines](https://linuxcontainers.org/incus/docs/main/explanation/containers_and_vms/)
- [Docker Engine on Debian](https://docs.docker.com/engine/install/debian/)
- [Docker Compose plugin](https://docs.docker.com/compose/install/linux/)
