class SandboxshError(RuntimeError):
    """Expected user-facing sandboxsh failure."""


class ConfigError(SandboxshError):
    """Invalid project configuration."""


class CommandTimeoutError(SandboxshError):
    """An external command exceeded its deadline."""

    def __init__(self, command: list[str], timeout: float) -> None:
        super().__init__(f"command timed out after {timeout:g}s: {command[0]}")
        self.command = command
        self.timeout = timeout


class CommandError(SandboxshError):
    """An external command failed."""

    def __init__(self, command: list[str], returncode: int, stderr: str = "") -> None:
        rendered = " ".join(command)
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        super().__init__(f"command failed ({returncode}): {rendered}{detail}")
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
