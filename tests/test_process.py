import subprocess
import sys

import pytest

from sandboxsh.errors import CommandTimeoutError
from sandboxsh.process import Runner


@pytest.mark.parametrize("check", (True, False))
def test_runner_bounds_a_hung_process(check: bool) -> None:
    command = [sys.executable, "-c", "import time; time.sleep(60)"]

    with pytest.raises(CommandTimeoutError, match="timed out after 0.1s"):
        Runner().run(command, timeout=0.1, check=check)


def test_runner_passes_timeout_to_subprocess(monkeypatch) -> None:
    options = {}

    def run(args, **kwargs):
        options.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", run)

    assert Runner().run(["true"], timeout=30).stdout == "ok"
    assert options["timeout"] == 30


def test_long_running_commands_have_no_implicit_timeout(monkeypatch) -> None:
    options = {}

    def run(args, **kwargs):
        options.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)

    Runner().run(["build"], capture=False)

    assert options["timeout"] is None
