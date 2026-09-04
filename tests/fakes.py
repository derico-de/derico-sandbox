"""Test doubles shared by the Incus, builder, and CLI tests."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from sandboxsh.errors import CommandError, SandboxshError
from sandboxsh.process import Result

Responder = Result | Callable[[list[str]], Result]


def operation(command: list[str]) -> list[str]:
    """Strip the fixed `incus --force-local --project X` / `sudo incus` prefixes."""
    if command[:3] == ["sudo", "incus", "--force-local"]:
        return ["sudo", *command[3:]]
    if command[:3] == ["incus", "--force-local", "--project"]:
        return command[4:]
    return command


@dataclass
class KeyedRunner:
    """Command-prefix to response, so a multi-stage loop never depends on counting.

    The longest matching prefix wins; anything unmatched succeeds silently.
    """

    responses: dict[tuple[str, ...], Responder] = field(default_factory=dict)
    commands: list[list[str]] = field(default_factory=list)
    kwargs: list[dict] = field(default_factory=list)

    def respond(
        self, *prefix: str, stdout: str = "", stderr: str = "", returncode: int = 0
    ) -> None:
        self.responses[prefix] = Result(stdout, stderr, returncode)

    def respond_with(self, *prefix: str, handler: Callable[[list[str]], Result]) -> None:
        self.responses[prefix] = handler

    def run(self, command, **kwargs) -> Result:
        argv = [str(part) for part in command]
        self.commands.append(argv)
        self.kwargs.append(kwargs)
        op = operation(argv)
        for prefix in sorted(self.responses, key=len, reverse=True):
            if tuple(op[: len(prefix)]) == prefix:
                responder = self.responses[prefix]
                result = responder(op) if callable(responder) else responder
                if kwargs.get("check", True) and result.returncode:
                    raise CommandError(argv, result.returncode, result.stderr)
                return result
        return Result("", "", 0)

    def operations(self) -> list[list[str]]:
        return [operation(command) for command in self.commands]

    def matching(self, *prefix: str) -> list[list[str]]:
        return [op for op in self.operations() if tuple(op[: len(prefix)]) == prefix]


@dataclass
class FakeLock:
    """Records acquisitions; can refuse a non-waiting caller."""

    busy: bool = False
    acquired: int = 0
    waits: list[bool] = field(default_factory=list)

    @contextmanager
    def held(self, *, wait: bool = True) -> Iterator[None]:
        self.waits.append(wait)
        if self.busy and not wait:
            raise SandboxshError("build lock is held")
        self.acquired += 1
        yield


def cache_listing(records: list[dict]) -> Result:
    return Result(json.dumps(records), "", 0)


def cache_record(
    key: str,
    stage: str,
    parent: str | None,
    created: str,
    *,
    source: str = "",
    generation: int = 0,
    stage_generation: int = 0,
    build_allow: str = "",
    script: str = "",
    inputs: str = "",
) -> dict:
    """One `incus list --format=json` row for a cache entry."""
    prefix = "user.sandboxsh.cache."
    return {
        "name": f"sandboxsh-cache-{key}",
        "status": "Stopped",
        "config": {
            f"{prefix}key": key,
            f"{prefix}stage": stage,
            f"{prefix}parent": parent or "",
            f"{prefix}created": created,
            f"{prefix}source": source,
            f"{prefix}generation": str(generation),
            f"{prefix}stage_generation": str(stage_generation),
            f"{prefix}build_allow": build_allow,
            f"{prefix}script": script,
            f"{prefix}inputs": inputs,
        },
    }
