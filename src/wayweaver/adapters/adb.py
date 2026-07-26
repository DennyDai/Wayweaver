import asyncio
import shutil
from typing import Any

from ..errors import ActionError
from ..image import png_size
from ..types import Capability, Frame
from .base import Adapter, key_expressions
from .android_ui import AndroidUI

_KEYEVENTS = {
    "backspace": "KEYCODE_DEL",
    "delete": "KEYCODE_FORWARD_DEL",
    "enter": "KEYCODE_ENTER",
    "return": "KEYCODE_ENTER",
    "escape": "KEYCODE_ESCAPE",
    "home": "KEYCODE_HOME",
    "back": "KEYCODE_BACK",
    "tab": "KEYCODE_TAB",
    "space": "KEYCODE_SPACE",
    "up": "KEYCODE_DPAD_UP",
    "down": "KEYCODE_DPAD_DOWN",
    "left": "KEYCODE_DPAD_LEFT",
    "right": "KEYCODE_DPAD_RIGHT",
    "pageup": "KEYCODE_PAGE_UP",
    "page_up": "KEYCODE_PAGE_UP",
    "pagedown": "KEYCODE_PAGE_DOWN",
    "page_down": "KEYCODE_PAGE_DOWN",
    "end": "KEYCODE_MOVE_END",
    "insert": "KEYCODE_INSERT",
    "menu": "KEYCODE_MENU",
    **{f"f{index}": f"KEYCODE_F{index}" for index in range(1, 13)},
}


class ADBAdapter(Adapter):
    kind = "adb"
    base_capabilities = frozenset(
        {
            Capability.CAPTURE,
            Capability.POINTER,
            Capability.SCROLL,
            Capability.KEYBOARD,
            Capability.SHELL,
            Capability.VIEWER,
        }
    )
    capabilities = base_capabilities
    raw_operations = {
        "uiautomator.dump": "Return the parsed Android UIAutomator hierarchy"
    }

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.ui = AndroidUI(
            self._checked,
            str(config.get("uiautomator_path", "/sdcard/window.xml")),
        )

    def _argv(self, *args: str) -> list[str]:
        command = [str(self.config.get("command", "adb"))]
        if serial := self.config.get("serial"):
            command += ["-s", str(serial)]
        return command + list(args)

    async def _run(
        self, *args: str, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        process = await asyncio.create_subprocess_exec(
            *self._argv(*args),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate(stdin)
        return process.returncode, stdout, stderr

    async def _checked(self, *args: str) -> bytes:
        code, stdout, stderr = await self._run(*args)
        if code:
            raise ActionError(
                stderr.decode(errors="replace").strip() or f"adb exited {code}"
            )
        return stdout

    async def available(self) -> tuple[bool, str | None]:
        command = str(self.config.get("command", "adb"))
        if not shutil.which(command):
            return False, f"{command} not found"
        code, stdout, stderr = await self._run("get-state")
        if code != 0 or stdout.strip() != b"device":
            return (
                False,
                stderr.decode(errors="replace").strip() or "ADB device unavailable",
            )
        uia_code, _, _ = await self._run("shell", "command", "-v", "uiautomator")
        self.capabilities = (
            self.base_capabilities | {Capability.ELEMENTS}
            if uia_code == 0
            else self.base_capabilities
        )
        return True, None

    async def capture(self, params: dict[str, Any] | None = None) -> Frame:
        if params:
            raise ActionError("Android capture does not support a window or region")
        png = await self._checked("exec-out", "screencap", "-p")
        width, height = png_size(png)
        return Frame(png, width, height, self.kind)

    async def shell(
        self, command: str, stdin: bytes | None = None
    ) -> tuple[int, bytes, bytes]:
        return await self._run("shell", "sh", "-c", command, stdin=stdin)

    async def act(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action in {"click", "double_click"}:
            x, y = int(params["x"]), int(params["y"])
            repeats = 2 if action == "double_click" else 1
            for _ in range(repeats):
                await self._checked("shell", "input", "tap", str(x), str(y))
                if repeats == 2:
                    await asyncio.sleep(0.12)
            return {"x": x, "y": y, "action": action}
        if action == "drag":
            values = [int(params[key]) for key in ("x1", "y1", "x2", "y2")]
            duration = int(params.get("duration_ms", 500))
            await self._checked(
                "shell", "input", "swipe", *map(str, values), str(duration)
            )
            return {"from": values[:2], "to": values[2:]}
        if action == "scroll":
            direction = str(params.get("direction", "down"))
            if direction not in {"up", "down"}:
                raise ActionError("Android scroll supports only up or down")
            frame = await self.capture()
            x = int(params.get("x", frame.width // 2))
            y = int(params.get("y", frame.height // 2))
            amount = max(1, int(params.get("amount", 3)))
            distance = min(frame.height // 3, 120 * amount)
            y2 = y + distance if direction == "up" else y - distance
            await self._checked(
                "shell", "input", "swipe", str(x), str(y), str(x), str(y2), "350"
            )
            return {"direction": direction, "amount": amount}
        if action == "type":
            text = str(params.get("text", ""))
            encoded = text.replace("%", "%25").replace(" ", "%s")
            await self._checked("shell", "input", "text", encoded)
            return {"characters": len(text)}
        if action in {"key", "hotkey"}:
            keys = key_expressions(params)
            for key in keys:
                event = _KEYEVENTS.get(str(key).casefold(), str(key))
                await self._checked("shell", "input", "keyevent", event)
            return {"keys": keys}
        raise ActionError(f"unsupported Android action: {action}")

    async def perform(self, operation: str, params: dict[str, Any]) -> Any:
        if operation.startswith("element."):
            if Capability.ELEMENTS not in self.capabilities:
                raise ActionError("UIAutomator is unavailable on the Android target")
            return await self.ui.perform(operation, params)
        return await super().perform(operation, params)

    async def raw(self, operation: str, params: dict[str, Any]) -> Any:
        if operation != "uiautomator.dump":
            raise ActionError(f"unknown Android raw operation: {operation}")
        limit = max(1, int(params.get("limit", 500)))
        elements = await self.ui.elements()
        return {"elements": elements[:limit], "truncated": len(elements) > limit}

    async def viewer(self) -> dict[str, Any]:
        command = str(self.config.get("scrcpy", "scrcpy"))
        if not shutil.which(command):
            raise ActionError(f"{command} not found")
        arguments = (
            ["--serial", str(self.config["serial"])]
            if self.config.get("serial")
            else []
        )
        process = await asyncio.create_subprocess_exec(command, *arguments)
        return {"pid": process.pid, "command": command}


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    return ADBAdapter(name, config)
