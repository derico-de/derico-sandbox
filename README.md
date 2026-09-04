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
  additions. Guest root and Docker cannot disable this ACL. Incus owns the
  incus-user bridge/ACL namespace in its `default` network project, so only fixed
  ACL create/update/delete operations use trusted host `sudo`; VM lifecycle stays
  on the restricted user socket.
- Only declared development ports accept host-to-VM traffic. They are reached at
  the VM's private address, avoiding host-port collisions between projects.
- A declared port can additionally be published on the host's tailnet address, so
  it is reachable as `<tailnet-node>:<port>` from any node in the tailnet. Because
  that turns a host-local port into a tailnet-wide one, and the request comes from
  guest-writable configuration, each mapping is published only after the trusted
  host approves it.
- CPU, memory, and root-disk limits are enforced by Incus.
- Claude, pi, and Vibe state is placed on one Incus-managed filesystem volume and
  shared across project VMs. This provides login-once behavior, but it deliberately
  means compromise of one project can read the shared agent credentials.
- SSH, cloud, application, and other credentials remain on each persistent VM's
  own disk unless you explicitly expose them.

See [SECURITY.md](SECURITY.md) for the threat model and residual risks.

## Install

Host requirements: Linux, KVM, systemd, Incus **6.0.6+ (LTS) or 6.22+**, Python
3.11+, `pipx`, `sudo`, and `kmod`. Earlier daemons look a bridged NIC's ACL up in
the instance's own project while ACLs can only exist in the `default` network
project, so every sandbox fails to start with `Network ACL not found`; Debian 13's
packaged 6.0.4 is affected, so install Incus from the upstream (zabbly) LTS
repository. The installer loads `br_netfilter` immediately and persists it
through `/etc/modules-load.d/sandboxsh.conf` because Incus NIC filtering requires
bridge netfilter support.
On a Debian/Ubuntu-style host, run the installer directly from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/derico-de/derico-sandbox/main/install.sh | bash
# Log out/in if it added you to the `incus` group.
sandboxsh doctor
sandboxsh image build       # one reusable image; the first build takes minutes, later ones seconds
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
per-user managed bridge when you first use it. Expect a host `sudo` prompt when
sandboxsh creates, refreshes, or removes the bridge's ACL; no other routine Incus
operations use administrator authority. Commands that will need it ask for the
password up front, before the slow Incus work, so the prompt never appears on a
terminal you have stopped watching.

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
sandboxsh agent pi          # create/start and launch Pi directly
sandboxsh agent claude      # create/start and launch Claude Code directly
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

### Herdr agent status

A VM hides its guest process tree from the host: Herdr sees the host-side
`incus exec` wrapper, not the `pi` or `claude` process inside it. Launch either
agent directly when the sandbox runs in a Herdr pane:

```bash
sandboxsh agent pi
sandboxsh agent claude
```

The command creates or starts the VM, launches the selected agent, and sets
Herdr's documented `HERDR_AGENT` hint only on the host-visible Incus process.
`sandboxsh exec -- pi` and `sandboxsh exec -- claude` also set the hint
automatically when the VM is already running. This lets Herdr apply its normal
Pi or Claude screen detection without exposing Herdr's control socket to guest
root.

Starting an agent after entering a generic `sandboxsh` shell cannot update that
hint: the already-running host wrapper cannot have its environment changed by a
process inside the VM. Use `sandboxsh agent ...` for panes whose agent status
Herdr should track.

Pass agent options after `--`, for example:

```bash
sandboxsh agent pi -- --session /path/to/session.jsonl
```

### Monitoring all sandboxes

`sandboxsh status` covers only the current project's VM. To see every sandbox
and its live resource usage, query Incus directly **on the host**, using the
same restricted per-user project that sandboxsh itself uses:

```bash
incus --force-local --project "user-$(id -u)" list -c nsmMuD4   # one-shot table
incus --force-local --project "user-$(id -u)" top               # live view
incus --force-local --project "user-$(id -u)" info <instance>   # per-VM detail
```

The `list` columns are name, state, memory usage, memory %, CPU time, disk
usage, and IPv4 address.

A small wrapper saves the typing. Create `~/.local/bin/incustop` on the host:

```bash
#!/bin/sh
exec incus --force-local --project "user-$(id -u)" top "$@"
```

Make it executable (`chmod +x ~/.local/bin/incustop`), then `incustop` gives a
live CPU/memory/disk view of all sandbox VMs. These commands work only on the
host; the Incus control socket is deliberately not exposed inside the VMs.

## Configuration

```json
{
  "name": "acme",
  "workdir": "/workspaces/acme",
  "dirs": [
    ".",
    {"path": "../shared-reference", "ro": true, "target": "/reference"}
  ],
  "ports": [
    3000,
    {"guest": 8080, "host": 18080},
    {"guest": 5432, "tailnet": false}
  ],
  "tailscale": {"enabled": true},
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
- Host-path mounts use virtiofs or 9p, which are unsuitable for some SQLite WAL
  workloads. pnpm's SQLite-backed package store is therefore kept on the VM-local
  disk at `~/.local/share/pnpm/store`; project files and `node_modules` remain in
  the live host checkout.

### Network

The built-in allowlist covers agent APIs, GitHub, npm/PyPI,
Debian/Node/Docker/Chrome package sources, Playwright's browser CDN, and the
Docker Hub registry. Project additions require approval:

```bash
sandboxsh approve
sandboxsh refresh-firewall
```

Hostnames are resolved on the **host** and their current IPv4/IPv6 addresses are
written into the ACL. The same addresses are pinned in the guest's `/etc/hosts`
whenever the ACL is applied, so a rotating CDN cannot hand the guest an address
the host never approved — without it, a name such as `download.docker.com` returns
different CloudFront edges to host and guest and the guest hangs until its connect
timeout. The pin is for availability; the ACL remains the enforcement point.
Run `refresh-firewall` after CDN rotation. The golden-image
builder is protected by the same ACL; add an exceptional build-only hostname with
`SANDBOXSH_BUILD_ALLOW=host1,host2 sandboxsh image build`. The extra hosts are
part of every cached build stage's key, so a stage built under a widened
allowlist is never reused by a build that did not ask for it. Wildcards are
not accepted. Private/RFC1918 destinations are rejected unless the endpoint sets
`"allow_private": true`; that expanded authority is highlighted during approval.
Loopback, link-local/metadata, multicast, unspecified, and reserved destinations
are always rejected. A built-in endpoint that is temporarily unresolvable is
omitted from the ACL (fail-closed) and reported by the CLI; an unresolvable
project-added endpoint remains an error.

`firewall.enabled=false` is rejected unless the trusted host shell explicitly
sets `SANDBOXSH_ALLOW_OPEN_NETWORK=1`.

### Host-wide endpoints

Endpoints every sandbox on this host may reach — a tailnet service, an internal
registry — go in `~/.config/sandboxsh/endpoints.json`. The file lives outside
every mounted project, so like the built-in allowlist it is trusted host policy
and needs no per-project approval. Entries use the same shape as
`firewall.allow`:

```json
{
  "version": 1,
  "allow": [
    {"host": "planetmobile", "ports": [8228], "allow_private": true}
  ]
}
```

Tailnet names resolve to CGNAT (100.64.0.0/10) addresses, so they need
`"allow_private": true`. Like a built-in endpoint, a host-wide endpoint that is
temporarily unresolvable (e.g. tailscale is down) is omitted fail-closed and
reported rather than blocking `up`. Existing sandboxes pick the change up on
the next `sandboxsh up` or `sandboxsh refresh-firewall`.

If Docker or podman also runs on the host, it sets the IPv4 `FORWARD` policy to
`DROP` and accepts only its own bridges. VM traffic is routed off the Incus bridge
and crosses that hook, so every allowlisted endpoint blackholes while host-side
checks keep working. `sandboxsh doctor` detects this and prints the fix:

```bash
sudo iptables -I DOCKER-USER -i incusbr-<uid> -j ACCEPT
sudo iptables -I DOCKER-USER -o incusbr-<uid> -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT
```

On a ufw-managed host, `sudo ufw route allow in on incusbr-<uid>` is equivalent;
`doctor` accepts an allow rule for the bridge from any chain, whichever tool wrote
it. Either way the rule is lost on reboot. To re-add it at every boot:

```bash
./install.sh --forward-unit    # or: curl -fsSL <raw install.sh> | bash -s -- --forward-unit
```

That installs `/usr/local/sbin/sandboxsh-bridge-forward` and a oneshot
`sandboxsh-bridge-forward.service` ordered after `docker`/`podman`/`incus`. The
helper only ever adds the two rules — it never flushes a chain, exits cleanly when
`iptables` is absent, and works before the bridge exists, since an interface match
does not require the interface. Nothing `Requires=` the unit, so a failure cannot
hold up the boot. A firewall reload that flushes the chains still drops the rule;
`sudo systemctl restart sandboxsh-bridge-forward` puts it back. They grant no authority inside
the sandbox — the ACL is enforced in nftables' `bridge incus` table, which an
accept in `ip filter` cannot override.

`sandboxsh image build` streams each stage's output, so a blocked endpoint
appears as the URL that timed out. `SANDBOXSH_KEEP_BUILDER=1` keeps the failed
stage's worker VM (`sandboxsh-build-<key>-<random>`) and the build ACL, so the
same request can be retried from inside the VM under the ACL that blocked it.
The error names the worker; delete it afterwards, or let the next build remove
it (it says so when it does).

To open a development server, declare its guest port and bind the service to
`0.0.0.0` inside the VM:

```json
{"ports": [3000]}
```

```bash
sandboxsh refresh-firewall  # applies firewall and port changes live
sandboxsh url 3000
```

### Reaching a sandbox from your tailnet

A declared port is reachable at the VM's private address from the host that owns
the VM. To reach it from anywhere in your tailnet, sandboxsh can publish it on
this host's tailnet address, so a service in the VM answers on
`<tailnet-node>:<port>`.

Install the host helper once (it writes the systemd units that do the
forwarding, so it needs root):

```bash
./install.sh --publish-helper --no-deps
```

Then declare the ports and start the VM. The first `up` asks the host to approve
each mapping, because `.sandboxsh.json` is guest-writable and publishing widens a
host-local port to the whole tailnet:

```json
{"ports": [8080, 8085]}
```

```bash
sandboxsh up --no-shell
# Publish project 'travelstream' guest port 8080 to the whole tailnet on host port 8080? [y/N]
sandboxsh status     # lists every tailnet URL
sandboxsh url 8080   # http://powerman:8080
```

Publishing is deliberately never fatal. An unapproved port, a tailnet that is
down, or a missing helper is reported and skipped, and the VM still starts with
its port reachable at the VM address. `sandboxsh url <port> --vm` always prints
that address.

A tailnet node has a single port 8080, so two projects cannot both publish it.
The second one to start is refused by name rather than silently shadowing the
first; give it a different host port:

```json
{"ports": [{"guest": 8080, "host": 18080}]}
```

Use `{"guest": 5432, "tailnet": false}` for a port that should stay reachable
only at the VM address, or `"tailscale": {"enabled": false}` for a whole project.
Set `"tailscale": {"address": "..."}` to bind a specific host address instead of
the one `tailscale ip -4` reports.

Withdraw a project's listeners and free its host ports with `sandboxsh
unpublish`; `sandboxsh down` withdraws them too, but keeps the ports reserved for
that project. Revoke the approvals themselves with `sandboxsh approve --revoke`.

Two things are worth knowing about how this is wired. The listener runs on the
host and dials the VM over the Incus bridge, so the guest sees the bridge gateway
as the source address — the ACL's existing ingress rule already allows exactly
that, and publishing therefore changes no network policy inside the sandbox. The
same property means the `DOCKER-USER` forwarding rule above is not involved:
host-originated traffic never crosses the `FORWARD` hook.

Your tailnet policy still applies on top. If the tailnet ACL or `tailscale up
--shields-up` blocks inbound connections to this node, the published port is
listening but unreachable.

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

## Host agent skills

User-level skills found on the host in `~/.agents/skills` (the cross-agent
standard), `~/.claude/skills` (or `$CLAUDE_CONFIG_DIR/skills` when set), and
`~/.vibe/skills` are mounted read-only under `/opt/sandboxsh/host-skills` at
VM creation. Each skill is then symlinked into every agent's skill location
inside the VM — `~/.claude/skills` for Claude Code, `~/.agents/skills` for pi,
and `~/.vibe/skills` for Mistral Vibe — so all agents share all host skills.
Skills installed inside a sandbox under the same name keep priority; the
read-only mounts mean the guest can use but never modify host skills. The
mounts are captured when the VM is created, so run `sandboxsh recreate` after
adding the first skill on a host that had none.

## Skills in the image

Two skills ship with the golden image itself, so they are there on a host that
keeps no skills directory at all:

- `unslop` cuts the tells that mark text as model-written.
- `bro` restates the previous message in plain language, without jargon.

Both come from the [pstack plugin](https://github.com/cursor/plugins/tree/main/pstack)
and are fetched at build time, so a rebuild picks up upstream edits. They live
under `/opt/sandboxsh/image-skills` and are linked into `~/.claude/skills`,
`~/.agents/skills`, and `~/.vibe/skills` at every boot by the same code that
links host skills, host root first. A skill installed inside the sandbox wins,
then a host skill of that name, then the image copy. Remove one for a single sandbox
by deleting its symlink, or for good by dropping it from `guest/stages/50-agents.sh`
and rebuilding.

## Host agent instructions

The host's user-level `~/.agents/AGENTS.md` — the cross-agent standard — is
mounted read-only at `/opt/sandboxsh/host-instructions` by the same reasoning,
so agents in the sandbox follow the same rules as on the host. The guest links
it into `~/.agents/AGENTS.md` for pi and other AGENTS.md-aware agents and into
`~/.claude/CLAUDE.md` for Claude Code. A `CLAUDE.md` or `AGENTS.md` written
inside the sandbox keeps priority, and a link left behind by a removed host file
is cleaned up on the next start. A host `CLAUDE.md` is not mounted: when it only
imports `AGENTS.md` it adds nothing, and reaching it would mean sharing all of
`~/.claude`. Like host skills, the mount is captured at VM creation, so run
`sandboxsh recreate` for a VM created before the host had an `AGENTS.md`.

Changes to image, resources, or `agent_credentials` are immutable for an
existing VM. `sandboxsh up` detects their fingerprint and requires
`sandboxsh recreate`. Mount changes are applied by the next `up` — external
mounts still require host approval first — hotplugging into a running VM
without touching its disk. Firewall destinations and declared ports are updated
by `refresh-firewall` or the next `up`; `workdir` changes affect the next
shell.

## Golden image contents

The build installs:

- Docker Engine, BuildKit, and Compose v2
- Git, tig, curl, jq, ripgrep, fd, yazi, build-essential, PostgreSQL client,
  Vim/Neovim
- Node.js 24 and pnpm; image-wide, project, and Pi package installs all use pnpm
- Python 3, uv, ruff, pytest, tox/tox-uv, Invoke, and plonecli 7.0.0b14 or newer
- OpenLDAP and SASL development headers for building `python-ldap`
- Cairo, Pango, and image libraries for WeasyPrint-based PDF exports
- Claude Code, pi (plus subagents/Impeccable/sideshow), and Mistral Vibe
- Google Chrome and the Chrome DevTools MCP server (see below; amd64 only)
- Playwright with its Chromium build, plus the Playwright MCP server
  (see below)
- the sideshow CLI, so any agent can publish to a visual surface (see below)
- the `unslop` and `bro` writing skills from pstack, linked into every
  agent's skill directory (see *Skills in the image*)
- a default git pre-push hook that requires `SANDBOXSH_ALLOW_PUSH=1`
- a default Claude Code status line (model, directory, branch, context-window
  usage, and five-hour/weekly plan-limit percentages), installed next to
  settings.json at boot; plan limits appear for Claude.ai subscribers after the
  first API response, and a statusLine you configure yourself is left alone

Pi package identifiers still use Pi's `npm:<package>` source syntax, but the
configured `npmCommand` is `pnpm`, so registry lookups and installation are
performed by pnpm rather than npm.

Rebuild the alias after changing anything under `guest/`:

```bash
sandboxsh image build
sandboxsh recreate
```

### Incremental builds

The build is a chain of stage scripts under `guest/stages/`, run in order as
root inside a disposable worker VM:

| Stage | Installs |
|-------|----------|
| `10-base` | Debian packages, locales, editors |
| `20-docker-node` | Docker Engine and Compose, Node.js 24, pnpm |
| `30-user` | the `dev` account matched to your uid/gid, sudoers, `/workspaces` |
| `40-browser` | Google Chrome, Chrome DevTools MCP, Playwright with Chromium, Yazi |
| `50-agents` | sideshow, image skills, uv, Claude Code, pi and its extensions, uv tools |
| `90-finalize` | git hooks, login profile, agent seed, guest helpers, cleanup, smoke checks |

Every finished stage is kept as a stopped VM named `sandboxsh-cache-<key>` in
your Incus project. The key hashes the stage script, its inputs (the pinned
source image, your uid/gid, the guest helper scripts, `SANDBOXSH_BUILD_ALLOW`),
and the key of the stage before it. The next build reuses the deepest matching
entry and rebuilds only what follows; a build whose final key is already
stamped on the published image exits in seconds without creating a VM or
asking for a password. The output says why each stage is rebuilt:

```text
source   debian/13/cloud  a046182b (pinned 2026-09-04)
hit      10-base          a1b2c3d4a1b2c3d4  age 3d
hit      20-docker-node   b2c3d4e5b2c3d4e5  age 3d
miss     30-user          c3d4e5f6c3d4e5f6  script changed
miss     40-browser       d4e5f6a7d4e5f6a7  parent changed
...
publish  sandboxsh/base   build_key f6a7b8c9f6a7b8c9 (4 of 6 stages rebuilt, 7m12s)
```

Installers that fetch "latest" (apt repositories, pnpm, Claude Code, pi, uv,
Yazi, the image skills) are not part of the key, so a cache hit deliberately
does not pick up a new upstream release. Choose when it does:

```bash
sandboxsh image build --refresh                # re-pin the source image, rebuild every network-facing stage
sandboxsh image build --refresh-from 50-agents # new agent releases without redoing apt, Docker, or Chrome
sandboxsh image build --no-cache               # rebuild everything, replacing the cached entries
sandboxsh image build --dry-run                # show the plan and change nothing
sandboxsh image build --no-publish             # iterate on stages without paying the publish
sandboxsh image status                         # published build key; which stage a build would start from
sandboxsh image cache list                     # every entry with age and chain membership
sandboxsh image cache prune                    # drop entries no current or previous-generation build uses
```

A reused entry older than `SANDBOXSH_CACHE_MAX_AGE_DAYS` (default 30) prints
a warning; older than three times that refuses to build unless you pass
`--refresh` or `--allow-stale`, so the base OS, Docker, and Chrome always get
a forced refresh point. `--refresh` counts up a generation stored in
`~/.cache/sandboxsh/build/manifest.json` together with the pinned source
fingerprint; if a refresh brought in a bad release, `sandboxsh image build
--generation <previous>` rolls back to the earlier chain, whose entries stay
until pruned. Losing the manifest only costs one full build. Builds and prunes
on one host take a single lock (`--no-wait` fails instead of queueing).

The first build after upgrading from a release without the stage cache runs
every stage. A leftover `sandboxsh-image-builder-<uid>` VM from that release
is reported by `sandboxsh doctor` and `image cache list`, never deleted for you.

## Browser automation

The image ships Google Chrome stable and
[`chrome-devtools-mcp`](https://github.com/ChromeDevTools/chrome-devtools-mcp),
registered at boot as the user-scope Claude Code MCP server `chrome-devtools`,
so an agent can drive a real browser, read the console, inspect network
requests, and record performance traces. In shared mode the registration lands
in `/agent-creds/claude/.claude.json`, otherwise in `~/.claude.json`; MCP
servers are never read from `settings.json`. Verify it with `claude mcp list`
inside the sandbox.

The defaults are `--headless` (the VM has no display) and `--isolated` (a
throwaway Chrome profile per server, so two agents in one VM do not fight over
one user-data directory), with Google's usage statistics and update checks
turned off. Change the entry inside a sandbox and it is left alone on every
later boot — an existing `chrome-devtools` server is never overwritten.

Playwright is the second browser, registered the same way as the MCP server
`playwright` with `--headless --isolated`. It drives Playwright's own Chromium
through accessibility snapshots instead of the DevTools protocol, which suits
filling forms and walking end-to-end flows, and it is the engine a project's
Playwright tests already use. Drop either entry from `.claude.json` inside a
sandbox to give an agent one browser instead of two tool sets.

The `playwright` CLI is on `PATH` and the Chromium build is shared through
`PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright`, owned by `dev`. A project pinned
to a different Playwright version installs its own revision there with
`pnpm exec playwright install`, and `pnpm exec playwright install firefox webkit` adds the
other engines; Playwright's primary and fallback download mirrors are on the
built-in allowlist, so both work without a firewall change. Node prefers IPv4
for these downloads because an Incus bridge may not provide IPv6 egress.

Both browsers are subject to the same ACL as everything else in the VM: they
reach `localhost` services in the sandbox freely, and any external site they
should load needs an entry in `firewall.allow`. Only Claude Code is wired up;
pi has no MCP support by design.

## Visual previews

[sideshow](https://github.com/modem-dev/sideshow) is a live surface a user
watches in a browser while an agent publishes HTML, markdown, diffs, diagrams,
and highlighted code to it. pi reaches it through the extension in the image;
Claude Code and Mistral Vibe have no such extension, so the image installs the
`sideshow` CLI globally and every agent in the VM can use it:

```bash
SIDESHOW_URL=http://your-host:8228 sideshow agent-howto
SIDESHOW_URL=http://your-host:8228 sideshow publish sketch.html --title Layout
```

Which surface the CLI talks to is yours, not the image's: it reads
`SIDESHOW_URL` (and `SIDESHOW_TOKEN` for a deployed instance that wants a
bearer token) on each call, so nothing is baked in and one image serves several
surfaces. The default is `localhost:8228`, which inside a sandbox means a server
in that VM, not the one on your workstation. A surface outside the VM is a
network destination like any other: add its host and port to `firewall.allow`,
or to `~/.config/sandboxsh/endpoints.json` on the host to reach it from every
sandbox. Telling agents about the surface is the job of your `~/.agents/AGENTS.md`,
which is already shared into every VM (see *Host agent instructions*) — the
running server prints a block to paste at `curl -s $SIDESHOW_URL/setup`.

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
- `guest/` — golden-image stage scripts (`stages/`) and per-instance init scripts
- `install.sh` — host bootstrap using the restricted Incus user service
- `docs/architecture.md` — design and control-plane details
- `tests/` — configuration, security-policy, and CLI tests
