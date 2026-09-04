# Future improvement: configurable provisioning

Status: proposed (2026-08-14). Not yet implemented.

## Problem

Everything opinionated about the sandbox environment is baked into
`guest/provision.sh`, which mixes three different kinds of content:

1. **Mechanism** — the `dev` user and uid remap, Docker daemon, the git
   pre-push guard, agent-seed plumbing, installing `agent-init.sh` /
   `instance-init.sh`. Security- and correctness-relevant; must stay fixed.
2. **Personal toolchain** — `plonecli`, `mistral-vibe`, `tox`, `invoke`, the
   LDAP/cairo/pango apt packages, the specific pi plugins. Useful for one
   maintainer, noise for everyone else.
3. **Generally useful defaults** — Node 24 + pnpm, uv, ripgrep/fd/jq,
   Claude Code.

Because (2) is hardcoded, the repo is only directly useful to people with the
same stack. The goal is to keep (1) and (3) as the shipped default and make
everything else configurable — per host (baked into the golden image) and per
project (installed on create/recreate).

Existing plumbing already points this way:

- `.sandboxsh.json` has an `image` field, so projects can already select
  among multiple golden images.
- `sandboxsh image build` takes `--alias` and `--source`.
- The default firewall allowlist (`security.py`, `DEFAULT_ENDPOINTS`) covers
  `deb.debian.org`, `pypi.org`, and `registry.npmjs.org`, so package installs
  inside a running VM work even with the firewall enabled.

## Design: four layers

### Layer 1 — core stages

The build is already an ordered chain of stage scripts in `guest/stages/`
(see `docs/plan-incremental-image-build.md`), each cached as a stopped VM keyed
by its script and inputs. The core stages keep only mechanism plus
near-universal basics: git, curl, build-essential, python3, uv, the agents,
Docker, Node/pnpm. Everything Plone/LDAP/cairo-specific moves out of
`10-base.sh` and `50-agents.sh` into an optional stage (layer 2).

### Layer 2 — optional stages (baked into the image)

A directory of small, self-contained optional stage scripts, named like the
core ones so they sort into the chain before `90-finalize.sh`:

```text
guest/features/
  60-node.sh
  60-plone.sh
  60-pdf-export.sh
  ...
```

`load_stages(stages_dir, extra=...)` already inserts such scripts before
finalize; an optional stage is one more link in the key chain, so selecting
or editing one rebuilds only it and the stages after it. Which optional stages
get baked into an image is chosen by *host* configuration:

- CLI: `sandboxsh image build --features node,plone`
- Host config `~/.config/sandboxsh/build.json`:

  ```json
  {
    "features": ["node", "plone"],
    "apt": ["graphviz"],
    "uv_tools": ["tox"],
    "scripts": ["~/my-extra-provisioning.sh"]
  }
  ```

Combined with `--alias`, a host can maintain several golden images
(`sandboxsh/base`, `sandboxsh/plone`, …) and point each project at one via the
existing `image` field.

**Security constraint:** image-build scripts run as root in the builder VM.
Their inputs must therefore come only from host-trusted locations (repo
`guest/features/`, `~/.config/sandboxsh/`, explicit CLI flags) — never from
the guest-writable project tree or `.sandboxsh.json`. An agent editing the
repo must not be able to inject code into the next image build.

### Layer 3 — project-based packages, installed on create/recreate

New optional `.sandboxsh.json` section, executed *inside* the VM as `dev`
after `instance-init`:

```json
"setup": {
  "apt": ["libpq-dev", "graphviz"],
  "uv_tools": ["tox"],
  "script": "./sandbox-setup.sh"
}
```

- Running it inside the guest is safe by construction: the VM is already the
  untrusted, agent-controlled zone, and the firewall allowlist already
  permits the package registries.
- `setup` must **not** enter the immutable fingerprint
  (`ProjectConfig._immutable_document`) — that would force VM replacement on
  every package tweak. Instead, stamp a hash of the setup section on the VM
  (alongside the existing fingerprint stamp) and rerun setup on `up`/`sync`
  when the hash changes.
- Reruns are acceptable because the operations are idempotent
  (`apt-get install`, `uv tool install`); `script` is documented as
  must-be-idempotent.
- `apt` entries run via `sudo apt-get install`; package names are validated
  with the same `_safe_text`-style checks as other config fields.

### Layer 4 (deferred) — per-project image caching

If a project's setup gets slow, something like
`sandboxsh image build --from sandboxsh/base --setup-from ./project` could
snapshot a project-specific image so recreate stays fast. Deferred until
layer 3's rerun cost actually hurts; the `image` field already gives projects
a manual escape hatch.

## Implementation sketch (layers 1–3, one pass)

1. Move the Plone/LDAP/PDF/mistral-vibe/pi-plugin content out of
   `guest/stages/10-base.sh` and `50-agents.sh` into `guest/features/60-plone.sh`
   (and friends).
2. `imagebuild.py` `ImageBuilder.stages()`: resolve the feature list from
   `--features` and `~/.config/sandboxsh/build.json`, pass the selected
   scripts to `load_stages(extra=...)`, and record the feature set in the
   `user.sandboxsh.stages` image property `image status` already reads.
3. `config.py`: parse the `setup` block (new allowed top-level key, typed
   validation like `ports`/`firewall`), expose a `setup_fingerprint`.
4. Create/recreate/up path: after `instance-init`, compare the stamped setup
   hash, run installs + script as `dev` in the workdir when stale, restamp.
5. Docs/README: describe build config, features, and the `setup` block;
   update `examples/.sandboxsh.json`.

End state for the current maintainer: a `plone` feature enabled in the host
build config plus per-project `setup` blocks — nothing personal left in the
repo defaults.
