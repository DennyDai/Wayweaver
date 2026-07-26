import asyncio
import json
from abc import ABC
from typing import Any

from ..errors import ActionError, CapabilityError, ConfigError
from ..operations import OPERATIONS
from ..types import Capability, Frame


class Adapter(ABC):
    kind: str
    capabilities: frozenset[Capability] = frozenset()
    raw_operations: dict[str, str] = {}

    def __init__(self, name: str, config: dict[str, Any]):
        self.name = name
        self.config = config

    async def available(self) -> tuple[bool, str | None]:
        return True, None

    async def surface_session(self) -> str:
        return str(self.config.get("session_id", f"{self.kind}:{self.name}"))

    async def capture(self, params: dict[str, Any] | None = None) -> Frame:
        raise CapabilityError(f"{self.kind} does not capture screens")

    async def act(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        raise CapabilityError(f"{self.kind} does not support {action}")

    async def shell(
        self, command: str, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        raise CapabilityError(f"{self.kind} does not provide a shell")

    async def browser(self, method: str, params: dict[str, Any]) -> Any:
        raise CapabilityError(f"{self.kind} does not control a browser")

    async def windows(self) -> list[dict[str, Any]]:
        raise CapabilityError(f"{self.kind} does not inspect windows")

    async def viewer(self) -> dict[str, Any]:
        raise CapabilityError(f"{self.kind} does not launch a viewer")

    async def perform(self, operation: str, params: dict[str, Any]) -> Any:
        if operation == "pointer.click":
            button = int(params.get("button", 1))
            count = int(params.get("count", 1))
            if count == 2 and button == 1:
                return await self.act("double_click", params)
            action = "right_click" if button == 3 else "click"
            result = None
            for _ in range(count):
                result = await self.act(action, params)
            return result
        spec = OPERATIONS.get(operation)
        if spec and spec.action:
            return await self.act(spec.action, params)
        if operation == "window.list":
            return {"windows": await self.windows()}
        if operation == "shell.execute":
            stdin = params.get("stdin")
            code, stdout, stderr = await self.shell(
                str(params["command"]),
                stdin.encode() if isinstance(stdin, str) else stdin,
            )
            allowed = params.get("allowed_exit_codes", [0])
            success = code in allowed
            if params.get("check") and not success:
                raise ActionError(
                    f"shell command exited {code}",
                    details={
                        "exit_code": code,
                        "allowed_exit_codes": allowed,
                    },
                )
            return {
                "exit_code": code,
                "success": success,
                "stdout": stdout.decode(errors="replace"),
                "stderr": stderr.decode(errors="replace"),
            }
        if operation == "viewer.open":
            return await self.viewer()
        raise CapabilityError(f"{self.kind} does not perform {operation}")

    async def open_session(self, command: str) -> "ShellSession | None":
        """Start a long-lived process for `command`, or None if unsupported."""
        return None

    async def raw(self, operation: str, params: dict[str, Any]) -> Any:
        raise CapabilityError(f"{self.kind} does not expose raw operation {operation}")

    async def close(self) -> None:
        return None


#: Accessibility trees are the largest thing a helper returns, and a browser's
#: runs to megabytes on one line. asyncio's 64 KiB default made `readline` fail
#: on exactly the applications worth inspecting, so sessions raise the ceiling.
SESSION_STREAM_LIMIT = 16 * 1024 * 1024


class ShellSession:
    """A long-lived remote process exchanging newline-delimited JSON.

    Some helpers pay a large fixed cost per connection -- libatspi initializes
    by marshalling every application's accessible tree -- so re-spawning them
    per call dominates the work they do. A session pays that once.
    """

    def __init__(self, process: Any, cleanup: Any = None):
        self.process = process
        self._cleanup = cleanup
        self._lock = asyncio.Lock()

    async def _readline(self, timeout: float) -> bytes:
        try:
            return await asyncio.wait_for(self.process.stdout.readline(), timeout)
        except ValueError as error:
            # readline turns an over-long line into a bare ValueError whose text
            # says nothing about which limit was hit.
            raise ActionError(
                f"session reply exceeded {SESSION_STREAM_LIMIT} bytes; "
                "narrow the request with max_depth or limit"
            ) from error

    async def banner(self, timeout: float) -> dict[str, Any]:
        """Consume the greeting the server writes before any request.

        Leaving it in the stream shifts every reply by one, so each call
        receives the previous call's answer.
        """
        line = await self._readline(timeout)
        if not line:
            raise ActionError("session closed before announcing itself")
        return json.loads(line)

    async def request(self, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        async with self._lock:
            if self.process.returncode is not None:
                raise ActionError("session process has exited")
            self.process.stdin.write((json.dumps(payload) + "\n").encode())
            await self.process.stdin.drain()
            line = await self._readline(timeout)
        if not line:
            raise ActionError("session closed before replying")
        return json.loads(line)

    async def close(self) -> None:
        try:
            if self.process.returncode is None:
                self.process.stdin.close()
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), 5)
        except (ProcessLookupError, asyncio.TimeoutError, OSError):
            pass
        finally:
            if self._cleanup is not None:
                self._cleanup.cleanup()


def key_expressions(params: dict[str, Any]) -> list[str]:
    """Normalize a key action's payload to a list of key expressions.

    Every input backend accepted `key`/`keys` as either a string or a list and
    repeated the same validation, which is how the x11 copy drifted far enough
    to mangle keysym names. Splitting an expression on `+` stays with the
    backend, since each one names modifiers differently.
    """
    values = params.get("keys", params.get("key"))
    values = [values] if isinstance(values, str) else values
    if not isinstance(values, list) or not all(
        isinstance(value, str) for value in values
    ):
        raise ActionError("key action requires a key string or list")
    return values


def require_shell_transport(
    owner: str,
    config: dict[str, Any],
    adapters: dict[str, Adapter],
) -> Adapter:
    reference = config.get("transport")
    if not isinstance(reference, str) or not reference:
        raise ConfigError(f"{owner} adapter requires a transport name")
    transport = adapters.get(reference)
    if transport is None or Capability.SHELL not in transport.capabilities:
        raise ConfigError(
            f"{owner} adapter requires shell-capable transport {reference!r}"
        )
    return transport
