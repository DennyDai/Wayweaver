import asyncio
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import threading
import urllib.parse
import urllib.request
from typing import Any

from ..errors import ActionError, ProtocolError
from ..image import png_size
from ..motion import trajectory
from ..types import Capability, Frame
from .base import Adapter, key_expressions

# Chrome only performs a key's default action -- scrolling, caret movement,
# form submission -- when the event carries a virtual key code. Dispatching a
# `key` name alone delivers an event the page can observe but the browser will
# not act on, so navigation keys silently did nothing.
_CDP_VIRTUAL_KEYS = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
    "Escape": 27,
    " ": 32,
    "PageUp": 33,
    "PageDown": 34,
    "End": 35,
    "Home": 36,
    "ArrowLeft": 37,
    "ArrowUp": 38,
    "ArrowRight": 39,
    "ArrowDown": 40,
    "Insert": 45,
    "Delete": 46,
    **{f"F{index}": 111 + index for index in range(1, 13)},
}


class WebSocket:
    def __init__(self, url: str, timeout: float):
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        sock = socket.create_connection((parsed.hostname, port), timeout=timeout)
        if parsed.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(
                sock, server_hostname=parsed.hostname
            )
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.netloc}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
        sock.sendall(request)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            response.extend(sock.recv(4096))
        header, remainder = bytes(response).split(b"\r\n\r\n", 1)
        if b" 101 " not in header.split(b"\r\n", 1)[0]:
            raise ProtocolError(header.decode(errors="replace"))
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        )
        if b"sec-websocket-accept: " + expected.lower() not in header.lower():
            raise ProtocolError("invalid WebSocket accept key")
        self.sock = sock
        self.buffer = bytearray(remainder)

    def _read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            data = self.sock.recv(max(4096, size - len(self.buffer)))
            if not data:
                raise ProtocolError("WebSocket closed")
            self.buffer.extend(data)
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    def send(self, payload: bytes, opcode: int = 1) -> None:
        mask = os.urandom(4)
        size = len(payload)
        header = bytearray([0x80 | opcode])
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", size))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", size))
        header.extend(mask)
        header.extend(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self.sock.sendall(header)

    def receive(self) -> bytes:
        payload = bytearray()
        while True:
            first, second = self._read(2)
            final, opcode = bool(first & 0x80), first & 0x0F
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if second & 0x80 else None
            data = bytearray(self._read(size))
            if mask:
                for index in range(size):
                    data[index] ^= mask[index % 4]
            if opcode == 8:
                raise ProtocolError("WebSocket peer closed")
            if opcode == 9:
                self.send(bytes(data), 10)
                continue
            if opcode in (1, 2, 0):
                payload.extend(data)
                if final:
                    return bytes(payload)

    def close(self) -> None:
        try:
            self.send(b"", 8)
        finally:
            self.sock.close()


class CDPAdapter(Adapter):
    kind = "cdp"
    capabilities = frozenset(
        {
            Capability.BROWSER,
            Capability.CAPTURE,
            Capability.POINTER,
            Capability.SCROLL,
            Capability.KEYBOARD,
        }
    )
    raw_operations = {"call": "Call an arbitrary Chrome DevTools Protocol method"}

    def __init__(self, name: str, config: dict[str, Any]):
        super().__init__(name, config)
        self.base = str(config["url"]).rstrip("/")
        self.timeout = float(config.get("timeout", 10))
        self.pointer = (0, 0)
        self._socket: WebSocket | None = None
        self._next_id = 0
        self._lock = threading.Lock()

    def _json(self, path: str) -> Any:
        with urllib.request.urlopen(self.base + path, timeout=self.timeout) as response:
            return json.load(response)

    def _websocket_url(self) -> str:
        scope = self.config.get("scope", "page")
        if scope == "browser":
            return str(self._json("/json/version")["webSocketDebuggerUrl"])
        pages = [
            target
            for target in self._json("/json/list")
            if target.get("type") == "page"
        ]
        if not pages:
            raise ProtocolError("CDP has no page target")
        return str(pages[0]["webSocketDebuggerUrl"])

    def _drop_socket(self) -> None:
        socket_, self._socket = self._socket, None
        if socket_ is not None:
            try:
                socket_.sock.close()
            except OSError:
                pass

    def _call(self, method: str, params: dict[str, Any]) -> Any:
        with self._lock:
            if self._socket is None:
                self._socket = WebSocket(self._websocket_url(), self.timeout)
            self._next_id += 1
            identifier = self._next_id
            # A transport failure must not leave the dead socket in place, or
            # every later call reuses it and the adapter stays broken for the
            # rest of the process. A CDP method error is not a transport
            # failure, so it is raised below without discarding the connection.
            try:
                self._socket.send(
                    json.dumps(
                        {"id": identifier, "method": method, "params": params}
                    ).encode()
                )
                while True:
                    message = json.loads(self._socket.receive())
                    if message.get("id") == identifier:
                        break
            except (OSError, ProtocolError):
                self._drop_socket()
                raise
            if "error" in message:
                raise ProtocolError(
                    message["error"].get("message", str(message["error"]))
                )
            return message.get("result")

    async def surface_session(self) -> str:
        if configured := self.config.get("session_id"):
            return str(configured)
        version = await asyncio.to_thread(self._json, "/json/version")
        return str(version["webSocketDebuggerUrl"])

    async def available(self) -> tuple[bool, str | None]:
        try:
            await asyncio.to_thread(self._json, "/json/version")
            return True, None
        except Exception as error:
            return False, str(error)

    async def browser(self, method: str, params: dict[str, Any]) -> Any:
        return await asyncio.to_thread(self._call, method, params)

    async def capture(self, params: dict[str, Any] | None = None) -> Frame:
        options: dict[str, Any] = {"format": "png"}
        if params and (region := params.get("region")):
            if not isinstance(region, list) or len(region) != 4:
                raise ActionError("region must be [left, top, right, bottom]")
            left, top, right, bottom = map(int, region)
            options["clip"] = {
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
                "scale": 1,
            }
        result = await self.browser("Page.captureScreenshot", options)
        png = base64.b64decode(result["data"])
        width, height = png_size(png)
        return Frame(png, width, height, self.kind)

    async def _mouse(self, event: str, x: int, y: int, **extra: Any) -> None:
        await self.browser(
            "Input.dispatchMouseEvent", {"type": event, "x": x, "y": y, **extra}
        )

    async def act(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action in {"move", "click", "double_click", "right_click"}:
            target = int(params["x"]), int(params["y"])
            await self._mouse("mouseMoved", *self.pointer)
            points, duration = trajectory(
                self.pointer, target, params.get("duration_ms")
            )
            delay = duration / max(1, len(points)) / 1000
            for x, y in points:
                await self._mouse("mouseMoved", x, y)
                if delay:
                    await asyncio.sleep(delay)
            self.pointer = target
            if action != "move":
                button_number = int(params.get("button", 1))
                button = (
                    "right"
                    if action == "right_click" or button_number == 3
                    else "middle"
                    if button_number == 2
                    else "left"
                )
                count = 2 if action == "double_click" else 1
                for click_count in range(1, count + 1):
                    await self._mouse(
                        "mousePressed", *target, button=button, clickCount=click_count
                    )
                    await self._mouse(
                        "mouseReleased", *target, button=button, clickCount=click_count
                    )
            return {"x": target[0], "y": target[1], "action": action}
        if action == "drag":
            start = int(params["x1"]), int(params["y1"])
            end = int(params["x2"]), int(params["y2"])
            await self.act("move", {"x": start[0], "y": start[1]})
            await self._mouse("mousePressed", *start, button="left", clickCount=1)
            points, duration = trajectory(start, end, params.get("duration_ms"))
            delay = duration / max(1, len(points)) / 1000
            for x, y in points:
                await self._mouse("mouseMoved", x, y, button="left", buttons=1)
                if delay:
                    await asyncio.sleep(delay)
            await self._mouse("mouseReleased", *end, button="left", clickCount=1)
            self.pointer = end
            return {"from": list(start), "to": list(end)}
        if action == "scroll":
            x, y = (
                int(params.get("x", self.pointer[0])),
                int(params.get("y", self.pointer[1])),
            )
            direction = params.get("direction", "down")
            if direction not in {"up", "down", "left", "right"}:
                raise ActionError(f"invalid scroll direction: {direction}")
            delta = int(params.get("amount", 3)) * 100
            delta_x = (
                -delta if direction == "left" else delta if direction == "right" else 0
            )
            delta_y = (
                -delta if direction == "up" else delta if direction == "down" else 0
            )
            await self._mouse("mouseWheel", x, y, deltaX=delta_x, deltaY=delta_y)
            return {"direction": direction, "amount": int(params.get("amount", 3))}
        if action == "type":
            text = str(params.get("text", ""))
            await self.browser("Input.insertText", {"text": text})
            return {"characters": len(text)}
        if action in {"key", "hotkey"}:
            values = key_expressions(params)
            for expression in values:
                parts = str(expression).split("+")
                modifiers = 0
                for modifier in parts[:-1]:
                    modifiers |= {
                        "alt": 1,
                        "ctrl": 2,
                        "control": 2,
                        "meta": 4,
                        "shift": 8,
                    }.get(modifier.casefold(), 0)
                raw_key = parts[-1]
                key = {
                    "return": "Enter",
                    "enter": "Enter",
                    "escape": "Escape",
                    "backspace": "Backspace",
                    "delete": "Delete",
                    "ctrl": "Control",
                    "control": "Control",
                    "alt": "Alt",
                    "shift": "Shift",
                    "meta": "Meta",
                    "space": " ",
                    "tab": "Tab",
                    "home": "Home",
                    "end": "End",
                    "insert": "Insert",
                    "page_up": "PageUp",
                    "pageup": "PageUp",
                    "page_down": "PageDown",
                    "pagedown": "PageDown",
                    "up": "ArrowUp",
                    "down": "ArrowDown",
                    "left": "ArrowLeft",
                    "right": "ArrowRight",
                    "caps_lock": "CapsLock",
                    "super": "Meta",
                }.get(raw_key.casefold(), raw_key)
                event: dict[str, Any] = {"key": key, "modifiers": modifiers}
                if (code := _CDP_VIRTUAL_KEYS.get(key)) is not None:
                    event["windowsVirtualKeyCode"] = code
                    event["nativeVirtualKeyCode"] = code
                await self.browser(
                    "Input.dispatchKeyEvent",
                    {"type": "rawKeyDown" if code is not None else "keyDown", **event},
                )
                await self.browser(
                    "Input.dispatchKeyEvent", {"type": "keyUp", **event}
                )
            return {"keys": values}
        raise ActionError(f"unsupported CDP action: {action}")

    async def perform(self, operation: str, params: dict[str, Any]) -> Any:
        if operation == "browser.navigate":
            return await self.browser("Page.navigate", {"url": str(params["url"])})
        if operation == "tab.list":
            return {"tabs": await asyncio.to_thread(self._json, "/json/list")}
        if operation == "browser.read":
            selector = json.dumps(str(params.get("selector", "body")))
            expression = f"document.querySelector({selector})?.innerText ?? ''"
            result = await self.browser(
                "Runtime.evaluate", {"expression": expression, "returnByValue": True}
            )
            return {"text": result["result"].get("value", "")}
        if operation == "browser.click":
            selector = json.dumps(str(params["selector"]))
            expression = f"(() => {{ const element = document.querySelector({selector}); if (!element) return false; element.click(); return true; }})()"
            result = await self.browser(
                "Runtime.evaluate", {"expression": expression, "returnByValue": True}
            )
            return {"clicked": bool(result["result"].get("value"))}
        if operation == "browser.type":
            selector = json.dumps(str(params["selector"]))
            clear = "element.value = '';" if params.get("clear", True) else ""
            expression = (
                f"(() => {{ const element = document.querySelector({selector}); "
                f"if (!element) return false; element.focus(); {clear} return true; }})()"
            )
            result = await self.browser(
                "Runtime.evaluate", {"expression": expression, "returnByValue": True}
            )
            if not result["result"].get("value"):
                raise ActionError(f"browser element not found: {params['selector']!r}")
            text = str(params.get("text", ""))
            await self.browser("Input.insertText", {"text": text})
            return {"characters": len(text)}
        return await super().perform(operation, params)

    async def raw(self, operation: str, params: dict[str, Any]) -> Any:
        if operation != "call":
            raise ActionError(f"unknown CDP raw operation: {operation}")
        return await self.browser(str(params["method"]), params.get("params", {}))

    async def close(self) -> None:
        if self._socket:
            await asyncio.to_thread(self._socket.close)
            self._socket = None


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    return CDPAdapter(name, config)
