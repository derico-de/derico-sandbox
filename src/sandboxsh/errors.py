class SandboxshError(RuntimeError):
    """Expected user-facing sandboxsh failure."""


class ConfigError(SandboxshError):
    """Invalid project configuration."""


class CommandError(SandboxshError):
    """An external command failed."""

    def __init__(self, command: list[str], returncode: int, stderr: str = "") -> None:
        rendered = " ".join(command)
        detail = f": {stderr.strip()}" if stderr.strip() else ""
        super().__init__(f"command failed ({returncode}): {rendered}{detail}")
        self.command = command
        self.returncode = returncode
        self.stderr = stderr
