from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .errors import CommandError


@dataclass(frozen=True)
class Result:
    stdout: str
    stderr: str
    returncode: int


class Runner:
    """Small subprocess seam used by the CLI and tests."""

    def run(
        self,
        command: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        capture: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> Result:
        args = [str(part) for part in command]
        completed = subprocess.run(
            args,
            input=input_text,
            text=True,
            capture_output=capture,
            check=False,
            env=env,
        )
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
        if check and completed.returncode:
            raise CommandError(args, completed.returncode, stderr)
        return Result(stdout=stdout, stderr=stderr, returncode=completed.returncode)

    def exec(self, command: Sequence[str], *, env: Mapping[str, str] | None = None) -> None:
        """Replace sandboxsh with an interactive command."""
        import os

        args = [str(part) for part in command]
        os.execvpe(args[0], args, dict(env or os.environ))
