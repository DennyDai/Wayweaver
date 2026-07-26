import asyncio
import socket
import struct
import subprocess
import time
from typing import Any

from ..errors import ActionError, ProtocolError
from ..image import encode_png
from ..motion import trajectory
from ..types import Capability, Frame
from .base import Adapter, key_expressions

_KEYSYMS = {
    "backspace": 0xFF08,
    "tab": 0xFF09,
    "return": 0xFF0D,
    "enter": 0xFF0D,
    "escape": 0xFF1B,
    "delete": 0xFFFF,
    "home": 0xFF50,
    "left": 0xFF51,
    "up": 0xFF52,
    "right": 0xFF53,
    "down": 0xFF54,
    "pageup": 0xFF55,
    "page_up": 0xFF55,
    "pagedown": 0xFF56,
    "page_down": 0xFF56,
    "end": 0xFF57,
    "insert": 0xFF63,
    "shift": 0xFFE1,
    "ctrl": 0xFFE3,
    "control": 0xFFE3,
    "alt": 0xFFE9,
    "meta": 0xFFE7,
    "super": 0xFFEB,
    "space": 0x20,
    "caps_lock": 0xFFE5,
    "print": 0xFF61,
    "menu": 0xFF67,
    **{f"f{index}": 0xFFBE + index - 1 for index in range(1, 25)},
}


def _exact(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ProtocolError("VNC connection closed")
        data.extend(chunk)
    return bytes(data)


def _reverse_bits(value: int) -> int:
    return int(f"{value:08b}"[::-1], 2)


def _vnc_response(password: str, challenge: bytes) -> bytes:
    key = bytes(
        _reverse_bits(byte) for byte in password.encode("latin1")[:8].ljust(8, b"\0")
    )
    process = subprocess.run(
        [
            "openssl",
            "enc",
            "-des-ecb",
            "-K",
            key.hex(),
            "-nosalt",
            "-nopad",
            "-provider",
            "legacy",
            "-provider",
            "default",
        ],
        input=challenge,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.returncode:
        raise ProtocolError(process.stderr.decode(errors="replace").strip())
    return process.stdout


class RFB:
    def __init__(self, host: str, port: int, password: str | None, timeout: float):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self.password = password
        self.width = 0
        self.height = 0
        self.name = ""
        self._handshake()

    def _handshake(self) -> None:
        server_version = _exact(self.sock, 12)
        if not server_version.startswith(b"RFB "):
            raise ProtocolError("invalid VNC protocol banner")
        version = (
            b"RFB 003.008\n" if server_version >= b"RFB 003.007" else server_version
        )
        self.sock.sendall(version)
        if version == b"RFB 003.003\n":
            security = struct.unpack(">I", _exact(self.sock, 4))[0]
        else:
            count = _exact(self.sock, 1)[0]
            if count == 0:
                size = struct.unpack(">I", _exact(self.sock, 4))[0]
                raise ProtocolError(_exact(self.sock, size).decode(errors="replace"))
            offered = _exact(self.sock, count)
            security = 2 if self.password and 2 in offered else 1 if 1 in offered else 0
            if not security:
                raise ProtocolError(f"unsupported VNC security types: {list(offered)}")
            self.sock.sendall(bytes([security]))
        if security == 2:
            if not self.password:
                raise ProtocolError("VNC password required")
            self.sock.sendall(_vnc_response(self.password, _exact(self.sock, 16)))
        if version != b"RFB 003.003\n" or security == 2:
            result = struct.unpack(">I", _exact(self.sock, 4))[0]
            if result:
                reason = "authentication failed"
                if version != b"RFB 003.003\n":
                    size = struct.unpack(">I", _exact(self.sock, 4))[0]
                    reason = _exact(self.sock, size).decode(errors="replace")
                raise ProtocolError(reason)
        self.sock.sendall(b"\x01")
        header = _exact(self.sock, 24)
        self.width, self.height = struct.unpack(">HH", header[:4])
        name_size = struct.unpack(">I", header[20:24])[0]
        self.name = _exact(self.sock, name_size).decode(errors="replace")
        pixel_format = struct.pack(
            ">BBBBHHHBBBxxx", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0
        )
        self.sock.sendall(b"\x00\x00\x00\x00" + pixel_format)
        self.sock.sendall(struct.pack(">BBHii", 2, 0, 2, 0, -223))

    def capture(self) -> Frame:
        self.sock.sendall(struct.pack(">BBHHHH", 3, 0, 0, 0, self.width, self.height))
        rgb = bytearray(self.width * self.height * 3)
        while True:
            message = _exact(self.sock, 1)[0]
            if message == 0:
                _exact(self.sock, 1)
                rectangles = struct.unpack(">H", _exact(self.sock, 2))[0]
                for _ in range(rectangles):
                    x, y, width, height, encoding = struct.unpack(
                        ">HHHHi", _exact(self.sock, 12)
                    )
                    if encoding == -223:
                        self.width, self.height = width, height
                        rgb = bytearray(width * height * 3)
                        continue
                    if encoding != 0:
                        raise ProtocolError(f"unsupported VNC encoding: {encoding}")
                    pixels = _exact(self.sock, width * height * 4)
                    for row in range(height):
                        source = row * width * 4
                        target = ((y + row) * self.width + x) * 3
                        for column in range(width):
                            blue, green, red = pixels[
                                source + column * 4 : source + column * 4 + 3
                            ]
                            offset = target + column * 3
                            rgb[offset : offset + 3] = bytes((red, green, blue))
                return Frame(
                    encode_png(self.width, self.height, bytes(rgb)),
                    self.width,
                    self.height,
                    "vnc",
                )
            if message == 1:
                _, count = struct.unpack(">xHH", _exact(self.sock, 5))
                _exact(self.sock, count * 6)
            elif message == 2:
                continue
            elif message == 3:
                size = struct.unpack(">xxxI", _exact(self.sock, 7))[0]
                _exact(self.sock, size)
            else:
                raise ProtocolError(f"unknown VNC server message: {message}")

    def pointer(self, x: int, y: int, mask: int = 0) -> None:
        self.sock.sendall(struct.pack(">BBHH", 5, mask, x, y))

    def key(self, keysym: int, down: bool) -> None:
        self.sock.sendall(struct.pack(">BBxxI", 4, int(down), keysym))

    def close(self) -> None:
        self.sock.close()


class VNCAdapter(Adapter):
    kind = "vnc"
    capabilities = frozenset(
        {Capability.CAPTURE, Capability.POINTER, Capability.SCROLL, Capability.KEYBOARD}
    )

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.pointer = (0, 0)

    def _connect(self) -> RFB:
        return RFB(
            str(self.config["host"]),
            int(self.config.get("port", 5900)),
            self.config.get("password"),
            float(self.config.get("timeout", 10)),
        )

    async def available(self) -> tuple[bool, str | None]:
        try:
            connection = await asyncio.to_thread(self._connect)
            self.pointer = (connection.width // 2, connection.height // 2)
            await asyncio.to_thread(connection.close)
            return True, None
        except Exception as error:
            return False, str(error)

    async def capture(self, params: dict[str, Any] | None = None) -> Frame:
        if params:
            raise ActionError("VNC capture does not support a window or region")

        def run() -> Frame:
            connection = self._connect()
            try:
                return connection.capture()
            finally:
                connection.close()

        return await asyncio.to_thread(run)

    def _keysym(self, value: str) -> int:
        lowered = value.casefold()
        if lowered in _KEYSYMS:
            return _KEYSYMS[lowered]
        if len(value) == 1:
            return ord(value)
        if (
            lowered.startswith("f")
            and lowered[1:].isdigit()
            and 1 <= int(lowered[1:]) <= 24
        ):
            return 0xFFBD + int(lowered[1:])
        raise ActionError(f"unknown VNC key: {value}")

    def _act_sync(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        connection = self._connect()
        try:
            if action in {"move", "click", "double_click", "right_click"}:
                target = int(params["x"]), int(params["y"])
                connection.pointer(*self.pointer)
                points, duration = trajectory(
                    self.pointer, target, params.get("duration_ms")
                )
                delay = duration / max(1, len(points)) / 1000
                for x, y in points:
                    connection.pointer(x, y)
                    time.sleep(delay)
                self.pointer = target
                if action != "move":
                    button = (
                        4
                        if action == "right_click"
                        else 1 << (int(params.get("button", 1)) - 1)
                    )
                    repeats = 2 if action == "double_click" else 1
                    for _ in range(repeats):
                        connection.pointer(*target, button)
                        time.sleep(0.07)
                        connection.pointer(*target, 0)
                        time.sleep(0.12)
                return {"x": target[0], "y": target[1], "action": action}
            if action == "drag":
                start = int(params["x1"]), int(params["y1"])
                end = int(params["x2"]), int(params["y2"])
                connection.pointer(*self.pointer)
                first, _ = trajectory(self.pointer, start)
                for point in first:
                    connection.pointer(*point)
                button = 1 << (int(params.get("button", 1)) - 1)
                connection.pointer(*start, button)
                points, duration = trajectory(start, end, params.get("duration_ms"))
                delay = duration / max(1, len(points)) / 1000
                for point in points:
                    connection.pointer(*point, button)
                    time.sleep(delay)
                connection.pointer(*end, 0)
                self.pointer = end
                return {"from": list(start), "to": list(end)}
            if action == "scroll":
                target = (
                    (int(params["x"]), int(params["y"]))
                    if "x" in params
                    else self.pointer
                )
                connection.pointer(*target)
                direction = params.get("direction", "down")
                masks = {"up": 8, "down": 16, "left": 32, "right": 64}
                if direction not in masks:
                    raise ActionError(f"invalid scroll direction: {direction}")
                for _ in range(max(0, int(params.get("amount", 3)))):
                    connection.pointer(*target, masks[direction])
                    connection.pointer(*target, 0)
                    time.sleep(0.06)
                return {"direction": direction, "amount": int(params.get("amount", 3))}
            if action == "type":
                text = str(params.get("text", ""))
                for character in text:
                    keysym = ord(character)
                    connection.key(keysym, True)
                    connection.key(keysym, False)
                return {"characters": len(text)}
            if action in {"key", "hotkey"}:
                values = key_expressions(params)
                for expression in values:
                    parts = expression.split("+")
                    keysyms = [self._keysym(part) for part in parts]
                    for keysym in keysyms:
                        connection.key(keysym, True)
                    for keysym in reversed(keysyms):
                        connection.key(keysym, False)
                return {"keys": values}
            raise ActionError(f"unsupported VNC action: {action}")
        finally:
            connection.close()

    async def act(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._act_sync, action, params)


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    return VNCAdapter(name, config)
