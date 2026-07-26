import asyncio
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from ..types import Capability
from .base import SESSION_STREAM_LIMIT, Adapter, ShellSession


class SSHAdapter(Adapter):
    kind = "ssh"
    capabilities = frozenset({Capability.SHELL})

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self._probe_lock = asyncio.Lock()
        self._probe_at = 0.0
        self._probe_result: tuple[bool, str | None] | None = None
        self._control_dir = Path(tempfile.gettempdir()) / f"wayweaver-ssh-{os.getuid()}"

    async def available(self) -> tuple[bool, str | None]:
        async with self._probe_lock:
            ttl = max(0.0, float(self.config.get("probe_ttl", 5)))
            if (
                self._probe_result is not None
                and time.monotonic() - self._probe_at <= ttl
            ):
                return self._probe_result
            if not shutil.which("ssh"):
                result = False, "OpenSSH client not found"
            else:
                code, _, stderr = await self.shell("true")
                if code:
                    reason = stderr.decode(errors="replace").strip()
                    result = False, reason or f"SSH probe exited {code}"
                else:
                    result = True, None
            self._probe_result = result
            self._probe_at = time.monotonic()
            return result

    def _argv(
        self,
    ) -> tuple[list[str], dict[str, str], tempfile.TemporaryDirectory[str] | None]:
        host = self.config["host"]
        user = self.config.get("user")
        destination = f"{user}@{host}" if user else host
        args = ["ssh", "-T", "-p", str(self.config.get("port", 22))]
        args += ["-o", f"ConnectTimeout={int(self.config.get('timeout', 10))}"]
        if self.config.get("multiplex", True):
            self._control_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._control_dir.chmod(0o700)
            args += [
                "-o",
                "ControlMaster=auto",
                "-o",
                f"ControlPersist={int(self.config.get('control_persist', 60))}",
                "-o",
                f"ControlPath={self._control_dir}/%C",
            ]
        checking = self.config.get("host_key_checking", "accept-new")
        if checking == "none":
            args += [
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
            ]
        else:
            args += ["-o", f"StrictHostKeyChecking={checking}"]
        if identity := self.config.get("identity_file"):
            args += ["-i", str(Path(identity).expanduser())]
        environment = os.environ.copy()
        temporary = None
        if password := self.config.get("password"):
            temporary = tempfile.TemporaryDirectory(prefix="wayweaver-askpass-")
            helper = Path(temporary.name) / "askpass"
            helper.write_text("#!/bin/sh\nprintf '%s\n' \"$WAYWEAVER_SSH_PASSWORD\"\n")
            helper.chmod(0o700)
            environment.update(
                {
                    "DISPLAY": environment.get("DISPLAY", ":0"),
                    "WAYWEAVER_SSH_PASSWORD": str(password),
                    "SSH_ASKPASS": str(helper),
                    "SSH_ASKPASS_REQUIRE": "force",
                }
            )
        args.append(destination)
        return args, environment, temporary

    async def open_session(self, command: str) -> ShellSession | None:
        args, environment, temporary = self._argv()
        process = await asyncio.create_subprocess_exec(
            *args,
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            limit=SESSION_STREAM_LIMIT,
        )
        return ShellSession(process, temporary)

    async def shell(
        self, command: str, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        args, environment, temporary = self._argv()
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=environment,
            )
            stdout, stderr = await process.communicate(stdin)
            return process.returncode, stdout, stderr
        finally:
            if temporary:
                temporary.cleanup()


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    return SSHAdapter(name, config)
