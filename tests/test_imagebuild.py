from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fakes import FakeLock, KeyedRunner, cache_listing, cache_record

from sandboxsh import imagebuild
from sandboxsh.config import parse_size
from sandboxsh.errors import SandboxshError
from sandboxsh.imagebuild import (
    BUILD_KEY_PROPERTY,
    CACHE_PREFIX,
    CLOUD_INIT_CLEAN,
    CLOUD_INIT_DISABLE,
    CLOUD_INIT_ENABLE,
    DISK_PROPERTY,
    WORKER_PREFIX,
    BuildInputs,
    ImageBuilder,
    Manifest,
    PinnedSource,
    load_stages,
    stage_keys,
)
from sandboxsh.incus import Incus, ResolvedImage
from sandboxsh.process import Result
from sandboxsh.security import AclPolicy

SOURCE = "images:debian/13/cloud"
ALIAS = "sandboxsh/base"
FINGERPRINT = "a046182b774be4603940471e20c2015c4607c3c11663cc38508e7938642397ec"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
IMAGE_INFO = (
    f"Fingerprint: {FINGERPRINT}\nSize: 377.63MiB\nArchitecture: x86_64\n"
    "Type: virtual-machine\nProperties:\n    serial: 20260904_05:24\n"
)

STAGE_BODIES = {
    "10-base.sh": f"apt-get install -y base\n{CLOUD_INIT_DISABLE}\n",
    "20-docker-node.sh": "install docker\n",
    "30-user.sh": 'useradd --uid "$1" --gid "$2" dev\n',
    "50-agents.sh": "install agents\n",
    "90-finalize.sh": f"install helpers\n{CLOUD_INIT_ENABLE}\n{CLOUD_INIT_CLEAN}\n",
}


def write_guest(root: Path, bodies: dict[str, str] | None = None) -> Path:
    guest = root / "guest"
    stages = guest / "stages"
    stages.mkdir(parents=True, exist_ok=True)
    for name, body in (bodies or STAGE_BODIES).items():
        (stages / name).write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}")
    (guest / "agent-init.sh").write_text("#!/usr/bin/env bash\necho agent\n")
    (guest / "instance-init.sh").write_text("#!/usr/bin/env bash\necho instance\n")
    return guest


def inputs_for(guest: Path, **overrides) -> BuildInputs:
    values = dict(
        source_fingerprint=FINGERPRINT,
        architecture="x86_64",
        uid=1000,
        gid=1000,
        stages=load_stages(guest / "stages"),
        helper_hashes=("h1", "h2"),
        build_allow=(),
        generation=0,
        stage_generations={},
    )
    values.update(overrides)
    return BuildInputs(**values)


def keys_of(inputs: BuildInputs) -> list[str]:
    return [key.key for key in stage_keys(inputs)]


# -- keys ---------------------------------------------------------------------


def test_editing_a_stage_changes_its_key_and_every_later_one_only(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)
    before = keys_of(inputs_for(guest))

    (guest / "stages" / "30-user.sh").write_text("#!/usr/bin/env bash\nuseradd dev extra\n")
    after = keys_of(inputs_for(guest))

    assert after[:2] == before[:2]
    assert all(a != b for a, b in zip(after[2:], before[2:], strict=True))


def test_uid_change_leaves_base_and_docker_stages_alone(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)
    before = keys_of(inputs_for(guest))

    after = keys_of(inputs_for(guest, uid=1234))

    assert after[:2] == before[:2]
    assert after[2:] != before[2:]


def test_global_generation_changes_floating_stages_only(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)
    inputs = inputs_for(guest)
    before = stage_keys(inputs)

    bumped = stage_keys(replace(inputs, generation=1))
    finalize_bumped = stage_keys(replace(inputs, stage_generations={"90-finalize": 7}))

    assert bumped[0].key != before[0].key
    # Finalize fetches nothing, so its own generation is ignored: same chain.
    assert [key.key for key in finalize_bumped] == [key.key for key in before]


def test_refresh_from_bumps_that_stage_and_its_descendants(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)
    before = keys_of(inputs_for(guest))

    after = keys_of(inputs_for(guest, stage_generations={"50-agents": 1}))

    assert after[:3] == before[:3]
    assert after[3:] != before[3:]


def test_build_allow_hosts_enter_every_floating_stage_key(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)
    before = keys_of(inputs_for(guest))

    after = keys_of(inputs_for(guest, build_allow=("mirror.example.com",)))

    assert after[0] != before[0]


def test_source_fingerprint_is_the_root_of_the_chain(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)
    before = keys_of(inputs_for(guest))

    after = keys_of(inputs_for(guest, source_fingerprint="f" * 64))

    assert all(a != b for a, b in zip(after, before, strict=True))


# -- stage loader ---------------------------------------------------------------


def test_stages_load_in_numeric_order_with_declared_inputs(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)

    stages = load_stages(guest / "stages")

    assert [stage.name for stage in stages] == [
        "10-base",
        "20-docker-node",
        "30-user",
        "50-agents",
        "90-finalize",
    ]
    user = stages[2]
    assert user.arguments(inputs_for(guest, uid=7, gid=8)) == ("7", "8")
    assert user.inputs(inputs_for(guest, uid=7, gid=8)) == ("uid=7", "gid=8")
    finalize = stages[-1]
    assert not finalize.floating
    assert finalize.files == ("agent-init.sh", "instance-init.sh")


def test_a_stage_file_without_the_numeric_prefix_is_rejected(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)
    (guest / "stages" / "notes.md").write_text("stray\n")

    with pytest.raises(SandboxshError, match="notes.md"):
        load_stages(guest / "stages")


def test_optional_stages_are_inserted_before_finalize(tmp_path: Path) -> None:
    guest = write_guest(tmp_path)
    extra = tmp_path / "features" / "60-plone.sh"
    extra.parent.mkdir()
    extra.write_text("#!/usr/bin/env bash\ninstall plone\n")

    stages = load_stages(guest / "stages", extra=(extra,))

    assert [stage.name for stage in stages][-3:] == ["50-agents", "60-plone", "90-finalize"]


def test_the_cloud_init_contract_is_asserted_by_the_loader(tmp_path: Path) -> None:
    bodies = dict(STAGE_BODIES)
    bodies["10-base.sh"] = "apt-get install -y base\n"
    guest = write_guest(tmp_path, bodies)

    with pytest.raises(SandboxshError, match="cloud-init.disabled"):
        load_stages(guest / "stages")

    bodies = dict(STAGE_BODIES)
    bodies["90-finalize.sh"] = f"{CLOUD_INIT_CLEAN}\n{CLOUD_INIT_ENABLE}\n"
    guest = write_guest(tmp_path / "second", bodies)

    with pytest.raises(SandboxshError, match="re-enable cloud-init before cleaning"):
        load_stages(guest / "stages")


def test_the_packaged_stages_satisfy_the_loader() -> None:
    stages = load_stages(Path(__file__).parents[1] / "guest" / "stages")

    assert [stage.name for stage in stages] == [
        "10-base",
        "20-docker-node",
        "30-user",
        "40-browser",
        "50-agents",
        "90-finalize",
    ]


# -- builder harness -------------------------------------------------------------


class Harness:
    def __init__(self, tmp_path: Path, *, now: datetime = NOW, environ: dict | None = None):
        self.guest = write_guest(tmp_path)
        self.runner = KeyedRunner()
        self.runner.respond("image", "info", SOURCE, "--vm", stdout=IMAGE_INFO)
        self.runner.respond(
            "image", "get-property", stderr="Error: Property not found", returncode=1
        )
        self.runner.respond("list", CACHE_PREFIX, "--format=json", stdout="[]")
        self.runner.respond("list", WORKER_PREFIX, "--format=json", stdout="[]")
        self.clock = now
        self.lines: list[str] = []
        self.lock = FakeLock()
        self.state = tmp_path / "state"
        self.incus = Incus(self.runner)
        self.builder = ImageBuilder(
            self.incus,
            guest_dir=self.guest,
            lock=self.lock,
            state_dir=self.state,
            now=lambda: self.clock,
            echo=self.lines.append,
            uid=1000,
            gid=1000,
            build_allow=(),
            environ=environ or {},
        )

    def keys(self) -> list[str]:
        manifest = Manifest(self.state / "manifest.json")
        pinned = PinnedSource(FINGERPRINT, "x86_64", "20260904_05:24", "2026-09-04")
        return [key.key for key in stage_keys(self.builder.inputs_for(manifest, pinned))]

    def stage_keys(self):
        manifest = Manifest(self.state / "manifest.json")
        pinned = PinnedSource(FINGERPRINT, "x86_64", "20260904_05:24", "2026-09-04")
        return stage_keys(self.builder.inputs_for(manifest, pinned))

    def pin(self) -> None:
        manifest = Manifest(self.state / "manifest.json")
        manifest.sources[SOURCE] = PinnedSource(
            FINGERPRINT, "x86_64", "20260904_05:24", "2026-09-04"
        )
        manifest.save()

    def cache(self, upto: int, *, created: datetime | None = None) -> list[str]:
        """Populate the listing with entries for stages [0, upto]; returns their keys."""
        records = []
        stamp = (created or self.clock - timedelta(days=3)).isoformat(timespec="seconds")
        for key in self.stage_keys()[: upto + 1]:
            records.append(
                cache_record(
                    key.key,
                    key.stage.name,
                    key.parent,
                    stamp,
                    source=FINGERPRINT,
                    generation=key.generation,
                    stage_generation=key.stage_generation,
                    script=key.script,
                    inputs=key.inputs,
                )
            )
        self.runner.responses[("list", CACHE_PREFIX, "--format=json")] = cache_listing(records)
        return [key.key for key in self.stage_keys()[: upto + 1]]

    def publish_key(self, key: str) -> None:
        self.runner.respond("image", "get-property", ALIAS, BUILD_KEY_PROPERTY, stdout=f"{key}\n")


@pytest.fixture
def fake_acl(monkeypatch) -> list[str]:
    calls: list[str] = []
    policy = AclPolicy(
        document={}, resolutions={"deb.debian.org": ("1.2.3.4",)}, unresolved_defaults=()
    )

    def apply_acl(self, config):
        calls.append("apply")
        return policy

    def delete_acl(self, config):
        calls.append("delete")

    monkeypatch.setattr(Incus, "apply_acl", apply_acl)
    monkeypatch.setattr(Incus, "delete_acl", delete_acl)
    monkeypatch.setattr(Incus, "wait_for_agent", lambda self, instance, timeout=300: None)
    return calls


def state_changing(operations: list[list[str]]) -> list[list[str]]:
    read_only = {"list", "image", "query"}
    return [op for op in operations if op and op[0] not in read_only and op[0] != "sudo"]


# -- builder ----------------------------------------------------------------------


def test_an_up_to_date_image_is_a_noop_with_one_image_query(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.publish_key(harness.keys()[-1])

    report = harness.builder.build(ALIAS, SOURCE)

    assert report.noop
    assert harness.runner.operations() == [
        ["image", "get-property", ALIAS, BUILD_KEY_PROPERTY],
    ]
    assert fake_acl == []
    assert [line.split()[0] for line in harness.lines] == ["source", "up"]


def test_a_full_build_inits_from_the_pinned_source_and_publishes(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)

    report = harness.builder.build(ALIAS, SOURCE)

    keys = harness.keys()
    init = harness.runner.matching("init")
    assert len(init) == 1
    assert init[0][1] == f"images:{FINGERPRINT}"
    assert init[0][2].startswith(f"{WORKER_PREFIX}{keys[0]}-")
    assert harness.runner.matching("copy") and len(harness.runner.matching("copy")) == 4
    publish = harness.runner.matching("publish")
    assert publish == [
        [
            "publish",
            f"{CACHE_PREFIX}{keys[-1]}",
            "--alias",
            ALIAS,
            "--reuse",
            f"{BUILD_KEY_PROPERTY}={keys[-1]}",
            f"user.sandboxsh.source={FINGERPRINT}",
            "user.sandboxsh.stages=10-base,20-docker-node,30-user,50-agents,90-finalize",
            f"{DISK_PROPERTY}=30GiB",
        ]
    ]
    assert report.rebuilt == 5 and report.published
    assert fake_acl == ["apply", "delete"]
    manifest = json.loads((harness.state / "manifest.json").read_text())
    assert manifest["sources"][SOURCE]["fingerprint"] == FINGERPRINT
    assert [line.split()[0] for line in harness.lines] == [
        "source",
        "miss",
        "miss",
        "miss",
        "miss",
        "miss",
        "build",
        "build",
        "build",
        "build",
        "build",
        "publish",
    ]


def test_a_miss_at_the_third_stage_copies_the_second_entry_and_never_inits(
    tmp_path: Path, fake_acl
) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    cached = harness.cache(1)

    harness.builder.build(ALIAS, SOURCE)

    assert harness.runner.matching("init") == []
    copies = harness.runner.matching("copy")
    assert copies[0][1] == f"{CACHE_PREFIX}{cached[1]}"
    assert [line.split()[0] for line in harness.lines[:6]] == [
        "source",
        "hit",
        "hit",
        "miss",
        "miss",
        "miss",
    ]
    assert "no entry" in harness.lines[3]
    assert "parent changed" in harness.lines[4]


def test_a_stage_runs_under_the_acl_with_pins_and_is_committed_by_stamp_then_rename(
    tmp_path: Path, fake_acl
) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(3)

    harness.builder.build(ALIAS, SOURCE, publish=False)

    ops = harness.runner.operations()
    worker = harness.runner.matching("copy")[0][2]
    key = harness.keys()[-1]
    touching = [op for op in ops if worker in op]
    assert touching[0][:1] == ["copy"]
    assert touching[1][:4] == ["config", "device", "override", worker]
    assert touching[2] == ["start", worker]
    exec_ops = [op for op in ops if op[:2] == ["exec", worker]]
    assert exec_ops[0][2:] == ["--", "cloud-init", "status", "--wait"]
    assert "/etc/hosts" in exec_ops[1][5] and "1.2.3.4 deb.debian.org" in exec_ops[1][-1]  # pin
    stage_run = next(op for op in exec_ops if "/root/90-finalize.sh" in op)
    assert stage_run[2] == "--disable-stdin"
    pushed = [op[3] for op in ops if op[:2] == ["file", "push"]]
    assert pushed == [
        f"{worker}/root/90-finalize.sh",
        f"{worker}/root/agent-init.sh",
        f"{worker}/root/instance-init.sh",
    ]
    assert exec_ops[-1][2:] == ["--", "sync"]
    stop = ops.index(["stop", worker, "--force"])
    stamp = next(i for i, op in enumerate(ops) if op[:3] == ["config", "set", worker])
    rename = ops.index(["rename", worker, f"{CACHE_PREFIX}{key}"])
    assert stop < stamp < rename
    assert f"user.sandboxsh.cache.key={key}" in ops[stamp]
    assert "user.sandboxsh.cache.stage=90-finalize" in ops[stamp]
    assert harness.runner.matching("publish") == []
    assert not any(op[:1] == ["init"] for op in ops)


def test_the_user_stage_receives_uid_and_gid_as_arguments(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(1)

    harness.builder.build(ALIAS, SOURCE, publish=False)

    run = next(op for op in harness.runner.operations() if "/root/30-user.sh" in op)
    assert run[-3:] == ["/root/30-user.sh", "1000", "1000"]


def test_a_failing_stage_deletes_its_worker_and_leaves_the_parent(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    cached = harness.cache(1)

    def fail(op: list[str]) -> Result:
        if "/root/30-user.sh" in op:
            return Result("", "useradd: boom", 1)
        return Result("", "", 0)

    harness.runner.respond_with("exec", handler=fail)

    with pytest.raises(SandboxshError) as info:
        harness.builder.build(ALIAS, SOURCE)

    message = str(info.value)
    assert "stage 30-user failed" in message
    assert f"parent {cached[1]}" in message
    deleted = harness.runner.matching("delete")
    assert len(deleted) == 1 and deleted[0][1].startswith(WORKER_PREFIX)
    assert harness.runner.matching("rename") == []
    assert fake_acl == ["apply", "delete"]


def test_keep_builder_keeps_the_worker_and_the_acl(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path, environ={"SANDBOXSH_KEEP_BUILDER": "1"})
    harness.pin()
    harness.cache(1)
    harness.runner.respond_with(
        "exec",
        handler=lambda op: Result("", "boom", 1) if "/root/30-user.sh" in op else Result("", "", 0),
    )

    with pytest.raises(SandboxshError) as info:
        harness.builder.build(ALIAS, SOURCE)

    assert harness.runner.matching("delete") == []
    assert fake_acl == ["apply"]
    assert any("kept worker" in note for note in info.value.__notes__)


def test_leftover_workers_are_removed_before_a_build(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.runner.respond(
        "list",
        WORKER_PREFIX,
        "--format=json",
        stdout=json.dumps([{"name": f"{WORKER_PREFIX}deadbeefdeadbeef-abc123", "config": {}}]),
    )

    harness.builder.build(ALIAS, SOURCE, publish=False)

    assert harness.runner.matching("delete")[0][1] == f"{WORKER_PREFIX}deadbeefdeadbeef-abc123"
    assert any(line.startswith("cleanup") for line in harness.lines)


def test_dry_run_prints_the_plan_and_issues_no_state_changing_command(
    tmp_path: Path, fake_acl
) -> None:
    harness = Harness(tmp_path)

    report = harness.builder.build(ALIAS, SOURCE, dry_run=True, refresh=True)

    assert report.dry_run
    assert state_changing(harness.runner.operations()) == []
    assert fake_acl == []
    assert not (harness.state / "manifest.json").exists()
    assert all(line.startswith("[dry-run] ") for line in harness.lines)


def test_no_cache_ignores_entries_but_replaces_the_one_it_rebuilds(
    tmp_path: Path, fake_acl
) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    cached = harness.cache(4)

    harness.builder.build(ALIAS, SOURCE, no_cache=True, publish=False)

    assert all("--no-cache" in line for line in harness.lines[1:6])
    assert len(harness.runner.matching("init")) == 1
    deleted = [op[1] for op in harness.runner.matching("delete")]
    assert deleted == [f"{CACHE_PREFIX}{key}" for key in cached]
    for key in cached:
        delete = harness.runner.operations().index(["delete", f"{CACHE_PREFIX}{key}", "--force"])
        rename = next(
            i
            for i, op in enumerate(harness.runner.operations())
            if op[:1] == ["rename"] and op[2] == f"{CACHE_PREFIX}{key}"
        )
        assert delete < rename


def test_the_miss_reason_names_a_changed_script_or_input(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(4)
    (harness.guest / "stages" / "20-docker-node.sh").write_text("#!/usr/bin/env bash\nnew docker\n")

    plan = harness.builder.plan(ALIAS, SOURCE)

    assert [(step.hit, step.reason) for step in plan.steps] == [
        (True, ""),
        (False, "script changed"),
        (False, "parent changed"),
        (False, "parent changed"),
        (False, "parent changed"),
    ]

    harness = Harness(tmp_path / "uid")
    harness.pin()
    harness.cache(4)
    harness.builder.uid = 4321
    plan = harness.builder.plan(ALIAS, SOURCE)

    assert plan.steps[2].reason == "inputs changed (uid/gid)"
    assert plan.first_rebuild is not None and plan.first_rebuild.name == "30-user"


def test_the_miss_reason_names_a_bumped_generation_and_widened_build_allow(
    tmp_path: Path, fake_acl
) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(4)

    plan = harness.builder.plan(ALIAS, SOURCE, refresh=True)

    assert plan.steps[0].reason == "generation bumped"

    harness.builder.build_allow = ("mirror.example.com",)
    plan = harness.builder.plan(ALIAS, SOURCE)

    assert plan.steps[0].reason == "inputs changed (build-allow)"


def test_refresh_re_pins_the_source_and_persists_the_generation(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.runner.respond(
        "image", "info", SOURCE, "--vm", stdout=IMAGE_INFO.replace(FINGERPRINT, "b" * 64)
    )

    harness.builder.build(ALIAS, SOURCE, refresh=True, publish=False)

    manifest = Manifest.load(harness.state / "manifest.json")
    assert manifest.generation == 1
    assert manifest.sources[SOURCE].fingerprint == "b" * 64
    assert harness.runner.matching("init")[0][1] == "images:" + "b" * 64


def test_refresh_from_bumps_only_that_stage(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(4)

    harness.builder.build(ALIAS, SOURCE, refresh_from="50-agents", publish=False)

    manifest = Manifest.load(harness.state / "manifest.json")
    assert manifest.generation == 0
    assert manifest.stage_generations == {"50-agents": 1}
    assert [line.split()[0] for line in harness.lines[1:6]] == ["hit", "hit", "hit", "miss", "miss"]
    assert "generation bumped" in harness.lines[4]
    assert len(harness.runner.matching("copy")) == 2

    with pytest.raises(SandboxshError, match="unknown stage"):
        harness.builder.plan(ALIAS, SOURCE, refresh_from="99-nope")


def test_generation_restores_an_earlier_chain(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    manifest = Manifest.load(harness.state / "manifest.json")
    manifest.generation = 3
    manifest.save()

    plan = harness.builder.plan(ALIAS, SOURCE, generation=2)

    assert plan.inputs.generation == 2
    assert plan.dirty_manifest


def test_an_old_entry_warns_and_a_very_old_one_refuses_without_allow_stale(
    tmp_path: Path, fake_acl
) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(1, created=NOW - timedelta(days=40))

    harness.builder.build(ALIAS, SOURCE, publish=False)

    assert any(line.startswith("warn") for line in harness.lines)

    harness = Harness(tmp_path / "stale")
    harness.pin()
    harness.cache(1, created=NOW - timedelta(days=100))

    with pytest.raises(SandboxshError, match="beyond the 90-day ceiling"):
        harness.builder.build(ALIAS, SOURCE, publish=False)
    assert harness.runner.matching("copy") == []

    harness.lines.clear()
    harness.builder.build(ALIAS, SOURCE, publish=False, allow_stale=True)

    assert len(harness.runner.matching("copy")) == 3


def test_the_age_ceiling_is_configurable(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path, environ={"SANDBOXSH_CACHE_MAX_AGE_DAYS": "2"})
    harness.pin()
    harness.cache(1, created=NOW - timedelta(days=7))

    with pytest.raises(SandboxshError, match="6-day ceiling"):
        harness.builder.build(ALIAS, SOURCE, publish=False)


def test_a_publish_only_retry_needs_no_acl_and_no_sudo(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(4)
    primed = []

    report = harness.builder.build(ALIAS, SOURCE, before_run=lambda plan: primed.append(plan))

    assert report.published and report.rebuilt == 0
    assert primed == []
    assert fake_acl == []
    assert len(harness.runner.matching("publish")) == 1


def test_prune_without_a_manifest_resolves_the_source_instead_of_orphaning_all(
    tmp_path: Path,
) -> None:
    harness = Harness(tmp_path)
    current = harness.stage_keys()
    harness.runner.responses[("list", CACHE_PREFIX, "--format=json")] = cache_listing(
        [
            cache_record(current[0].key, "10-base", None, NOW.isoformat()),
            cache_record("0123456789abcdef", "20-docker-node", None, NOW.isoformat()),
        ]
    )

    assert harness.builder.prune(SOURCE) == ("0123456789abcdef",)
    assert not (harness.state / "manifest.json").exists()


def test_a_publish_failure_keeps_the_template_and_says_so(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(4)
    harness.runner.respond("publish", stderr="Error: no space left on device", returncode=1)

    with pytest.raises(Exception) as info:
        harness.builder.build(ALIAS, SOURCE)

    assert any("retries only the publish" in note for note in info.value.__notes__)
    assert harness.runner.matching("copy") == []
    assert fake_acl == []


def test_a_stage_10_init_failure_points_at_refresh(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.runner.respond("init", stderr="Error: Image not found", returncode=1)

    with pytest.raises(SandboxshError, match="--refresh"):
        harness.builder.build(ALIAS, SOURCE)


def test_build_takes_the_lock_and_no_wait_fails_fast(tmp_path: Path, fake_acl) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.publish_key(harness.keys()[-1])

    harness.builder.build(ALIAS, SOURCE)
    assert harness.lock.acquired == 1 and harness.lock.waits == [True]

    harness.lock.busy = True
    with pytest.raises(SandboxshError, match="lock"):
        harness.builder.build(ALIAS, SOURCE, wait=False)


def test_the_real_lock_refuses_a_second_non_waiting_holder(tmp_path: Path) -> None:
    lock = imagebuild.BuildLock(tmp_path / "build" / "lock")

    with (
        lock.held(),
        pytest.raises(SandboxshError, match="holds the build lock"),
        lock.held(wait=False),
    ):
        pass


# -- cache listing and prune ------------------------------------------------------


def test_cache_rows_classify_current_previous_orphan_and_stale(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    manifest = Manifest.load(harness.state / "manifest.json")
    manifest.generation = 1
    manifest.save()
    inputs = harness.builder.inputs_for(manifest, manifest.sources[SOURCE])
    current = stage_keys(inputs)
    previous = stage_keys(replace(inputs, generation=0))
    recent = (NOW - timedelta(days=3)).isoformat()
    ancient = (NOW - timedelta(days=200)).isoformat()
    harness.runner.responses[("list", CACHE_PREFIX, "--format=json")] = cache_listing(
        [
            cache_record(current[0].key, "10-base", None, recent),
            cache_record(previous[0].key, "10-base", None, recent),
            cache_record(
                "0123456789abcdef",
                "20-docker-node",
                "ffffffffffffffff",
                recent,
                build_allow="mirror.example.com",
            ),
            cache_record("fedcba9876543210", "30-user", None, ancient),
        ]
    )

    rows = harness.builder.cache_rows(SOURCE)

    by_key = {row["key"]: row for row in rows}
    assert by_key[current[0].key]["chain"] == "current"
    assert by_key[previous[0].key]["chain"] == "previous"
    assert by_key["0123456789abcdef"]["chain"] == "orphan"
    assert by_key["0123456789abcdef"]["build_allow"] == "mirror.example.com"
    assert by_key["fedcba9876543210"]["chain"] == "stale"
    assert by_key[current[0].key]["age"] == "3d"
    assert by_key[current[0].key]["instance"] == f"{CACHE_PREFIX}{current[0].key}"


def test_prune_deletes_only_off_chain_entries_under_the_lock(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    manifest = Manifest.load(harness.state / "manifest.json")
    manifest.generation = 1
    manifest.save()
    inputs = harness.builder.inputs_for(manifest, manifest.sources[SOURCE])
    current = stage_keys(inputs)
    previous = stage_keys(replace(inputs, generation=0))
    recent = NOW.isoformat()
    harness.runner.responses[("list", CACHE_PREFIX, "--format=json")] = cache_listing(
        [
            cache_record(current[0].key, "10-base", None, recent),
            cache_record(previous[0].key, "10-base", None, recent),
            cache_record("0123456789abcdef", "20-docker-node", None, recent),
        ]
    )

    removed = harness.builder.prune(SOURCE)

    assert removed == ("0123456789abcdef",)
    assert harness.runner.matching("delete") == [
        ["delete", f"{CACHE_PREFIX}0123456789abcdef", "--force"]
    ]
    assert harness.lock.acquired == 1

    removed = harness.builder.prune(SOURCE, all_=True)

    assert set(removed) == {"0123456789abcdef", previous[0].key}


def test_prune_never_touches_project_vms_or_workers(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.runner.responses[("list", CACHE_PREFIX, "--format=json")] = cache_listing(
        [
            {
                "name": "ss-demo-1000",
                "status": "Running",
                "config": {"user.sandboxsh.name": "demo"},
            },
            {"name": f"{CACHE_PREFIX}notakey", "status": "Stopped", "config": {}},
        ]
    )

    assert harness.builder.prune(SOURCE) == ()
    assert harness.runner.matching("delete") == []


# -- status ---------------------------------------------------------------------------


def test_status_reports_the_published_key_and_the_first_stage_to_rebuild(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    harness.pin()
    harness.cache(2)
    harness.runner.respond(
        "image", "get-property", ALIAS, BUILD_KEY_PROPERTY, stdout="0000000000000000\n"
    )

    lines = harness.builder.status(ALIAS, SOURCE)

    assert lines[0].startswith("image    sandboxsh/base   build_key 0000000000000000")
    assert "serial 20260904_05:24" in lines[1]
    assert lines[2] == "chain    would rebuild from 50-agents (2 of 5 stages)"

    harness.publish_key(harness.keys()[-1])
    assert harness.builder.status(ALIAS, SOURCE)[2] == "chain    up to date"


# -- manifest and helpers ---------------------------------------------------------------


def test_manifest_round_trips_and_tolerates_garbage(tmp_path: Path) -> None:
    path = tmp_path / "build" / "manifest.json"
    manifest = Manifest(path, generation=2, stage_generations={"50-agents": 1})
    manifest.sources[SOURCE] = PinnedSource("f" * 64, "x86_64", "1", "2026-09-04")
    manifest.save()

    loaded = Manifest.load(path)

    assert loaded.generation == 2
    assert loaded.stage_generations == {"50-agents": 1}
    assert loaded.sources[SOURCE].fingerprint == "f" * 64

    path.write_text("{not json")
    assert Manifest.load(path).generation == 0


def test_resolve_vm_image_parses_info_and_falls_back_to_the_local_cache() -> None:
    runner = KeyedRunner()
    runner.respond("image", "info", SOURCE, "--vm", stdout=IMAGE_INFO)

    resolved = Incus(runner).resolve_vm_image(SOURCE)

    assert resolved == ResolvedImage(FINGERPRINT, "x86_64", "20260904_05:24")

    runner = KeyedRunner()
    runner.respond("image", "info", stderr="Error: dial tcp: no route", returncode=1)
    runner.respond(
        "image",
        "list",
        "--format=json",
        stdout=json.dumps(
            [
                {
                    "fingerprint": "c" * 64,
                    "type": "container",
                    "architecture": "x86_64",
                    "update_source": {"alias": "debian/13/cloud"},
                },
                {
                    "fingerprint": "d" * 64,
                    "type": "virtual-machine",
                    "architecture": "x86_64",
                    "properties": {"serial": "20260903_05:24"},
                    "update_source": {"alias": "debian/13/cloud"},
                },
            ]
        ),
    )

    assert Incus(runner).resolve_vm_image(SOURCE) == ResolvedImage(
        "d" * 64, "x86_64", "20260903_05:24"
    )

    runner = KeyedRunner()
    runner.respond("image", "info", stderr="Error: dial tcp: no route", returncode=1)
    runner.respond("image", "list", "--format=json", stdout="[]")
    with pytest.raises(SandboxshError, match="cannot resolve source image"):
        Incus(runner).resolve_vm_image(SOURCE)


def test_image_property_distinguishes_absent_from_error() -> None:
    runner = KeyedRunner()
    runner.respond("image", "get-property", stderr="Error: Property not found", returncode=1)
    assert Incus(runner).image_property(ALIAS, BUILD_KEY_PROPERTY) is None

    runner.respond("image", "get-property", stderr='Error: Image "x" not found', returncode=1)
    assert Incus(runner).image_property("x", BUILD_KEY_PROPERTY) is None

    runner.respond("image", "get-property", stderr="permission denied", returncode=1)
    with pytest.raises(SandboxshError):
        Incus(runner).image_property(ALIAS, BUILD_KEY_PROPERTY)


def test_list_instances_keeps_only_the_named_prefix() -> None:
    runner = KeyedRunner()
    runner.respond(
        "list",
        stdout=json.dumps(
            [{"name": "sandboxsh-cache-a"}, {"name": "sandboxsh-cache-b-old"}, {"name": "ss-x"}]
        ),
    )

    names = [record["name"] for record in Incus(runner).list_instances("sandboxsh-cache-")]

    assert names == ["sandboxsh-cache-a", "sandboxsh-cache-b-old"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("30GiB", 30 * 1024**3),
        ("30GB", 30 * 1000**3),
        ("30G", 30 * 1000**3),
        ("512MiB", 512 * 1024**2),
        ("7", 7),
    ],
)
def test_parse_size(value: str, expected: int) -> None:
    assert parse_size(value) == expected


def test_a_project_disk_smaller_than_the_template_is_refused_before_incus(tmp_path: Path) -> None:
    from sandboxsh.config import load_config

    path = tmp_path / ".sandboxsh.json"
    path.write_text(json.dumps({"name": "demo", "dirs": ["."], "resources": {"disk": "16GiB"}}))
    config = load_config(path)
    runner = KeyedRunner()
    runner.respond("image", "get-property", ALIAS, DISK_PROPERTY, stdout="30GiB\n")

    with pytest.raises(SandboxshError, match="at least 30GiB"):
        Incus(runner)._check_disk_fits(config)

    runner.respond("image", "get-property", ALIAS, DISK_PROPERTY, stdout="16GiB\n")
    Incus(runner)._check_disk_fits(config)

    runner.respond("image", "get-property", stderr="Error: Property not found", returncode=1)
    Incus(runner)._check_disk_fits(config)


def test_build_image_wrapper_uses_the_builder(monkeypatch, tmp_path: Path) -> None:
    calls = []

    class FakeBuilder:
        def __init__(self, incus, **kwargs):
            calls.append(kwargs)

        def build(self, alias, source):
            calls.append((alias, source))
            return imagebuild.BuildReport.__new__(imagebuild.BuildReport)

    monkeypatch.setattr(imagebuild, "ImageBuilder", FakeBuilder)
    runner = KeyedRunner()

    Incus(runner).build_image(image="x/y", source="images:z")

    assert (("x/y", "images:z")) in calls
