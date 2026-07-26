import asyncio
import os
import shutil
import signal
from pathlib import Path
from typing import Any
from ..errors import ConfigError

from ..types import Capability
from .base import SESSION_STREAM_LIMIT, Adapter, ShellSession


class LocalAdapter(Adapter):
    kind = "local"
    capabilities = frozenset({Capability.SHELL})

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        default_shell = "powershell.exe" if os.name == "nt" else "/bin/bash"
        self.executable = str(config.get("shell", default_shell))
        default_arguments = (
            ["-NoProfile", "-NonInteractive", "-Command"] if os.name == "nt" else ["-c"]
        )
        configured_arguments = config.get("arguments", default_arguments)
        if not isinstance(configured_arguments, list) or not all(
            isinstance(value, str) for value in configured_arguments
        ):
            raise ConfigError("local arguments must be a string array")
        self.arguments = configured_arguments
        configured_environment = config.get("environment", {})
        if not isinstance(configured_environment, dict):
            raise ConfigError("local environment must be a table")
        self.environment = {
            str(key): str(value) for key, value in configured_environment.items()
        }
        configured_cwd = config.get("cwd")
        self.cwd = Path(str(configured_cwd)).expanduser() if configured_cwd else None

    async def available(self) -> tuple[bool, str | None]:
        if not shutil.which(self.executable):
            return False, f"local shell not found: {self.executable}"
        if self.cwd is not None and not self.cwd.is_dir():
            return False, f"local working directory not found: {self.cwd}"
        return True, None

    async def open_session(self, command: str) -> ShellSession | None:
        environment = os.environ.copy()
        environment.update(self.environment)
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "-c",
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=str(self.cwd) if self.cwd else None,
            env=environment,
            limit=SESSION_STREAM_LIMIT,
        )
        return ShellSession(process)

    async def shell(
        self, command: str, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        environment = os.environ.copy()
        environment.update(self.environment)
        process = await asyncio.create_subprocess_exec(
            self.executable,
            *self.arguments,
            command,
            cwd=self.cwd,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = await process.communicate(stdin)
        except asyncio.CancelledError:
            if os.name == "posix":
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
            else:
                process.terminate()
            await process.wait()
            raise
        return process.returncode, stdout, stderr


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    return LocalAdapter(name, config)
