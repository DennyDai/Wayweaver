import asyncio
import io
from typing import Any

from ..errors import ActionError
from ..motion import trajectory
from ..types import Capability, Frame
from .base import Adapter, key_expressions

_VIRTUAL_KEYS = {
    "backspace": "VK_BACK",
    "escape": "VK_ESCAPE",
    "tab": "VK_TAB",
    "return": "VK_RETURN",
    "enter": "VK_RETURN",
    "insert": "VK_INSERT",
    "delete": "VK_DELETE",
    "home": "VK_HOME",
    "end": "VK_END",
    "pageup": "VK_PRIOR",
    "page_up": "VK_PRIOR",
    "pagedown": "VK_NEXT",
    "page_down": "VK_NEXT",
    "caps_lock": "VK_CAPITAL",
    "print": "VK_SNAPSHOT",
    "menu": "VK_APPS",
    "super": "VK_LWIN",
    "meta": "VK_LWIN",
    "left": "VK_LEFT",
    "up": "VK_UP",
    "right": "VK_RIGHT",
    "down": "VK_DOWN",
    "ctrl": "VK_CONTROL",
    "control": "VK_CONTROL",
    "alt": "VK_MENU",
    "shift": "VK_SHIFT",
    "space": "VK_SPACE",
    **{f"f{index}": f"VK_F{index}" for index in range(1, 25)},
}


class RDPAdapter(Adapter):
    kind = "rdp"
    capabilities = frozenset(
        {Capability.CAPTURE, Capability.POINTER, Capability.SCROLL, Capability.KEYBOARD}
    )

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.connection = None
        self._lock = asyncio.Lock()
        self.pointer = (0, 0)

    def _fail(self, error: Any) -> None:
        """Raise an aardwolf error, discarding the connection it came from.

        The connection is cached, so once the session drops every later call is
        made against the same corpse and fails identically until the process
        restarts.
        """
        self.connection = None
        raise ActionError(str(error))

    async def available(self) -> tuple[bool, str | None]:
        try:
            __import__("aardwolf")
        except ImportError:
            return False, "install wayweaver-agent[rdp] for RDP support"
        # Reporting availability from an import alone made an unreachable host
        # look usable, so routing preferred it over a working adapter and every
        # operation then failed. Availability means the session connects.
        try:
            async with self._lock:
                await self._ensure()
        except Exception as error:
            return False, str(error)
        return True, None

    async def _ensure(self):
        if self.connection is not None:
            return self.connection
        from aardwolf.commons.factory import RDPConnectionFactory
        from aardwolf.commons.iosettings import RDPIOSettings
        from aardwolf.commons.queuedata.constants import VIDEO_FORMAT

        settings = RDPIOSettings()
        settings.channels = []
        settings.video_width = int(self.config.get("width", 1280))
        settings.video_height = int(self.config.get("height", 720))
        settings.video_bpp_min = 15
        settings.video_bpp_max = 32
        settings.video_out_format = VIDEO_FORMAT.PNG
        settings.clipboard_use_pyperclip = False
        factory = RDPConnectionFactory.from_url(str(self.config["url"]), settings)
        connection = factory.get_connection(settings)
        _, error = await connection.connect()
        if error:
            raise ActionError(str(error))
        await asyncio.sleep(float(self.config.get("settle", 0.2)))
        self.connection = connection
        return connection

    async def capture(self, params: dict[str, Any] | None = None) -> Frame:
        if params:
            raise ActionError("RDP capture does not support a window or region")
        from aardwolf.commons.queuedata.constants import VIDEO_FORMAT

        async with self._lock:
            connection = await self._ensure()
            deadline = asyncio.get_running_loop().time() + float(
                self.config.get("capture_timeout", 10)
            )
            while not connection.desktop_buffer_has_data:
                if asyncio.get_running_loop().time() >= deadline:
                    self._fail("RDP framebuffer timed out")
                await asyncio.sleep(0.1)
            image = connection.get_desktop_buffer(VIDEO_FORMAT.PIL)
            if isinstance(image, tuple):
                _, error = image
                raise ActionError(str(error))
            data = io.BytesIO()
            image.save(data, "PNG")
            return Frame(data.getvalue(), image.width, image.height, self.kind)

    async def _key(self, connection, value: str, down: bool) -> None:
        name = _VIRTUAL_KEYS.get(value.casefold())
        if name:
            _, error = await connection.send_key_virtualkey(name, down, False)
        elif len(value) == 1:
            _, error = await connection.send_key_char(ord(value), down)
        else:
            raise ActionError(f"unknown RDP key: {value}")
        if error:
            self._fail(error)

    async def act(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        from aardwolf.commons.queuedata.constants import MOUSEBUTTON

        async with self._lock:
            connection = await self._ensure()
            if action in {"move", "click", "double_click", "right_click"}:
                target = int(params["x"]), int(params["y"])
                _, error = await connection.send_mouse(
                    MOUSEBUTTON.MOUSEBUTTON_HOVER, *self.pointer, False
                )
                if error:
                    self._fail(error)
                points, duration = trajectory(
                    self.pointer, target, params.get("duration_ms")
                )
                delay = duration / max(1, len(points)) / 1000
                error = None
                for x, y in points:
                    _, error = await connection.send_mouse(
                        MOUSEBUTTON.MOUSEBUTTON_HOVER, x, y, False
                    )
                    if error:
                        self._fail(error)
                    if delay:
                        await asyncio.sleep(delay)
                self.pointer = target
                if action != "move":
                    button = (
                        MOUSEBUTTON.MOUSEBUTTON_RIGHT
                        if action == "right_click"
                        else MOUSEBUTTON.MOUSEBUTTON_LEFT
                    )
                    for _ in range(2 if action == "double_click" else 1):
                        _, error = await connection.send_mouse(button, *target, True)
                        if not error:
                            _, error = await connection.send_mouse(
                                button, *target, False
                            )
                        if error:
                            raise ActionError(str(error))
                        await asyncio.sleep(0.12)
                return {"x": target[0], "y": target[1], "action": action}
            if action == "drag":
                start = int(params["x1"]), int(params["y1"])
                end = int(params["x2"]), int(params["y2"])
                _, error = await connection.send_mouse(
                    MOUSEBUTTON.MOUSEBUTTON_HOVER, *self.pointer, False
                )
                if error:
                    self._fail(error)
                approach, approach_duration = trajectory(self.pointer, start)
                approach_delay = approach_duration / max(1, len(approach)) / 1000
                for point in approach:
                    _, error = await connection.send_mouse(
                        MOUSEBUTTON.MOUSEBUTTON_HOVER, *point, False
                    )
                    if error:
                        self._fail(error)
                    if approach_delay:
                        await asyncio.sleep(approach_delay)
                _, error = await connection.send_mouse(
                    MOUSEBUTTON.MOUSEBUTTON_LEFT, *start, True
                )
                if error:
                    self._fail(error)
                points, duration = trajectory(start, end, params.get("duration_ms"))
                delay = duration / max(1, len(points)) / 1000
                for point in points:
                    _, error = await connection.send_mouse(
                        MOUSEBUTTON.MOUSEBUTTON_LEFT, *point, True
                    )
                    if error:
                        self._fail(error)
                    if delay:
                        await asyncio.sleep(delay)
                _, error = await connection.send_mouse(
                    MOUSEBUTTON.MOUSEBUTTON_LEFT, *end, False
                )
                if error:
                    self._fail(error)
                self.pointer = end
                return {"from": list(start), "to": list(end)}
            if action == "scroll":
                direction = str(params.get("direction", "down"))
                if direction not in {"up", "down"}:
                    raise ActionError("RDP scroll supports only up or down")
                button = (
                    MOUSEBUTTON.MOUSEBUTTON_WHEEL_UP
                    if direction == "up"
                    else MOUSEBUTTON.MOUSEBUTTON_WHEEL_DOWN
                )
                x = int(params.get("x", self.pointer[0]))
                y = int(params.get("y", self.pointer[1]))
                for _ in range(max(0, int(params.get("amount", 3)))):
                    _, error = await connection.send_mouse(button, x, y, False, 120)
                    if error:
                        self._fail(error)
                return {
                    "direction": direction,
                    "amount": int(params.get("amount", 3)),
                }
            if action == "type":
                text = str(params.get("text", ""))
                for character in text:
                    await self._key(connection, character, True)
                    await self._key(connection, character, False)
                return {"characters": len(text)}
            if action in {"key", "hotkey"}:
                values = key_expressions(params)
                for expression in values:
                    parts = str(expression).split("+")
                    for part in parts:
                        await self._key(connection, part, True)
                    for part in reversed(parts):
                        await self._key(connection, part, False)
                return {"keys": values}
            raise ActionError(f"unsupported RDP action: {action}")

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.terminate()
            self.connection = None


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    return RDPAdapter(name, config)
