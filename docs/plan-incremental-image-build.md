# Plan: incremental `sandboxsh image build`

Status: proposed (2026-09-04), reviewed, not yet implemented. Independently
reviewed by four reviewers (critic, architect, security, developer experience);
their findings are folded in and listed in section 12. Phase 0 measurements
are recorded in section 9.

## 1. Recommendation in short

Build the golden image as an ordered chain of **stage scripts**, cache the
result of every completed stage as a **stopped Incus VM in the user project**,
and reuse the deepest matching cache entry on the next build. Keep the final
`incus publish` in the first implementation and skip it entirely when the
published image already carries the final build key (no-op build). Defer
direct template cloning of project VMs until a measurement shows publish plus
first unpack still hurts after stage caching.

Why this shape rather than the full proposal:

- On this host (Btrfs pool) and on ZFS or LVM thin, a same-pool `incus copy`
  of a stopped VM is a copy-on-write clone (verified in the Btrfs driver
  source), so stopped instances are a sound cache medium.
- Incus **snapshots are blocked** in the `incus-user` project by default
  (`restricted.snapshots` defaults to `block`; `incus-user` never sets it), so
  a snapshot-based cache would need an admin change on every user project.
  Stopped instances need no project change.
- `incus init` from a published image is already a CoW clone of the unpacked
  image volume ("optimized image storage"); only the first init after a
  publish pays the qcow2 unpack. Direct template cloning therefore saves the
  publish (qcow2 convert plus gzip, serialized daemon-wide) and one unpack
  per build, not per project VM. That is real but bounded, and it adds a
  second creation path and new lifecycle coupling. Measure first.
- Publishing an image per stage instead of caching instances was considered
  and rejected for the same reason: each publish is the expensive step the
  cache exists to avoid.
- The stage split is the split `docs/plan-configurable-provisioning.md`
  already wants for "features": an optional feature is an optional stage in
  the chain. One mechanism serves both plans.

## 2. Verified facts

| Fact | How verified |
|------|--------------|
| Incus 7.3 client and server; pool `default` is **Btrfs, loop-backed** (300 GiB loop file under `/var/lib/incus/disks`, a bind of `/home/maik/incus-disks` on the `/home` Btrfs partition); 72.9 GiB used; 17 project VMs | `incus storage info default`, `findmnt` |
| `/var/lib/incus/images` is on `/` (ext4, 55 GiB, **1.1 GiB free** at 12:00 today). The directory is root-only, so its size could not be read | `df`, `findmnt` |
| VM publish refuses a running instance, mounts the disk, runs `qemu-img convert -f raw -O qcow2` into `os.MkdirTemp(VarPath("images"), "incus_export_")`, then tars and compresses | `driver_qemu.go` `Export()` |
| Publish operations are serialized by the daemon because of I/O and CPU cost | Incus docs, images_create |
| Same-pool copy on Btrfs: `CreateVolumeFromCopy` = `snapshotSubvolume` (CoW reflink, no Incus snapshot relationship) then `SetVolumeQuota` to the target size; the VM `root.img` lives inside the subvolume | `driver_btrfs_volumes.go` |
| Block (VM) volumes **cannot be shrunk** (`ErrCannotBeShrunk`) and cannot be resized online (`ErrInUse`); growing truncates the file and moves the GPT alt header | `drivers/utils.go` `ensureVolumeBlockFile` |
| Btrfs `DeleteVolume` refuses only when the volume has Incus snapshots; subvolumes sharing extents with a deleted source are unaffected, so pruning any cache entry is always allowed | `driver_btrfs_volumes.go` |
| Copy drops every `volatile.*` key except `volatile.base_image`, `volatile.apply_nvram`, `volatile.last_state.idmap`; the new instance gets a fresh `volatile.uuid`, NIC `hwaddr`, and cloud-init instance-id. Rename also resets the cloud-init instance-id | `internal/instance/config.go`, `instance_utils.go`, `driver_qemu.go` |
| Copy re-checks project restrictions against the merged source config (`project.AllowInstanceCreation`), so a cache entry may only carry config keys the restricted project allows for a fresh instance. Neither copy nor publish has its own `restricted.*` gate | `instances_post.go` `createFromCopy`, `project/permissions.go` |
| `restricted.snapshots` and `restricted.backups` default to `block` under `restricted=true`; `incus-user` sets neither | `project/permissions.go`, `cmd/incus-user/server.go` |
| Storage feature matrix: btrfs, lvm (thin) and zfs all have optimized image storage, CoW, and instant cloning; only lvm is block-based | Incus storage driver reference |
| Btrfs driver docs: VMs are "a big file on disk"; "you should never use VMs with Btrfs storage pools" | Incus Btrfs reference |
| ZFS: parents cannot be removed while clones exist; Incus renames such objects into `deleted/` and reclaims later; `zfs.clone_copy` (true/false/rebase) controls it | Incus ZFS reference |
| `images:debian/13/cloud` resolves to a **container** image unless `--vm` is passed; `incus image info images:debian/13/cloud --vm` gives `a046182b…` today (serial 20260904) | remote query, `incus image info --help` |
| The locally cached source image `85002cf5…` (serial 20260903) is `Cached: yes, Auto update: enabled`; the builder records the fingerprint it used in `volatile.base_image` | `incus image info`, builder config |
| `incus publish` accepts trailing `key=value` image properties; `incus rename` and `incus copy -c/-d` exist as assumed. Incus 6.23 misparsed a property value containing a colon as a remote (forum report); hex keys and fingerprints contain none | `incus publish --help`, forum |
| Every project VM has a stray `debian` user (uid 1001): after `cloud-init clean` in the builder, the first project boot re-runs cloud-init per-instance modules and re-creates the default user because uid 1000 is now `dev` | `getent passwd` in `ss-derico-sandbox-…` |
| Guest root filesystem holds about 4 GiB inside a 40 GiB root disk; the published image is 2.37 GiB compressed | `df` in a project VM, `incus image list` |
| `create_instance()` never passes a pool; the root disk comes from the profile (`pool: default`). `DEFAULT_POOL` only governs the credentials volume | `incus.py` |
| The build ACL name is derived from the fixed builder config path, so every build of every alias uses the **same** ACL | `build_image()`, `ProjectConfig.acl_name` |
| `resources.disk` is validated for format only; a value below the image's virtual size fails inside Incus (`Block volumes cannot be shrunk`), not in sandboxsh | `config.py`, `drivers/utils.go` |
| There is **no test** covering `build_image()` today | grep over `tests/` |

A build was running while this review started (builder created around
11:20, image `0d52033a` published 12:00). Nothing here touched it.

## 3. Answers to the review questions

1. **Same-pool VM copy cost.** Btrfs: CoW subvolume snapshot including
   `root.img`, then a file grow if the target size is larger. Instant
   regardless of size. LVM thin: a thin snapshot; without a thin pool it
   degrades to rsync/dd. ZFS: a clone of an implicit snapshot when
   `zfs.clone_copy=true` (default). Only a cross-pool copy or `--refresh`
   falls back to a full transfer. Cache, worker, template, and (if Phase 3
   ever happens) project VMs must therefore share one pool.
2. **Stopped instance as long-lived template.** Sound. Each copy gets a fresh
   UUID, MAC, and cloud-init instance-id; profiles are re-applied; `-c`/`-d`
   flags set limits and root size in the same call. Two constraints: the root
   disk may only grow (the template's disk must be at most the smallest
   `resources.disk` any project uses; the 30 GiB builder disk already imposes
   this on the published image because the qcow2 carries the same virtual
   size), and the guest data must be first-boot ready (machine-id cleared,
   cloud-init cleaned, hosts pins stripped) when the template is committed.
3. **Parent/child pruning.** Btrfs: no dependency; every entry is
   independently deletable. LVM thin: thin snapshots are independent. ZFS:
   deleting a cloned-from entry succeeds from the user's view (Incus parks it
   under `deleted/`); space returns when the last clone goes. Prune can
   always delete cache entries; only accounting differs per driver.
4. **Snapshots instead of instances.** Blocked in the restricted project
   unless an admin sets `restricted.snapshots=allow` per user project, and
   on ZFS restores across copy points are refused. Stopped instances need no
   project change and are inspectable with normal `incus` commands.
   **Rejected for the cache.**
5. **Direct template cloning vs keeping publish.** Keep publish in Phase 2;
   its cost is paid only when the final key changed and the per-project
   creation path stays untouched. Phase 3 revisits it with numbers.
6. **Freshness for unpinned inputs.** Separate a structural key (what the
   build would do) from a refresh generation (when it was last allowed to
   hit the network), plus a hard age ceiling. Section 5.3.
7. **Source alias to fingerprint.** Resolve with `incus image info <alias>
   --vm`, fall back to the local cached fingerprint when the remote is
   unreachable, and pin the chosen fingerprint in the build manifest. Only
   `--refresh` re-resolves. Debian publishes daily; keying on "latest
   upstream" would make a no-op build impossible.
8. **A VM-appropriate pool.** Separate track (section 8). The cache design
   is pool-agnostic; it needs only same-pool CoW, which all three drivers
   provide.
9. **Btrfs warning.** Relevant but not acute: the risk is `root.img`
   fragmentation and slow snapshot handling on a large CoW file, compounded
   here by a loop file that itself lives on Btrfs. `doctor` should report
   driver, loop backing, pool free space, and free space on the filesystem
   behind `/var/lib/incus/images` (publish needs roughly the uncompressed
   guest data plus the compressed tarball there; 1.1 GiB free is a
   build-failure risk today).
10. **Two-stage MVP.** Yes, as the first cache-enabled content: "toolchain"
    (everything network-heavy) and "finalize". The stage mechanism is N-ary
    from day one; the number of files in `guest/stages/` is a content
    decision, not a code one.
11. **Module interface and concurrency.** Sections 5.6 and 6.
12. **Trusted inputs.** Section 7.

## 4. Vocabulary

- **Source image**: the remote alias (`images:debian/13/cloud`) resolved to a
  VM-image fingerprint and pinned.
- **Stage**: one script `guest/stages/NN-name.sh`, run as root inside a
  booted worker, plus the files it needs. Ordered by `NN`. The stage name is
  the file stem (`40-browser`); `--refresh-from` takes exactly that stem.
- **Stage inputs**: parameters a stage reads, passed as script arguments and
  mixed into its key (uid/gid for the user stage, helper script hashes for
  finalize, architecture and pinned source for stage 10, the sorted extra
  build-allow hosts for every floating stage).
- **Stage key**: `sha256(parent key, stage name, sha256(script), stage
  inputs, generation if floating)[:16]`.
- **Cache entry**: a stopped VM `sandboxsh-cache-<key>` stamped with
  `user.sandboxsh.cache.*` config keys. Incus is the database; no host file
  is authoritative.
- **Worker**: a running VM `sandboxsh-build-<key>-<6 random hex>` created by
  copying its parent entry, or by `incus init` from the source image for
  stage 10. Either committed (renamed) into a cache entry or deleted.
- **Template**: the cache entry of the last stage. What gets published.
- **Build key**: the template's stage key, stamped on the published image as
  the property `user.sandboxsh.build_key`.
- **Refresh generation**: a host-side integer mixed into the key of every
  floating stage; bumped by `--refresh`, per stage by `--refresh-from`.

## 5. Design

### 5.1 Stage layout

```
guest/stages/10-base.sh         apt base packages, locales, fd link, editor
guest/stages/20-docker-node.sh  Docker Engine + Compose, Node 24, pnpm
guest/stages/30-user.sh         dev user/group (uid/gid input), sudoers, /workspaces
guest/stages/40-browser.sh      Chrome, chrome-devtools-mcp, Playwright + chromium, Yazi
guest/stages/50-agents.sh       uv, Claude Code, pi + extensions, uv tools, sideshow, image skills
guest/stages/90-finalize.sh     git hooks, /etc/profile.d/sandboxsh.sh, agent seed,
                                helper install, cleanup, smoke checks
```

Phase 1 splits `provision.sh` into these files with no behaviour change: the
concatenation in order is the current script, run in one worker in one boot.
Feature scripts from the configurable-provisioning plan later become extra
stages selected by host config, inserted before `90-finalize.sh`.

Rules for a stage script:

- runs as root with `set -euo pipefail`; may assume earlier stages ran;
- receives inputs as arguments (`30-user.sh UID GID`), never from the host
  environment;
- leaves no `/tmp` residue it needs later; runs `apt-get clean` and removes
  `/var/lib/apt/lists/*` if it used apt.

Cross-stage cloud-init contract (Phase 2, when workers boot more than once):
stage 10 ends with `touch /etc/cloud/cloud-init.disabled` after cloud-init
finished, and `90-finalize.sh` removes it before `cloud-init clean --logs
--machine-id`. Without this, every worker copied from a cache entry boots
with a new instance-id and cloud-init re-creates `debian`, re-grows
partitions, and regenerates SSH host keys at every stage. The stage loader
asserts that `10-*` and `90-finalize` exist and that the contract lines are
present, so the invariant lives in code, not in this document. (The stray
`debian` uid 1001 in today's project VMs is the same effect on first
project boot; removing it belongs to finalize but is a separate change.)

### 5.2 Build algorithm

```
lock(cache)                                     # one lock for every build and prune
inputs   = resolve(source, uid, gid, arch, stages, build_allow, generations)
keys     = chain(inputs)                        # [(stage, key), ...]
if image property build_key on alias == keys[-1].key and not --no-cache:
    print "up to date"; exit 0                  # no VM, no sudo, < 5 s
entries  = cache.entries()                      # `incus list sandboxsh-cache- --format=json`
hit      = deepest i with keys[i].key in entries (none if --no-cache)
prime sudo; acl = apply_acl(build_config)       # once per build, only on a miss
parent   = entries[keys[hit]] or None
for stage, key in keys[hit+1:]:
    worker = parent ? copy(parent, worker_name(key)) : init(source_fp, worker_name(key))
    attach_acl(worker); start; wait agent
    cloud-init status --wait                    # real work on stage 10 only, no-op later
    pin hosts
    push stage script (+ helper files for finalize); exec it, streamed
    unpin hosts; sync; stop --force
    parent = cache.commit(worker, stage, key, parent_key, inputs)   # stamp + rename
publish(parent, alias, build_key=..., source=..., stages=...) unless --no-publish
delete_acl; delete stale workers; unlock
```

The `cloud-init status --wait` before the pins is the same ordering the
current builder uses and must stay: on the stage-10 first boot cloud-init
rewrites `/etc/hosts`, and pinning before it finishes loses the pins
mid-apt.

Commit = `incus stop --force`, `incus config set user.sandboxsh.cache.*`,
`incus rename sandboxsh-build-<key>-<rand> sandboxsh-cache-<key>`. The rename
is the atomic visibility point. Anything still named `sandboxsh-build-*` at
the start of a build is a leftover and is deleted, unless it is the
`SANDBOXSH_KEEP_BUILDER` survivor described in 5.7.

Cache entry config keys (only `user.*` keys are ever stamped, so the entry
stays copyable under the restricted project's re-check):

```
user.sandboxsh.cache.key         = <key>
user.sandboxsh.cache.stage       = 40-browser
user.sandboxsh.cache.parent      = <parent key>
user.sandboxsh.cache.source      = <source fingerprint>
user.sandboxsh.cache.created     = <ISO timestamp>
user.sandboxsh.cache.generation  = <generation used>
user.sandboxsh.cache.build_allow = <sorted extra hosts or empty>
```

### 5.3 Cache keys and freshness

Structural inputs (in the key):

- pinned source VM-image fingerprint and architecture (stage 10, hence all);
- content hash of the stage script (each stage);
- uid/gid (stage 30 and descendants);
- content hash of `agent-init.sh` and `instance-init.sh` (finalize);
- selected optional stages (each is a link in the chain);
- sorted `SANDBOXSH_BUILD_ALLOW` hosts (every floating stage). A widened
  build ACL changes what a stage can fetch and therefore what it produces;
  an entry built under a widened allowlist must never be silently reused by
  a build that did not ask for it. The list is also stamped on the entry so
  `cache list` can show it.

Floating inputs (not in the key): apt repositories, `pnpm@latest`, the uv,
Claude and pi installers, GitHub `latest` release of Yazi, skill files at
`main`, PyPI/npm resolutions. Policy:

- `--refresh`: bumps the global generation, mixed into every floating stage
  (all except finalize). Full rebuild from a re-resolved source.
- `--refresh-from <stage>`: bumps only that stage's generation; descendants
  change because their parent key changes. Picks up a new Claude/pi release
  without redoing apt, Docker, or Chrome.
- `--no-cache`: ignore existing entries for lookup, still commit new ones
  (Docker semantics); same-key entries are replaced.
- Age policy on the deepest reused entry: older than
  `SANDBOXSH_CACHE_MAX_AGE_DAYS` (default 30) prints a warning; older than
  three times that (default 90) refuses to build unless `--allow-stale` or
  `--refresh` is given. The build never rebuilds silently and never reuses
  indefinitely; base OS, Docker, and Chrome do get a forced refresh point.

Generations and the pinned source fingerprint live in
`~/.cache/sandboxsh/build/manifest.json` (one file, all aliases). Losing it
is harmless: the next build re-resolves the source (network) and starts a
new chain; old entries become prunable. `--generation <n>` restores an
earlier global generation and is documented in the README as the rollback
for a bad `--refresh` (the previous chain's entries survive until pruned).

### 5.4 Publish and the no-op path

`incus publish <template> --alias <alias> --reuse user.sandboxsh.build_key=<key>
user.sandboxsh.source=<fp> user.sandboxsh.stages=<comma list>`. `image
status` and the no-op check read the property with `incus image info`.
`--no-publish` lets the maintainer iterate on stages without paying the
publish; a later plain `image build` hits every stage and only publishes.
The template stays a cache entry after publish, so `--refresh-from
90-finalize` boots exactly one worker.

First run after upgrading: the existing `sandboxsh/base` carries no build
key, so the first build is a full six-stage build followed by a publish.
The old single builder name `sandboxsh-image-builder-<uid>` is abandoned; a
leftover from a pre-upgrade crash is reported by `doctor` and `image cache
list` (as "legacy builder") but never deleted automatically.

### 5.5 Commands and output

```
sandboxsh image build [--alias A] [--source S] [--refresh] [--refresh-from STAGE]
                      [--no-cache] [--no-publish] [--allow-stale] [--dry-run]
                      [--generation N]
sandboxsh image cache list
sandboxsh image cache prune [--keep-generations N] [--all]
sandboxsh image status [--alias A]
```

`image build` prints one line per stage; the miss line names the reason so
"why did my one-line edit rebuild everything" is answerable from the output:

```
source   debian/13/cloud  a046182b (pinned 2026-09-04)
hit      10-base          a1b2c3d4  age 3d
hit      20-docker-node   b2c3d4e5  age 3d
miss     30-user          c3d4e5f6  script changed
build    30-user          worker sandboxsh-build-c3d4e5f6-9f1e2a ... [streamed output]
build    40-browser       parent changed
build    50-agents        parent changed
build    90-finalize      parent changed
publish  sandboxsh/base   build_key f6a7b8c9 (4 of 6 stages rebuilt, 7m12s)
```

Miss reasons: `script changed`, `inputs changed (uid/gid|build-allow|helpers)`,
`source changed`, `generation bumped`, `parent changed`, `no entry`,
`--no-cache`. `--dry-run` prints the same lines prefixed `[dry-run]` and
issues no state-changing call.

`image cache list`:

```
KEY       STAGE          PARENT    AGE   CHAIN     BUILD-ALLOW  INSTANCE
a1b2c3d4  10-base        -         3d    current   -            sandboxsh-cache-a1b2c3d4
f6a7b8c9  90-finalize    e5f6a7b8  30d   previous  -            sandboxsh-cache-f6a7b8c9
77aa88bb  20-docker-node a1b2c3d4  91d   stale     mirror.example.com  sandboxsh-cache-77aa88bb
```

`CHAIN` is `current`, `previous` (an earlier generation kept by prune
policy), `orphan` (not reachable from any known generation), or `stale`
(beyond the hard age ceiling). `image status` adds: published build key and
source serial, whether the chain is up to date, and otherwise the first
stage the next build would rebuild ("would rebuild from 30-user, 4 stages").

Prune default: delete every entry not on the chain the current inputs would
produce, except entries on the most recent N previous generations (default
1). `--all` keeps only the current chain. Prune reports reclaimed entries;
it cannot report bytes cheaply on Btrfs and does not try.

### 5.6 Concurrency

- One `flock` on `~/.cache/sandboxsh/build/lock` covers **every** `image
  build` and `image cache prune` on the host, regardless of alias. A second
  invocation waits, or fails fast with `--no-wait`. Reason: the build ACL is
  a single shared object (its name derives from the fixed builder config
  path), cache entries are shared across aliases by key, and prune must not
  delete an entry a build is about to copy. Serializing everything removes
  the ACL overwrite/delete race, the worker-name collision, and the prune
  race with one mechanism. Concurrent builds of different aliases on one
  host are not worth a finer lock.
- Worker names still carry a random suffix so a crashed build's leftover can
  never block the next build's creation step.
- Publishes are serialized by Incus itself.
- A partially built entry is never visible: a worker is renamed only after a
  successful stop.

### 5.7 Failure handling

- Stage failure: the worker is deleted and the parent entry stays; retry
  starts from that parent. The error message names the stage, the worker,
  and the parent key. With `SANDBOXSH_KEEP_BUILDER=1` the worker is kept
  under its build name **and the build ACL is kept**, exactly as today for
  the single builder, with the same inspection note: `inspect with: incus
  --project user-<uid> exec sandboxsh-build-<key>-<rand> -- bash; delete it
  and rerun image build afterwards`. The next build deletes a kept worker
  only after printing that it did so.
- Host crash mid-stage: leftover workers are removed on the next build; the
  ACL is re-applied idempotently as today.
- Publish failure (for example ENOSPC on `/`): the template entry exists, so
  the retry is publish-only. The message points at the `doctor` storage
  line.
- Source image gone from the local cache (Incus auto-update replaced it):
  a stage-10 miss runs `incus init images:<fingerprint>`; if the remote no
  longer serves that fingerprint, the build fails and tells the user to run
  `--refresh`.

### 5.8 Root disk sizing

Workers inherit the parent's root size; stage 10 inits with `root,size=`
from the build config (30 GiB today). Block volumes cannot shrink, so a
project with `resources.disk` below the template size fails inside Incus at
creation, as it already does for the published image. Phase 2 adds a
sandboxsh-level check in `create_instance` that reads the template size
from the image property `user.sandboxsh.disk` and rewords the failure.
Consider lowering the builder disk to 16 GiB (guest data is about 4 GiB)
to widen what projects may choose.

## 6. Module interface

New module `src/sandboxsh/imagebuild.py` owns stages, keys, cache, lock,
and the build loop. `Incus` keeps being the only subprocess owner and grows
thin verbs. This leaves `create_instance` as an in-`Incus` workflow while
the build workflow moves out; that asymmetry is deliberate for now (the
build loop is the larger and more testable piece) and is noted in
`docs/architecture.md`.

```python
@dataclass(frozen=True)
class Stage:        name: str; script: Path; floating: bool = True
                    # inputs a stage declares, so a new stage adds its own
                    # key material without touching stage_keys():
                    inputs: Callable[[BuildInputs], tuple[str, ...]] = lambda i: ()

@dataclass(frozen=True)
class BuildInputs:  source_fingerprint: str; architecture: str; uid: int; gid: int
                    stages: tuple[Stage, ...]; helper_hashes: tuple[str, ...]
                    build_allow: tuple[str, ...]; generations: Mapping[str, int]

def stage_keys(inputs: BuildInputs) -> tuple[tuple[Stage, str], ...]        # pure

@dataclass(frozen=True)
class CacheEntry:   key: str; stage: str; parent: str | None; source: str
                    created: datetime; generation: int; build_allow: tuple[str, ...]
                    instance: str

class ImageCache:   # wraps Incus; one filtered `list --format=json` per build
    def __init__(self, incus: Incus, *, now: Callable[[], datetime] = ...)
    def entries(self) -> dict[str, CacheEntry]
    def commit(self, worker, stage, key, parent, inputs) -> CacheEntry
    def discard(self, worker, *, keep: bool) -> None
    def prune(self, keep: set[str]) -> tuple[str, ...]

class ImageBuilder:
    def __init__(self, incus, cache, stages_dir, *, lock: BuildLock, now=...)
    def plan(self, alias, source, *, refresh, refresh_from, no_cache, generation) -> BuildPlan
    def run(self, plan: BuildPlan, *, publish: bool, keep_failed: bool) -> BuildReport
```

`BuildPlan` is a value: key chain, hit/miss per stage with reason, deepest
hit, age verdict, what would be published, `is_noop`. `--dry-run` prints it.
`BuildLock` is a small host-side helper (flock) injected into the builder;
it does not belong in `Incus`, matching how `_prime_sudo` keeps host
coordination out of `Incus`.

`Incus` additions: `copy_instance(src, dst, *, config)`, `rename_instance`,
`list_instances(prefix)`, `resolve_vm_image(alias)` (`image info --vm`),
`image_property(alias, key)`, `publish(instance, alias, properties)`.
`build_image()` becomes a thin wrapper keeping its signature for the CLI.

Tests (pytest):

- Replace the positional `FakeRunner` queue for these tests with a keyed
  fake: command-prefix to response, so a six-stage loop with several
  commands per stage does not depend on counting. Add a `cache_listing(
  (key, stage, parent, created), ...)` fixture that renders the
  `incus list --format=json` payload.
- `stage_keys`: editing stage N changes keys N.. and nothing before; uid
  change leaves stages 10/20 alone; global generation changes only floating
  stages; `build_allow` changes every floating stage.
- Stage loader: ordering, rejection of non-`NN-` names, optional stages
  inserted before finalize, cloud-init contract lines asserted.
- Builder: no-op build issues exactly one `image info` and no `sudo`; a miss
  at stage 3 copies from the stage-2 entry and never calls `init`; commit
  stamps then renames; a failing stage deletes the worker and leaves the
  parent; `keep_failed` keeps worker and ACL; `--no-publish` never calls
  `publish`; publish passes the build-key property; `--dry-run` issues no
  state-changing command; age warning and refusal with an injected clock.
- Lock: a `FakeLock` that blocks or raises; prune takes the lock.
- Guest: every `guest/stages/*.sh` passes `bash -n`; the invariants in
  `tests/test_guest_provision.py` are restructured, not re-pointed: the
  `runuser` body lives in `50-agents.sh` and the profile.d heredoc in
  `90-finalize.sh`, so the current single-file split helper is replaced by
  two file-scoped helpers.
- `doctor` storage lines with fake `incus storage info` output.

## 7. Security

- Trusted build inputs stay the set the configurable-provisioning plan
  names: packaged `guest/stages/*`, `~/.config/sandboxsh/build.json`, CLI
  flags, `SANDBOXSH_BUILD_ALLOW`. Never `.sandboxsh.json`, never files from
  a mounted project, never the contents of a cache entry (a stage script is
  pushed fresh from the host on every run; nothing already inside the
  worker is executed as the next step).
- A cache entry is a stopped VM that only ever booted under the build ACL
  with no host mounts, no credentials volume, no project workspace. Its
  trust level equals today's builder. Copies never inherit devices beyond
  the profile NIC and root disk; the builder passes no `-d` from user input.
- Workers attach the ACL before their first start, for both `init` and
  `copy` workers. A copied worker inherits the parent's eth0 override
  naming the same ACL; `apply_acl` has recreated it before the worker starts,
  and if it were missing Incus fails the start closed.
- The reachable-host set is a build input: extra build-allow hosts enter
  the key and are stamped on the entry (section 5.3).
- Hard age ceiling (section 5.3) bounds how long base OS, Docker, and Chrome
  can be reused without a refresh.
- The no-op path performs no privileged call; the build-key property is
  host-authoritative and unreachable from any guest (the guest cannot see
  the Incus socket, asserted by `_verify_guest`). `_prime_sudo` runs only
  after the plan says it is not a no-op.
- Names embed only hex keys and packaged stage stems; CLI strings never
  reach instance names. Only `user.*` keys are stamped on entries.
- `image cache prune` deletes only instances carrying
  `user.sandboxsh.cache.key`, never `ss-*`, and holds the build lock.

## 8. Separate tracks (not part of this feature)

- **`doctor` storage diagnostics**: driver, loop backing, pool free space,
  free space on the filesystem behind `/var/lib/incus/images`, a legacy
  builder leftover, and a WARN quoting the Incus Btrfs-VM statement. Point
  the admin at `storage.images_volume` so publish temp files leave `/`.
- **Pool migration**: an admin creates a second pool (ZFS on a real device,
  or LVM thin on a partition; loop-backed ZFS if no device is free); the
  builder and `create_instance` pass `root,pool=<pool>` from
  `SANDBOXSH_STORAGE_POOL`. Existing VMs stay; moving one is an explicit
  `incus move --storage` by the user (full copy, VM stopped). Never migrate
  automatically. LVM thin gives native block volumes; ZFS gives the best
  snapshot/clone tooling and `zfs.clone_copy` control; Btrfs stays
  acceptable for now.
- **Stray `debian` user**: remove in finalize or via cloud-init config;
  surfaced by this review, unrelated to caching.
- **Per-project image caching** (configurable-provisioning Layer 4): would
  need its own approval gate, like mounts and firewall, because project
  setup is guest-writable. Out of scope.

## 9. Phases

**Phase 0, measure. Done 2026-09-04 by the maintainer.**

| Step | Time |
|------|------|
| Full `sandboxsh image build` (provision + publish) | 8m13s |
| Provisioning alone (builder boot to "provisioned") | about 5m45s |
| `incus publish` of an instance-only copy of a project VM (40 GiB disk, ~4 GiB data) | 1m45s |
| First `incus init` from the fresh image (unpack) | 18.5s |
| Second `incus init` from the same image (CoW clone) | 0.17s |

Publish plus first unpack is about two minutes of an eight-minute build;
every later project creation is already a sub-second clone. Stage caching
targets the six-minute provisioning part. Direct template cloning (Phase 3)
could save at most about two minutes per *changed* build and 18 s on the
first project creation after a build. **Decision: keep publish; Phase 3 is
shelved unless the build changes shape.** The run also confirmed that
`incus copy --instance-only` of a project VM works in the restricted
project.

**Phase 1, stage split (1 day).** Move `provision.sh` into `guest/stages/`;
`build_image()` pushes and runs them in order in one worker, one boot,
unchanged otherwise. No cloud-init change yet. Restructure
`tests/test_guest_provision.py`, update README "Golden image contents" and
`docs/architecture.md`, and rewrite `docs/plan-configurable-provisioning.md`
Layer 1/2 as "core stages and optional stages".

**Phase 2, cache (2 to 3 days).** `imagebuild.py`, `Incus` verbs, CLI
flags, output formats, `image cache list|prune`, no-op path, lock, cloud-init
contract, disk-size check, tests. Keep publish. README: quickstart comment
("first build takes minutes, later builds seconds"), `SANDBOXSH_BUILD_ALLOW`
and `SANDBOXSH_KEEP_BUILDER` paragraphs rewritten for workers, rollback via
`--generation`.

**Phase 3, optional direct clone (only with Phase 0 evidence).**
`create_instance` copies from the template when the alias maps to one;
`assert_immutable_config` learns the template key; `recreate` uses the
same path; publish becomes opt-in for portability.

**Phase 4, separate tracks** as in section 8.

## 10. Acceptance criteria

- Unchanged inputs: `sandboxsh image build` exits 0 in under 5 s, creates no
  instance, prompts for no password.
- Editing only `90-finalize.sh` boots exactly one worker, runs only that
  stage, and publishes; no apt, Docker, Chrome, or Playwright install runs
  (asserted from the per-stage output lines and in unit tests).
- A failing stage leaves its parent; the rerun starts at that stage.
- `--no-cache` produces a working image that passes the manual E2E
  checklist (`up`, Docker Compose, Chrome, Playwright, agents, pins) and
  contains no `debian` uid 1001 regression beyond today's.
- `image cache list` shows every entry with age, chain state, and
  build-allow; `prune` removes only off-chain entries and reports them.
- Pool usage after two builds with one changed stage grows by roughly that
  stage's delta, not by another full image.
- Cache entries and workers are never touched by `up`, `shell`, `exec`,
  `status`, `destroy`.
- An entry built with `SANDBOXSH_BUILD_ALLOW` set is never reused by a build
  without it.
- Phase 1 alone: the image built from the split stages is byte-for-byte the
  same package set (`dpkg -l`, `pnpm ls -g`, `uv tool list`) as one built
  from the monolithic script on the same day.
- Docs updated as listed in section 9.

## 11. Trade-offs and open decisions

- **Stopped instances vs a project change.** Instances add rows to `incus
  list`. Snapshots would be tidier but need `restricted.snapshots=allow` per
  user project via `sudo`; revisit if `incus-user` ever allows snapshots by
  default.
- **One global lock.** Simplest correct option; loses concurrent builds of
  different aliases on one host, which nobody needs today.
- **Stage count.** More stages mean more boot/stop cycles on a cold build
  (about 20 to 40 s each) but finer reuse. Six is the proposal; merging
  later does not change keys of unaffected stages.
- **Refresh generations are host-local.** Two hosts share no cache anyway.
- **Publish kept.** Measured at 1m45s plus an 18 s first unpack, against
  about six minutes of provisioning. Not worth a second creation path.
- **Btrfs.** Not fixed here; only not made worse (fewer full-image writes,
  no large rsync copies).

## 12. Review findings folded in

- Critic: shared build ACL raced across concurrent alias builds (fixed by
  the global lock); cloud-init wait must stay before hosts pinning in every
  stage (restored in 5.2); worker names collide at creation (random suffix);
  copy re-checks project restrictions (facts table, `user.*`-only stamps);
  publish colon caveat; disk-size wording.
- Architect: worker naming, cloud-init contract moved out of Phase 1,
  filtered cache listing, orchestration asymmetry noted, lock ownership in
  the builder, per-stage publish alternative stated, Layer 4 needs its own
  approval gate.
- Security: build-allow hosts in the key and stamped; hard age ceiling;
  prune under the build lock. ACL ordering, no-op path, and naming
  confirmed sound.
- Developer experience: output formats for build, dry-run, cache list,
  status, and failures; keyed fake runner, listing fixture, clock and lock
  injection; `SANDBOXSH_KEEP_BUILDER` keeps worker and ACL; legacy builder
  name reported not deleted; first run is a full build; guest tests
  restructured; `--refresh-from` takes the stage stem; `--generation`
  documented; stage inputs declared on the `Stage`.
