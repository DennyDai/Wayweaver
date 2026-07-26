import json
import shlex
from typing import Any

from ..errors import ActionError, ConfigError
from ..runtime import windows_uia_command
from ..types import Capability
from .base import Adapter, require_shell_transport


class UIAAdapter(Adapter):
    kind = "uia"
    capabilities = frozenset({Capability.ELEMENTS})
    raw_operations = {"tree": "Return the bounded raw Windows UI Automation tree"}

    def __init__(self, name: str, config: dict[str, Any], transport: Adapter):
        super().__init__(name, config)
        self.transport = transport
        default = windows_uia_command()
        command = str(config.get("command", default)).strip()
        if not command:
            raise ConfigError("uia command cannot be empty")
        self.helper_command = command

    async def _call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        stdin = json.dumps(params or {}).encode() if params is not None else None
        command = f"{self.helper_command} -Action {shlex.quote(action)}"
        code, stdout, stderr = await self.transport.shell(command, stdin)
        if code:
            message = stderr.decode(errors="replace").strip()
            try:
                message = json.loads(message.splitlines()[-1])["error"]
            except (IndexError, KeyError, TypeError, json.JSONDecodeError):
                pass
            raise ActionError(message or f"UIA helper exited {code}")
        return json.loads(stdout)

    async def available(self) -> tuple[bool, str | None]:
        available, reason = await self.transport.available()
        if not available:
            return False, reason
        try:
            await self._call("probe")
            return True, None
        except Exception as error:
            return False, str(error)

    async def perform(self, operation: str, params: dict[str, Any]) -> Any:
        actions = {
            "element.list": "list",
            "element.find": "find",
            "element.assert": "assert",
            "element.activate": "invoke",
            "element.focus": "focus",
            "element.focused": "focused",
            "element.read": "read",
            "element.set_value": "set-value",
        }
        if operation == "element.wait":
            action = "wait-state" if "state" in params else "wait"
            return await self._call(action, params)
        if action := actions.get(operation):
            return await self._call(action, params)
        return await super().perform(operation, params)

    async def raw(self, operation: str, params: dict[str, Any]) -> Any:
        if operation != "tree":
            raise ActionError(f"unknown UIA raw operation: {operation}")
        return await self._call("list", params)


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    transport = require_shell_transport("uia", config, adapters)
    return UIAAdapter(name, config, transport)
