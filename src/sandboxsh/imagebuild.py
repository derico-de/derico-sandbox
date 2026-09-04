"""Incremental golden-image builds.

The image is built as an ordered chain of stage scripts (`guest/stages/`).
Every completed stage is kept as a stopped Incus VM in the user project, keyed
by a hash of everything that went into it, and the next build resumes from the
deepest entry whose key still matches. Incus itself is the cache database: a
stopped `sandboxsh-cache-<key>` instance stamped with `user.sandboxsh.cache.*`
keys is an entry, and nothing on the host is authoritative about it.

Design notes and the measurements behind them live in
`docs/plan-incremental-image-build.md`.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import FirewallEntry, ProjectConfig, Resources
from .errors import SandboxshError
from .incus import Incus, ResolvedImage
from .security import AclPolicy

DEFAULT_ALIAS = "sandboxsh/base"
DEFAULT_SOURCE = "images:debian/13/cloud"

CACHE_PREFIX = "sandboxsh-cache-"
WORKER_PREFIX = "sandboxsh-build-"
LEGACY_BUILDER_PREFIX = "sandboxsh-image-builder-"
CACHE_CONFIG_PREFIX = "user.sandboxsh.cache."

BUILD_KEY_PROPERTY = "user.sandboxsh.build_key"
SOURCE_PROPERTY = "user.sandboxsh.source"
STAGES_PROPERTY = "user.sandboxsh.stages"
DISK_PROPERTY = "user.sandboxsh.disk"

# Guest helpers installed by the finalize stage; their content is part of its key.
HELPER_FILES = ("agent-init.sh", "instance-init.sh")
FINALIZE_STAGE = "90-finalize"
BUILD_RESOURCES = Resources(cpus=4, memory="8GiB", disk="30GiB")

# Cross-stage cloud-init contract: workers boot more than once, and cloud-init
# must not treat every copied worker as a new machine. The first stage switches
# it off after its own first boot; finalize switches it back on for the image.
CLOUD_INIT_DISABLE = "touch /etc/cloud/cloud-init.disabled"
CLOUD_INIT_ENABLE = "rm -f /etc/cloud/cloud-init.disabled"
CLOUD_INIT_CLEAN = "cloud-init clean --logs --machine-id"

DEFAULT_MAX_AGE_DAYS = 30
STALE_MULTIPLIER = 3
MANIFEST_VERSION = 1

_STAGE_FILE_RE = re.compile(r"^\d{2}-[a-z0-9]+(?:-[a-z0-9]+)*\.sh$")
_KEY_RE = re.compile(r"^[0-9a-f]{16}$")

Echo = Callable[[str], None]


def _no_inputs(inputs: BuildInputs) -> tuple[str, ...]:
    return ()


@dataclass(frozen=True)
class Stage:
    name: str
    script: Path
    floating: bool = True
    # Key material a stage declares beyond its script: a new stage with new
    # inputs adds them here without touching `stage_keys()`.
    inputs: Callable[[BuildInputs], tuple[str, ...]] = _no_inputs
    inputs_label: str = ""
    # Command-line arguments the script receives inside the worker.
    arguments: Callable[[BuildInputs], tuple[str, ...]] = _no_inputs
    # Helper files pushed next to the script.
    files: tuple[str, ...] = ()


def _user_inputs(inputs: BuildInputs) -> tuple[str, ...]:
    return (f"uid={inputs.uid}", f"gid={inputs.gid}")


def _user_arguments(inputs: BuildInputs) -> tuple[str, ...]:
    return (str(inputs.uid), str(inputs.gid))


def _helper_inputs(inputs: BuildInputs) -> tuple[str, ...]:
    return tuple(f"helper={digest}" for digest in inputs.helper_hashes)


# Stage-specific declarations; a stage absent here is floating with no inputs.
STAGE_DECLARATIONS: Mapping[str, Mapping[str, object]] = {
    "30-user": {"inputs": _user_inputs, "inputs_label": "uid/gid", "arguments": _user_arguments},
    FINALIZE_STAGE: {
        "floating": False,
        "inputs": _helper_inputs,
        "inputs_label": "helpers",
        "files": HELPER_FILES,
    },
}


def packaged_guest_dir() -> Path:
    packaged = Path(__file__).parent / "guest"
    if packaged.is_dir():
        return packaged
    # Editable source install; wheel installs use the packaged path.
    return Path(__file__).parents[2] / "guest"


def load_stages(stages_dir: Path, extra: tuple[Path, ...] = ()) -> tuple[Stage, ...]:
    """The stage chain: every `NN-name.sh` in order, optional stages before finalize."""
    if not stages_dir.is_dir():
        raise SandboxshError(f"packaged stage directory is missing: {stages_dir}")
    core = []
    for path in sorted(stages_dir.iterdir()):
        if not _STAGE_FILE_RE.match(path.name):
            raise SandboxshError(
                f"stage script has an invalid name: {path.name} (expected NN-name.sh)"
            )
        core.append(_stage(path))
    if not core or not core[0].name.startswith("10-"):
        raise SandboxshError(f"the first stage in {stages_dir} must be a 10-* script")
    if core[-1].name != FINALIZE_STAGE:
        raise SandboxshError(f"the last stage in {stages_dir} must be {FINALIZE_STAGE}.sh")
    optional = []
    for path in extra:
        if not _STAGE_FILE_RE.match(path.name):
            raise SandboxshError(
                f"optional stage has an invalid name: {path.name} (expected NN-name.sh)"
            )
        if not path.is_file():
            raise SandboxshError(f"optional stage is missing: {path}")
        optional.append(_stage(path))
    stages = tuple(sorted(core[:-1] + optional, key=lambda stage: stage.name) + [core[-1]])
    names = [stage.name for stage in stages]
    if len(set(names)) != len(names):
        raise SandboxshError("stage names must be unique: " + ", ".join(names))
    _verify_cloud_init_contract(stages)
    return stages


def _stage(path: Path) -> Stage:
    declared = dict(STAGE_DECLARATIONS.get(path.stem, {}))
    return Stage(name=path.stem, script=path, **declared)  # type: ignore[arg-type]


def _verify_cloud_init_contract(stages: tuple[Stage, ...]) -> None:
    first = stages[0].script.read_text()
    if CLOUD_INIT_DISABLE not in first:
        raise SandboxshError(
            f"{stages[0].script.name} must end with `{CLOUD_INIT_DISABLE}`; without it "
            "every later build worker re-runs cloud-init as a new machine"
        )
    last = stages[-1].script.read_text()
    for line in (CLOUD_INIT_ENABLE, CLOUD_INIT_CLEAN):
        if line not in last:
            raise SandboxshError(f"{stages[-1].script.name} must run `{line}`")
    if last.index(CLOUD_INIT_ENABLE) > last.index(CLOUD_INIT_CLEAN):
        raise SandboxshError(
            f"{stages[-1].script.name} must re-enable cloud-init before cleaning it"
        )


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode())
        digest.update(b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class BuildInputs:
    source_fingerprint: str
    architecture: str
    uid: int
    gid: int
    stages: tuple[Stage, ...]
    helper_hashes: tuple[str, ...]
    build_allow: tuple[str, ...]
    generation: int = 0
    stage_generations: Mapping[str, int] = field(default_factory=dict)

    @property
    def source_key(self) -> str:
        return _digest("source", self.source_fingerprint, self.architecture)


@dataclass(frozen=True)
class StageKey:
    stage: Stage
    key: str
    parent: str | None
    script: str
    inputs: str
    generation: int
    stage_generation: int


def stage_keys(inputs: BuildInputs) -> tuple[StageKey, ...]:
    """The key chain for these inputs. Pure: no Incus, no clock."""
    keys: list[StageKey] = []
    parent: str | None = None
    for stage in inputs.stages:
        script = file_hash(stage.script)
        declared = _digest(*stage.inputs(inputs))
        material = [parent or inputs.source_key, stage.name, script, declared]
        generation = inputs.generation if stage.floating else 0
        stage_generation = inputs.stage_generations.get(stage.name, 0) if stage.floating else 0
        if stage.floating:
            material.extend(
                (
                    f"generation={generation}",
                    f"stage-generation={stage_generation}",
                    "build-allow=" + ",".join(inputs.build_allow),
                )
            )
        key = _digest(*material)[:16]
        keys.append(StageKey(stage, key, parent, script, declared, generation, stage_generation))
        parent = key
    return tuple(keys)


@dataclass(frozen=True)
class CacheEntry:
    key: str
    stage: str
    parent: str | None
    source: str
    created: datetime
    generation: int
    build_allow: tuple[str, ...]
    instance: str
    script: str = ""
    inputs: str = ""
    stage_generation: int = 0

    def age(self, now: datetime) -> timedelta:
        return max(now - self.created, timedelta(0))


def format_age(age: timedelta) -> str:
    seconds = int(age.total_seconds())
    if seconds >= 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return "<1m"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ImageCache:
    """Cache entries as stopped instances; one filtered listing per build."""

    def __init__(self, incus: Incus, *, now: Callable[[], datetime] = _utcnow) -> None:
        self.incus = incus
        self.now = now

    def entries(self) -> dict[str, CacheEntry]:
        entries: dict[str, CacheEntry] = {}
        for record in self.incus.list_instances(CACHE_PREFIX):
            entry = self._entry(record)
            if entry is not None:
                entries[entry.key] = entry
        return entries

    @staticmethod
    def _entry(record: Mapping) -> CacheEntry | None:
        config = record.get("config") or {}
        stamped = {
            key[len(CACHE_CONFIG_PREFIX) :]: str(value)
            for key, value in config.items()
            if key.startswith(CACHE_CONFIG_PREFIX)
        }
        key = stamped.get("key", "")
        if not _KEY_RE.match(key) or "stage" not in stamped:
            return None
        try:
            created = datetime.fromisoformat(stamped.get("created", ""))
        except ValueError:
            created = datetime.fromtimestamp(0, tz=UTC)
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        build_allow = tuple(host for host in stamped.get("build_allow", "").split(",") if host)
        return CacheEntry(
            key=key,
            stage=stamped["stage"],
            parent=stamped.get("parent") or None,
            source=stamped.get("source", ""),
            created=created,
            generation=_int(stamped.get("generation")),
            build_allow=build_allow,
            instance=str(record.get("name", "")),
            script=stamped.get("script", ""),
            inputs=stamped.get("inputs", ""),
            stage_generation=_int(stamped.get("stage_generation")),
        )

    def workers(self) -> tuple[str, ...]:
        return tuple(str(record.get("name")) for record in self.incus.list_instances(WORKER_PREFIX))

    def legacy_builders(self) -> tuple[str, ...]:
        return tuple(
            str(record.get("name")) for record in self.incus.list_instances(LEGACY_BUILDER_PREFIX)
        )

    def commit(
        self,
        worker: str,
        step: StageKey,
        *,
        source: str,
        build_allow: tuple[str, ...],
        existing: CacheEntry | None = None,
    ) -> CacheEntry:
        """Stamp a stopped worker and rename it into place; the rename is the visibility point."""
        created = self.now()
        stamps = {
            "key": step.key,
            "stage": step.stage.name,
            "parent": step.parent or "",
            "source": source,
            "created": created.isoformat(timespec="seconds"),
            "generation": str(step.generation),
            "stage_generation": str(step.stage_generation),
            "build_allow": ",".join(build_allow),
            "script": step.script,
            "inputs": step.inputs,
        }
        self.incus.set_config(
            worker, {f"{CACHE_CONFIG_PREFIX}{name}": value for name, value in stamps.items()}
        )
        instance = f"{CACHE_PREFIX}{step.key}"
        if existing is not None:
            # `--no-cache` rebuilt an entry that still exists; the new one replaces it.
            self.incus.delete_instance(existing.instance)
        self.incus.rename_instance(worker, instance)
        return CacheEntry(
            key=step.key,
            stage=step.stage.name,
            parent=step.parent,
            source=source,
            created=created,
            generation=step.generation,
            build_allow=build_allow,
            instance=instance,
            script=step.script,
            inputs=step.inputs,
            stage_generation=step.stage_generation,
        )

    def discard(self, worker: str, *, keep: bool) -> None:
        if not keep:
            self.incus.delete_instance(worker)

    def prune(self, keep: set[str]) -> tuple[str, ...]:
        """Delete every entry not in `keep`. Only stamped cache entries are ever deleted."""
        removed = []
        for key, entry in sorted(self.entries().items()):
            if key in keep:
                continue
            self.incus.delete_instance(entry.instance, check=True)
            removed.append(key)
        return tuple(removed)


def _int(value: str | None) -> int:
    try:
        return int(value or 0)
    except ValueError:
        return 0


@dataclass
class PinnedSource:
    fingerprint: str
    architecture: str
    serial: str
    pinned: str  # ISO date

    @classmethod
    def from_resolved(cls, image: ResolvedImage, now: datetime) -> PinnedSource:
        return cls(image.fingerprint, image.architecture, image.serial, now.date().isoformat())


@dataclass
class Manifest:
    """Host-side refresh generations and pinned sources; losing it is harmless."""

    path: Path
    generation: int = 0
    stage_generations: dict[str, int] = field(default_factory=dict)
    sources: dict[str, PinnedSource] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Manifest:
        manifest = cls(path)
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return manifest
        if not isinstance(data, dict):
            return manifest
        manifest.generation = _int(str(data.get("generation", 0)))
        stage_generations = data.get("stage_generations") or {}
        if isinstance(stage_generations, dict):
            manifest.stage_generations = {
                str(name): _int(str(value)) for name, value in stage_generations.items()
            }
        sources = data.get("sources") or {}
        if isinstance(sources, dict):
            for alias, value in sources.items():
                if isinstance(value, dict) and value.get("fingerprint"):
                    manifest.sources[str(alias)] = PinnedSource(
                        fingerprint=str(value["fingerprint"]),
                        architecture=str(value.get("architecture", "")),
                        serial=str(value.get("serial", "")),
                        pinned=str(value.get("pinned", "")),
                    )
        return manifest

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document = {
            "version": MANIFEST_VERSION,
            "generation": self.generation,
            "stage_generations": dict(sorted(self.stage_generations.items())),
            "sources": {
                alias: {
                    "fingerprint": source.fingerprint,
                    "architecture": source.architecture,
                    "serial": source.serial,
                    "pinned": source.pinned,
                }
                for alias, source in sorted(self.sources.items())
            },
        }
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(document, indent=2) + "\n")
        temporary.replace(self.path)

    def copy(self) -> Manifest:
        return Manifest(
            self.path,
            self.generation,
            dict(self.stage_generations),
            {alias: replace(source) for alias, source in self.sources.items()},
        )


class BuildLock:
    """One host-wide flock for every build and prune.

    The build ACL is a single shared object, cache entries are shared across
    aliases by key, and prune must not delete an entry a build is about to
    copy. One lock removes all three races.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def held(self, *, wait: bool = True) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w") as handle:
            flags = fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB)
            try:
                fcntl.flock(handle, flags)
            except BlockingIOError as exc:
                raise SandboxshError(
                    "another `sandboxsh image build` or `image cache prune` holds the build "
                    f"lock ({self.path}); wait for it or rerun without --no-wait"
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


@dataclass(frozen=True)
class PlanStep:
    key: StageKey
    hit: bool
    reason: str = ""
    entry: CacheEntry | None = None

    @property
    def stage(self) -> Stage:
        return self.key.stage


@dataclass(frozen=True)
class BuildPlan:
    alias: str
    source: str
    pinned: PinnedSource
    inputs: BuildInputs
    steps: tuple[PlanStep, ...]
    published_key: str | None
    manifest: Manifest
    no_cache: bool
    now: datetime
    max_age_days: int
    dirty_manifest: bool
    entries: Mapping[str, CacheEntry] = field(default_factory=dict)

    @property
    def build_key(self) -> str:
        return self.steps[-1].key.key

    @property
    def is_noop(self) -> bool:
        return not self.no_cache and self.published_key == self.build_key

    @property
    def deepest_hit(self) -> int:
        """Index of the last cache hit, or -1 for a build from the source image."""
        index = -1
        for position, step in enumerate(self.steps):
            if not step.hit:
                break
            index = position
        return index

    @property
    def to_build(self) -> tuple[PlanStep, ...]:
        return self.steps[self.deepest_hit + 1 :]

    @property
    def first_rebuild(self) -> Stage | None:
        remaining = self.to_build
        return remaining[0].stage if remaining else None

    @property
    def reused_entry(self) -> CacheEntry | None:
        index = self.deepest_hit
        return self.steps[index].entry if index >= 0 else None

    def age_verdict(self) -> tuple[str, timedelta | None]:
        """`ok`, `warn`, or `stale` for the deepest reused entry."""
        entry = self.reused_entry
        if entry is None:
            return "ok", None
        age = entry.age(self.now)
        if age > timedelta(days=self.max_age_days * STALE_MULTIPLIER):
            return "stale", age
        if age > timedelta(days=self.max_age_days):
            return "warn", age
        return "ok", age

    def lines(self) -> list[str]:
        short_source = self.source.split(":", 1)[-1]
        lines = [
            f"source   {short_source:<16} {self.pinned.fingerprint[:8]} "
            f"(pinned {self.pinned.pinned or 'now'})"
        ]
        if self.is_noop:
            lines.append(f"up to date: {self.alias} carries build_key {self.build_key}")
            return lines
        for step in self.steps:
            name = step.stage.name
            if step.hit and step.entry is not None:
                age = format_age(step.entry.age(self.now))
                lines.append(f"hit      {name:<16} {step.key.key}  age {age}")
            else:
                lines.append(f"miss     {name:<16} {step.key.key}  {step.reason}")
        verdict, age = self.age_verdict()
        if verdict != "ok" and age is not None:
            lines.append(
                f"{verdict:<8} reused entry is {format_age(age)} old "
                f"(warn after {self.max_age_days}d, refuse after "
                f"{self.max_age_days * STALE_MULTIPLIER}d)"
            )
        return lines


@dataclass(frozen=True)
class BuildReport:
    plan: BuildPlan
    policy: AclPolicy | None = None
    published: bool = False
    rebuilt: int = 0
    elapsed: float = 0.0
    dry_run: bool = False

    @property
    def noop(self) -> bool:
        return self.plan.is_noop


def build_allow_from_env(environ: Mapping[str, str] | None = None) -> tuple[str, ...]:
    raw = (environ if environ is not None else os.environ).get("SANDBOXSH_BUILD_ALLOW", "")
    return tuple(sorted({host.strip() for host in raw.split(",") if host.strip()}))


def build_project_config(source: str, build_allow: tuple[str, ...]) -> ProjectConfig:
    """The build ACL: every stage runs under the same host-enforced allowlist."""
    return ProjectConfig(
        path=Path.home() / ".config/sandboxsh/image-builder.json",
        name="image-builder",
        workdir="/root",
        mounts=(),
        ports=(),
        firewall_enabled=True,
        firewall_allow=(
            FirewallEntry("claude.ai"),
            # claude.ai/install.sh fetches the actual binary from here.
            FirewallEntry("downloads.claude.ai"),
            FirewallEntry("pi.dev"),
            FirewallEntry("storage.googleapis.com"),
            *(FirewallEntry(host) for host in build_allow),
        ),
        resources=BUILD_RESOURCES,
        image=source,
        agent_credentials=False,
    )


def _max_age_days(environ: Mapping[str, str]) -> int:
    raw = environ.get("SANDBOXSH_CACHE_MAX_AGE_DAYS", "")
    try:
        return max(int(raw), 1)
    except ValueError:
        return DEFAULT_MAX_AGE_DAYS


class ImageBuilder:
    def __init__(
        self,
        incus: Incus,
        cache: ImageCache | None = None,
        stages_dir: Path | None = None,
        *,
        guest_dir: Path | None = None,
        lock: BuildLock | None = None,
        state_dir: Path | None = None,
        now: Callable[[], datetime] = _utcnow,
        echo: Echo = print,
        uid: int | None = None,
        gid: int | None = None,
        build_allow: tuple[str, ...] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.incus = incus
        self.now = now
        self.cache = cache or ImageCache(incus, now=now)
        self.guest_dir = guest_dir or packaged_guest_dir()
        self.stages_dir = stages_dir or self.guest_dir / "stages"
        environment = dict(environ if environ is not None else os.environ)
        if state_dir is None:
            cache_home = Path(environment.get("XDG_CACHE_HOME") or Path.home() / ".cache")
            state_dir = cache_home / "sandboxsh" / "build"
        self.state_dir = state_dir
        self.manifest_path = state_dir / "manifest.json"
        self.lock = lock or BuildLock(state_dir / "lock")
        self.echo = echo
        self.uid = os.getuid() if uid is None else uid
        self.gid = os.getgid() if gid is None else gid
        self.build_allow = build_allow_from_env(environment) if build_allow is None else build_allow
        self.max_age_days = _max_age_days(environment)
        self.keep_failed = environment.get("SANDBOXSH_KEEP_BUILDER") == "1"

    # -- planning -----------------------------------------------------------

    def stages(self) -> tuple[Stage, ...]:
        return load_stages(self.stages_dir)

    def _helper_hashes(self) -> tuple[str, ...]:
        hashes = []
        for filename in HELPER_FILES:
            path = self.guest_dir / filename
            if not path.is_file():
                raise SandboxshError(f"packaged guest script is missing: {path}")
            hashes.append(file_hash(path))
        return tuple(hashes)

    def _pin_source(
        self, manifest: Manifest, source: str, *, refresh: bool
    ) -> tuple[PinnedSource, bool]:
        pinned = manifest.sources.get(source)
        if pinned is not None and not refresh:
            return pinned, False
        resolved = self.incus.resolve_vm_image(source)
        pinned = PinnedSource.from_resolved(resolved, self.now())
        manifest.sources[source] = pinned
        return pinned, True

    def inputs_for(self, manifest: Manifest, pinned: PinnedSource) -> BuildInputs:
        return BuildInputs(
            source_fingerprint=pinned.fingerprint,
            architecture=pinned.architecture,
            uid=self.uid,
            gid=self.gid,
            stages=self.stages(),
            helper_hashes=self._helper_hashes(),
            build_allow=self.build_allow,
            generation=manifest.generation,
            stage_generations=dict(manifest.stage_generations),
        )

    def plan(
        self,
        alias: str = DEFAULT_ALIAS,
        source: str = DEFAULT_SOURCE,
        *,
        refresh: bool = False,
        refresh_from: str | None = None,
        no_cache: bool = False,
        generation: int | None = None,
    ) -> BuildPlan:
        manifest = Manifest.load(self.manifest_path).copy()
        dirty = False
        if generation is not None:
            if generation < 0:
                raise SandboxshError("--generation must not be negative")
            dirty = dirty or manifest.generation != generation
            manifest.generation = generation
        if refresh:
            manifest.generation += 1
            dirty = True
        stages = self.stages()
        if refresh_from is not None:
            names = [stage.name for stage in stages]
            if refresh_from not in names:
                raise SandboxshError(
                    f"unknown stage {refresh_from!r}; stages are: " + ", ".join(names)
                )
            manifest.stage_generations[refresh_from] = (
                manifest.stage_generations.get(refresh_from, 0) + 1
            )
            dirty = True
        pinned, resolved = self._pin_source(manifest, source, refresh=refresh)
        dirty = dirty or resolved
        inputs = self.inputs_for(manifest, pinned)
        keys = stage_keys(inputs)
        now = self.now()
        published_key = self.incus.image_property(alias, BUILD_KEY_PROPERTY)
        entries: dict[str, CacheEntry] = {}
        if not no_cache and published_key == keys[-1].key:
            # The no-op path stops here: one image query, no listing, no sudo.
            steps = tuple(PlanStep(key, hit=True, reason="published") for key in keys)
        else:
            entries = self.cache.entries()
            steps = _plan_steps(keys, entries, pinned.fingerprint, inputs.build_allow, no_cache)
        return BuildPlan(
            alias=alias,
            source=source,
            pinned=pinned,
            inputs=inputs,
            steps=steps,
            published_key=published_key,
            manifest=manifest,
            no_cache=no_cache,
            now=now,
            max_age_days=self.max_age_days,
            dirty_manifest=dirty,
            entries=entries,
        )

    # -- building -----------------------------------------------------------

    def build(
        self,
        alias: str = DEFAULT_ALIAS,
        source: str = DEFAULT_SOURCE,
        *,
        refresh: bool = False,
        refresh_from: str | None = None,
        no_cache: bool = False,
        generation: int | None = None,
        allow_stale: bool = False,
        publish: bool = True,
        dry_run: bool = False,
        wait: bool = True,
        before_run: Callable[[BuildPlan], None] | None = None,
    ) -> BuildReport:
        """Plan, print, and (unless dry-run) run one build under the host lock."""
        with self.lock.held(wait=wait):
            plan = self.plan(
                alias,
                source,
                refresh=refresh,
                refresh_from=refresh_from,
                no_cache=no_cache,
                generation=generation,
            )
            prefix = "[dry-run] " if dry_run else ""
            for line in plan.lines():
                self.echo(prefix + line)
            if dry_run:
                return BuildReport(plan, dry_run=True)
            if plan.is_noop:
                if plan.dirty_manifest:
                    plan.manifest.save()
                return BuildReport(plan)
            if before_run is not None and plan.to_build:
                # A publish-only retry needs no ACL and therefore no sudo.
                before_run(plan)
            return self.run(plan, publish=publish, allow_stale=allow_stale or refresh)

    def run(
        self,
        plan: BuildPlan,
        *,
        publish: bool = True,
        allow_stale: bool = False,
        keep_failed: bool | None = None,
    ) -> BuildReport:
        started = time.monotonic()
        keep = self.keep_failed if keep_failed is None else keep_failed
        verdict, age = plan.age_verdict()
        if verdict == "stale" and not allow_stale and age is not None:
            raise SandboxshError(
                f"the deepest reusable cache entry is {format_age(age)} old, beyond the "
                f"{plan.max_age_days * STALE_MULTIPLIER}-day ceiling; rerun with --refresh "
                "to rebuild from a fresh source, or --allow-stale to reuse it anyway"
            )
        plan.manifest.save()
        if plan.is_noop:
            return BuildReport(plan)
        self._remove_leftover_workers()
        build_config = build_project_config(plan.source, plan.inputs.build_allow)
        policy: AclPolicy | None = None
        failure: Exception | None = None
        kept_worker: str | None = None
        parent = plan.reused_entry
        rebuilt = 0
        try:
            if plan.to_build:
                # Supply-chain scripts run only after the same host-enforced ACL
                # used for project VMs is attached to the stopped worker.
                policy = self.incus.apply_acl(build_config)
            for step in plan.to_build:
                try:
                    parent = self._build_stage(plan, step, parent, build_config, policy, keep=keep)
                except SandboxshError as error:
                    kept_worker = getattr(error, "kept_worker", None)
                    raise
                rebuilt += 1
            assert parent is not None
            if publish:
                self._publish(plan, parent, rebuilt, started)
        except Exception as error:
            failure = error
            raise
        finally:
            if failure is not None:
                self._annotate_failure(failure, policy)
            if failure is not None and kept_worker is not None:
                failure.add_note(
                    f"kept worker {kept_worker} and its ACL for inspection; enter it with "
                    f"`incus --project {self.incus.project} exec {kept_worker} -- bash`, "
                    "delete it afterwards and rerun `sandboxsh image build`"
                )
            elif policy is not None:
                try:
                    self.incus.delete_acl(build_config)
                except Exception as cleanup_error:
                    if failure is None:
                        raise
                    failure.add_note(f"ACL cleanup also failed: {cleanup_error}")
        return BuildReport(
            plan,
            policy=policy,
            published=publish,
            rebuilt=rebuilt,
            elapsed=time.monotonic() - started,
        )

    def _remove_leftover_workers(self) -> None:
        for worker in self.cache.workers():
            self.echo(f"cleanup  removing leftover worker {worker}")
            self.incus.delete_instance(worker)

    def _build_stage(
        self,
        plan: BuildPlan,
        step: PlanStep,
        parent: CacheEntry | None,
        build_config: ProjectConfig,
        policy: AclPolicy | None,
        *,
        keep: bool,
    ) -> CacheEntry:
        stage = step.stage
        worker = f"{WORKER_PREFIX}{step.key.key}-{secrets.token_hex(3)}"
        self.echo(f"build    {stage.name:<16} worker {worker}")
        if parent is None:
            self._init_worker(plan, worker)
        try:
            assert policy is not None
            if parent is not None:
                self.incus.copy_instance(parent.instance, worker)
            self.incus.attach_acl(build_config, instance=worker)
            self.incus.command("start", worker)
            self.incus.wait_for_agent(worker, timeout=600)
            # Real work on the first boot only, where cloud-init rewrites
            # /etc/hosts; the pins must land after that, never before.
            self.incus.command("exec", worker, "--", "cloud-init", "status", "--wait", check=False)
            self.incus.pin_allowlist(policy, instance=worker)
            pushed = [stage.script, *(self.guest_dir / name for name in stage.files)]
            for path in pushed:
                if not path.is_file():
                    raise SandboxshError(f"packaged guest script is missing: {path}")
                self.incus.command("file", "push", str(path), f"{worker}/root/{path.name}")
            # Stage output is streamed: a blocked endpoint is visible as the URL
            # that failed. Streaming inherits the terminal, so refuse stdin and
            # its pty; installers take their non-interactive defaults.
            self.incus.command(
                "exec",
                worker,
                "--disable-stdin",
                "--",
                "bash",
                f"/root/{stage.script.name}",
                *stage.arguments(plan.inputs),
                capture=False,
            )
            self.incus.command(
                "exec", worker, "--", "rm", "-f", *(f"/root/{path.name}" for path in pushed)
            )
            # The pins belong to this build, not to every VM cloned from the image.
            self.incus.unpin_allowlist(worker)
            # A forced stop is a power cut; flush the guest page cache first.
            self.incus.command("exec", worker, "--", "sync")
            self.incus.command("stop", worker, "--force")
        except Exception as error:
            parent_label = parent.key if parent is not None else "source image"
            failure = SandboxshError(
                f"stage {stage.name} failed in worker {worker} (parent {parent_label}): {error}"
            )
            if keep:
                # A failed supply-chain fetch is only diagnosable from inside the
                # worker, under the ACL that blocked it, so keep both.
                failure.kept_worker = worker  # type: ignore[attr-defined]
            self.cache.discard(worker, keep=keep)
            raise failure from error
        return self.cache.commit(
            worker,
            step.key,
            source=plan.pinned.fingerprint,
            build_allow=plan.inputs.build_allow,
            existing=plan.entries.get(step.key.key) if plan.no_cache else None,
        )

    def _init_worker(self, plan: BuildPlan, worker: str) -> None:
        remote, separator, _ = plan.source.partition(":")
        image = f"{remote}:{plan.pinned.fingerprint}" if separator else plan.pinned.fingerprint
        result = self.incus.command(
            "init",
            image,
            worker,
            "--vm",
            "--config",
            f"limits.cpu={BUILD_RESOURCES.cpus}",
            "--config",
            f"limits.memory={BUILD_RESOURCES.memory}",
            "--device",
            f"root,size={BUILD_RESOURCES.disk}",
            check=False,
        )
        if result.returncode:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown Incus error"
            raise SandboxshError(
                f"cannot create a worker from pinned source {plan.pinned.fingerprint[:12]} "
                f"({plan.source}): {detail}; if the remote no longer serves that image, "
                "rerun with --refresh to pin the current one"
            )

    def _publish(self, plan: BuildPlan, template: CacheEntry, rebuilt: int, started: float) -> None:
        properties = {
            BUILD_KEY_PROPERTY: plan.build_key,
            SOURCE_PROPERTY: plan.pinned.fingerprint,
            STAGES_PROPERTY: ",".join(stage.name for stage in plan.inputs.stages),
            DISK_PROPERTY: BUILD_RESOURCES.disk,
        }
        try:
            self.incus.publish(template.instance, plan.alias, properties)
        except Exception as error:
            error.add_note(
                f"the finished template {template.instance} is kept; rerunning "
                "`sandboxsh image build` retries only the publish. Publishing needs free "
                "space on the filesystem behind /var/lib/incus/images (see `sandboxsh doctor`)"
            )
            raise
        elapsed = time.monotonic() - started
        total = len(plan.steps)
        self.echo(
            f"publish  {plan.alias:<16} build_key {plan.build_key} "
            f"({rebuilt} of {total} stages rebuilt, {_format_elapsed(elapsed)})"
        )

    def _annotate_failure(self, failure: Exception, policy: AclPolicy | None) -> None:
        # An endpoint the host could not resolve is omitted from the ACL and
        # the pins, which the guest only ever sees as a connect timeout.
        try:
            remedy = self.incus.blocked_forwarding_remedy(self.incus.default_network())
        except Exception:
            remedy = None
        if remedy is not None:
            failure.add_note(remedy)
        if policy is not None and policy.unresolved_defaults:
            failure.add_note(
                "built-in endpoints omitted from the ACL because the host could "
                "not resolve them: " + ", ".join(policy.unresolved_defaults)
            )

    # -- cache inspection and pruning ----------------------------------------

    def chains(self, source: str, *, previous: int) -> tuple[set[str], set[str]]:
        """Keys on the current chain, and on the `previous` earlier global generations."""
        manifest = Manifest.load(self.manifest_path)
        pinned = manifest.sources.get(source)
        if pinned is None:
            # No manifest (first run, or lost): judge entries against the chain
            # the next build would start, rather than calling all of them orphans.
            pinned = PinnedSource.from_resolved(self.incus.resolve_vm_image(source), self.now())
        inputs = self.inputs_for(manifest, pinned)
        current = {key.key for key in stage_keys(inputs)}
        earlier: set[str] = set()
        for offset in range(1, previous + 1):
            generation = manifest.generation - offset
            if generation < 0:
                break
            earlier.update(key.key for key in stage_keys(replace(inputs, generation=generation)))
        return current, earlier - current

    def cache_rows(
        self, source: str = DEFAULT_SOURCE, *, previous: int = 1
    ) -> list[dict[str, str]]:
        current, earlier = self.chains(source, previous=previous)
        now = self.now()
        ceiling = timedelta(days=self.max_age_days * STALE_MULTIPLIER)
        rows = []
        for key, entry in sorted(self.cache.entries().items(), key=lambda item: item[1].stage):
            age = entry.age(now)
            if age > ceiling:
                chain = "stale"
            elif key in current:
                chain = "current"
            elif key in earlier:
                chain = "previous"
            else:
                chain = "orphan"
            rows.append(
                {
                    "key": key,
                    "stage": entry.stage,
                    "parent": entry.parent or "-",
                    "age": format_age(age),
                    "chain": chain,
                    "build_allow": ",".join(entry.build_allow) or "-",
                    "instance": entry.instance,
                }
            )
        return rows

    def prune(
        self,
        source: str = DEFAULT_SOURCE,
        *,
        keep_generations: int = 1,
        all_: bool = False,
        wait: bool = True,
    ) -> tuple[str, ...]:
        with self.lock.held(wait=wait):
            current, earlier = self.chains(source, previous=0 if all_ else keep_generations)
            return self.cache.prune(current | earlier)

    def status(self, alias: str = DEFAULT_ALIAS, source: str = DEFAULT_SOURCE) -> list[str]:
        published = self.incus.image_property(alias, BUILD_KEY_PROPERTY)
        published_source = self.incus.image_property(alias, SOURCE_PROPERTY) or ""
        lines = []
        if published is None:
            lines.append(
                f"image    {alias:<16} published without a build key (built before caching)"
            )
        else:
            lines.append(
                f"image    {alias:<16} build_key {published}"
                + (f"  source {published_source[:8]}" if published_source else "")
            )
        plan = self.plan(alias, source)
        serial = plan.pinned.serial
        lines.append(
            f"source   {source.split(':', 1)[-1]:<16} {plan.pinned.fingerprint[:8]}"
            + (f"  serial {serial}" if serial else "")
            + f"  (pinned {plan.pinned.pinned or 'now'})"
        )
        if plan.is_noop:
            lines.append("chain    up to date")
        else:
            first = plan.first_rebuild
            count = len(plan.to_build)
            total = len(plan.steps)
            lines.append(
                f"chain    would rebuild from {first.name if first else '?'} "
                f"({count} of {total} stages)"
            )
        return lines


def _plan_steps(
    keys: tuple[StageKey, ...],
    entries: Mapping[str, CacheEntry],
    source: str,
    build_allow: tuple[str, ...],
    no_cache: bool,
) -> tuple[PlanStep, ...]:
    steps: list[PlanStep] = []
    parent_missed = False
    for key in keys:
        entry = None if no_cache else entries.get(key.key)
        if entry is not None and not parent_missed:
            steps.append(PlanStep(key, hit=True, entry=entry))
            continue
        if no_cache:
            reason = "--no-cache"
        else:
            reason = _miss_reason(key, entries, source, build_allow, parent_missed)
        steps.append(PlanStep(key, hit=False, reason=reason))
        parent_missed = True
    return tuple(steps)


def _miss_reason(
    key: StageKey,
    entries: Mapping[str, CacheEntry],
    source: str,
    build_allow: tuple[str, ...],
    parent_missed: bool,
) -> str:
    """Why no entry matches, judged against the closest existing entry of the stage."""
    if parent_missed:
        return "parent changed"
    same_stage = [entry for entry in entries.values() if entry.stage == key.stage.name]
    if not same_stage:
        return "no entry"
    if key.parent is None:
        siblings = [entry for entry in same_stage if entry.source == source]
        if not siblings:
            return "source changed"
    else:
        siblings = [entry for entry in same_stage if entry.parent == key.parent]
        if not siblings:
            return "no entry"
    sibling = max(siblings, key=lambda entry: entry.created)
    if sibling.script != key.script:
        return "script changed"
    if sibling.inputs != key.inputs:
        return f"inputs changed ({key.stage.inputs_label or 'stage inputs'})"
    if sibling.generation != key.generation or sibling.stage_generation != key.stage_generation:
        return "generation bumped"
    if sibling.build_allow != build_allow:
        return "inputs changed (build-allow)"
    return "no entry"


def _format_elapsed(seconds: float) -> str:
    whole = int(seconds)
    minutes, rest = divmod(whole, 60)
    return f"{minutes}m{rest:02d}s" if minutes else f"{rest}s"
