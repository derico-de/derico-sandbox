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

Normal subprocesses are invoked with
`incus --force-local --project user-<uid>` and an isolated `INCUS_CONF` whose
default is `local`, so a configured TLS remote or different current project
cannot redirect lifecycle commands. Current Incus creates the user project with
`restricted=true`, project-local images/profiles/storage volumes, host disk paths
limited to the user's home, and `features.networks=false`. The last setting means
the per-user bridge and its ACL namespace are owned by `default`; Incus 6.0 does
not support managed bridges in non-default projects.

This layout needs Incus 6.0.6+ (LTS) or 6.22+. `nic_bridged` in older daemons
validates `security.acls` against the network project but looks it up in the
instance's project while starting the NIC, so a VM in `user-<uid>` fails with
`Failed to start device "eth0": Failed loading ACL … Network ACL not found`. The
version is checked alongside the trust-boundary checks.

The trusted host therefore uses interactive `sudo incus` only for UID-scoped ACL
create/update/delete operations in the default network project. Policy generation,
private-address checks, and approval of project-added destinations happen before
that privileged call. All VM, image, storage, device, and mount lifecycle stays on
the restricted socket. Low-level settings and dangerous devices remain blocked.

The live workspace requirement is the reason host-path disks are allowed at all.
The launcher adds a second policy layer: project-internal paths are expected;
external paths need an out-of-repository approval.

## VM lifecycle

### Golden image

`sandboxsh image build` (`src/sandboxsh/imagebuild.py`) runs the stage scripts
in `guest/stages/` as an ordered chain and caches every finished stage:

1. pins the source alias (`images:debian/13/cloud`) to a VM-image fingerprint
   in `~/.cache/sandboxsh/build/manifest.json`; only `--refresh` re-resolves;
2. computes one key per stage from the parent key, the script hash, the
   stage's declared inputs (uid/gid, helper script hashes), the refresh
   generation, and the `SANDBOXSH_BUILD_ALLOW` hosts;
3. exits when the published image already carries the final key (no VM, no
   sudo); otherwise lists the stopped `sandboxsh-cache-<key>` VMs and picks the
   deepest matching one;
4. for each remaining stage: creates a worker `sandboxsh-build-<key>-<rand>`
   by copying the parent entry (a copy-on-write clone on Btrfs, ZFS, and LVM
   thin) or by `incus init` from the pinned source for the first stage;
   attaches the default-deny build ACL before first boot; waits for
   `incus-agent` and cloud-init; pins the allowlist; pushes and runs the stage
   script with streamed output; unpins; stops; stamps `user.sandboxsh.cache.*`
   and renames the worker into its cache name (the atomic visibility point);
5. publishes the last entry as the alias with `user.sandboxsh.build_key`,
   `user.sandboxsh.source`, `user.sandboxsh.stages`, and `user.sandboxsh.disk`
   properties, then deletes the build ACL. The entry stays for the next build.

The first stage disables cloud-init after its own first boot and the finalize
stage re-enables and cleans it, so copied workers do not re-run cloud-init as
new machines while every project VM still gets a fresh first boot. The stage
loader asserts those lines exist. A failed stage deletes its worker and leaves
the parent entry, so the retry starts there; `SANDBOXSH_KEEP_BUILDER=1` keeps
the worker and the ACL for inspection instead. Every build and `image cache
prune` on the host holds one `flock`, because the build ACL is a single shared
object and prune must not delete an entry a build is about to copy.

The published alias is reusable by every project in the user's restricted Incus
project. Rebuilding the alias does not mutate existing persistent VMs; use
`sandboxsh recreate` per project to consume it. `create_instance` refuses a
`resources.disk` below the image's `user.sandboxsh.disk` before Incus fails on
a block volume that cannot shrink.

The build workflow lives outside `Incus` while the project-VM workflow
(`create_instance`) stays inside it. That asymmetry is deliberate for now:
the build loop is the larger and more testable piece, and `Incus` remains the
only subprocess owner, growing thin verbs (`copy_instance`, `rename_instance`,
`list_instances`, `resolve_vm_image`, `image_property`, `publish`) for it.

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
intact. On an existing VM, `up` re-runs the approval gate and syncs workspace
mounts to the approved configuration, so mount changes never require
`recreate`; image, resource, and `agent_credentials` changes still do.

## Network policy

Incus bridge ACLs operate outside the VM. The generated ACL contains only allow
rules; applying any ACL produces an automatic default reject for unmatched
traffic. The NIC explicitly requests reject defaults and logging.

DNS names are not placed directly in ACLs. The host resolves them at creation or
`refresh-firewall`, then rules contain IP literals. Rules are grouped by protocol
and port set. This is intentionally a snapshot: it is auditable and cannot be
bypassed by unsetting a proxy variable, but CDNs can rotate away from it.

Because the snapshot is the host's view, the guest must share it. Applying the ACL
also writes those addresses into a marked block in the guest's `/etc/hosts`, so
guest-side resolution cannot pick a rotating CDN edge outside the ACL and stall on
a dropped connect. The block is rewritten on every apply and stripped from the
golden image before publication. Guest root can edit `/etc/hosts`, so this is an
availability measure; the NIC ACL is unchanged as the enforcement boundary.

The VM receives a direct NIC ACL rather than only a network-level ACL. Incus
notes that direct NIC ACLs on a bridge can control intra-bridge traffic, whereas
network-level bridge ACLs primarily operate at the bridge/host boundary.

Declared development ports produce ingress allow rules sourced from the managed
bridge gateway. The host reaches the guest directly, so multiple VMs can all use
port 3000 without host port mappings.

Tailnet publication is layered on top of that rule rather than widening it. A
socket-activated `systemd-socket-proxyd` listens on the host's tailnet address
and dials the guest across the bridge, so the packets arriving at the NIC are
still sourced from the bridge gateway and match the same ingress rule. The host
port is what becomes scarce, so publication is recorded in a per-host registry
that assigns each published port to one project. Host-originated traffic also
never crosses the IPv4 `FORWARD` hook, so publishing is unaffected by the
container-runtime lockdown that the bridge rule works around.

## Credential storage

The shared filesystem volume is managed by the Incus storage pool, not a host
home bind mount. `sandboxsh-agent-init` uses `flock` while creating and seeding
agent directories. Home paths are symlinked after the volume is mounted.

This is intentionally a cross-project trust domain. Project-local credentials
remain on each root disk and disappear on `destroy`/`recreate`.

The host's user-level skill directories (`~/.agents/skills`, `~/.claude/skills`,
`~/.vibe/skills`) are additionally shared with each VM as read-only disk
devices; `sandboxsh-agent-init` symlinks each skill into the skill location of
every agent (Claude Code, pi via the `~/.agents/skills` standard, and Mistral
Vibe). Only the `skills` subdirectories are exposed — never all of `~/.claude`
or `~/.vibe`, which hold host credentials — and the read-only devices keep the
guest from writing into the host's home.

A second skill root, `/opt/sandboxsh/image-skills`, is baked into the image
and linked by the same function after the host root, so a host skill of the
same name shadows the image copy and a skill installed inside the sandbox
shadows both.

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
