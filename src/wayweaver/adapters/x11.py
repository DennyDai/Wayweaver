import asyncio
import json
import re
import shlex
import time
from typing import Any
from uuid import uuid4

from ..errors import ActionError
from ..image import png_size
from ..motion import trajectory
from ..runtime import linux_path_export
from ..types import Capability, Frame
from .base import Adapter, key_expressions, require_shell_transport


def _chord_key(key: str) -> str:
    """Lower a bare letter inside a chord so `ctrl+A` means Ctrl+A, not Ctrl+Shift+A.

    Everything else keeps its case: X11 resolves keysym names through
    XStringToKeysym, where `Return`, `Tab`, `F4`, and `Left` are case-sensitive
    and their lowercase spellings do not exist. Modifier aliases (`ctrl`, `alt`,
    `shift`, `super`) are matched case-insensitively by xdotool itself.
    """
    return key.lower() if len(key) == 1 and key.isalpha() else key


class X11Adapter(Adapter):
    kind = "x11"
    capabilities = frozenset(
        {
            Capability.APPLICATIONS,
            Capability.CAPTURE,
            Capability.CLIPBOARD,
            Capability.KEYBOARD,
            Capability.POINTER,
            Capability.SCROLL,
            Capability.WINDOWS,
            Capability.WORKSPACES,
            Capability.RECORDING,
        }
    )
    raw_operations = {
        "wmctrl": "Call wmctrl with an argument list",
        "xdotool": "Call xdotool with an argument list",
        "xprop": "Inspect X properties with an argument list",
        "xrandr": "Inspect XRandR state with an argument list",
        "xwininfo": "Inspect X windows with an argument list",
    }
    _WINDOW_ACTIONS = frozenset(
        {
            "wait_window",
            "assert_window",
            "focus_window",
            "close_window",
            "move_window",
            "resize_window",
            "minimize_window",
            "maximize_window",
            "fullscreen_window",
            "restore_window",
        }
    )
    _WORKSPACE_ACTIONS = frozenset({"list_workspaces", "switch_workspace"})

    def __init__(self, name: str, config: dict[str, Any], transport: Adapter):
        super().__init__(name, config)
        self.transport = transport
        configured_display = config.get("display")
        self.display = (
            str(configured_display) if configured_display is not None else None
        )
        self.raw_operations = dict(type(self).raw_operations)

    def _command(self, body: str) -> str:
        environment = linux_path_export()
        if self.display is not None:
            environment += f"export DISPLAY={shlex.quote(self.display)}; "
        return (
            f'{environment}export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"; '
            f"{body}"
        )

    async def _execute(
        self, body: str, stdin: bytes | None = None
    ) -> tuple[bytes, bytes]:
        code, stdout, stderr = await self.transport.shell(self._command(body), stdin)
        if code:
            raise ActionError(
                stderr.decode(errors="replace").strip()
                or f"remote command exited {code}"
            )
        return stdout, stderr

    async def _run(self, body: str, stdin: bytes | None = None) -> bytes:
        stdout, _ = await self._execute(body, stdin)
        return stdout

    async def available(self) -> tuple[bool, str | None]:
        available, reason = await self.transport.available()
        if not available:
            return False, reason
        try:
            output = await self._run(
                "command -v wayweaver-applications >/dev/null && "
                "command -v wayweaver-clipboard >/dev/null && "
                "command -v maim >/dev/null && "
                "command -v xclip >/dev/null && "
                "command -v xdotool >/dev/null && "
                "command -v wmctrl >/dev/null && "
                "command -v xprop >/dev/null && "
                "for tool in wmctrl xdotool xprop xrandr xwininfo wayweaver-x11-record; do "
                'if command -v "$tool" >/dev/null; then printf \'%s\n\' "$tool"; fi; done'
            )
            tools = set(output.decode().splitlines())
            self.capabilities = (
                type(self).capabilities
                if "wayweaver-x11-record" in tools
                else type(self).capabilities - {Capability.RECORDING}
            )
            self.raw_operations = {
                name: description
                for name, description in type(self).raw_operations.items()
                if name in tools
            }
            return True, None
        except ActionError as error:
            return False, str(error)

    async def surface_session(self) -> str:
        if configured := self.config.get("session_id"):
            return str(configured)
        output = await self._run(
            "window=$(xprop -root _NET_SUPPORTING_WM_CHECK 2>/dev/null "
            "| sed -n 's/.*# //p'); "
            'pid=$(xprop -id "$window" _NET_WM_PID 2>/dev/null '
            "| sed -n 's/.* = //p'); "
            'started=$(ps -o lstart= -p "$pid" 2>/dev/null); '
            'printf \'%s|%s|%s|%s\' "$DISPLAY" "$window" "$pid" "$started"'
        )
        value = output.decode(errors="replace").strip()
        if not value or value == f"{self.display}|||":
            raise ActionError("X11 session identity is unavailable")
        return value

    async def capture(self, params: dict[str, Any] | None = None) -> Frame:
        params = params or {}
        command = "maim -u"
        if region := params.get("region"):
            if not isinstance(region, list) or len(region) != 4:
                raise ActionError("region must be [left, top, right, bottom]")
            left, top, right, bottom = map(int, region)
            if right <= left or bottom <= top:
                raise ActionError("region right/bottom must exceed left/top")
            command += f" -g {right - left}x{bottom - top}+{left}+{top}"
        elif params.get("window") or params.get("id") or params.get("title"):
            locator = dict(params)
            if isinstance(locator.get("window"), str):
                locator["title"] = locator.pop("window")
            window = await self._window(locator)
            command += " -i " + shlex.quote(window["id"])
        png = await self._run(command)
        width, height = png_size(png)
        return Frame(png, width, height, self.kind)

    async def _pointer(self) -> tuple[int, int]:
        output = (await self._run("xdotool getmouselocation --shell")).decode()
        values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        return int(values["X"]), int(values["Y"])

    async def _move_script(self, x: int, y: int, duration_ms: int | None = None) -> str:
        points, duration = trajectory(await self._pointer(), (x, y), duration_ms)
        delay = duration / max(1, len(points)) / 1000
        commands = []
        for point_x, point_y in points:
            commands.append(f"xdotool mousemove {point_x} {point_y}")
            if delay:
                commands.append(f"sleep {delay:.4f}")
        return "; ".join(commands)

    async def _window(self, params: dict[str, Any]) -> dict[str, Any]:
        windows = await self.windows()
        identity = {
            key: params[key]
            for key in ("id", "title", "class", "pid", "desktop", "active")
            if key in params
        }
        if not identity:
            raise ActionError(
                "window action requires id, title, class, pid, desktop, or active"
            )
        matches = windows
        if identifier := identity.get("id"):
            matches = [
                window
                for window in matches
                if window["id"].casefold() == str(identifier).casefold()
            ]
        for field in ("title", "class"):
            if field not in identity:
                continue
            wanted = str(identity[field]).casefold()
            if params.get("exact"):
                matches = [
                    window
                    for window in matches
                    if str(window[field]).casefold() == wanted
                ]
            else:
                matches = [
                    window
                    for window in matches
                    if wanted in str(window[field]).casefold()
                ]
        for field in ("pid", "desktop"):
            if field in identity:
                matches = [
                    window
                    for window in matches
                    if window[field] == int(identity[field])
                ]
        if "active" in identity:
            matches = [
                window
                for window in matches
                if window["active"] is bool(identity["active"])
            ]
        nth = int(params.get("nth", 0))
        nth = nth if nth >= 0 else len(matches) + nth
        if not 0 <= nth < len(matches):
            raise ActionError(f"window not found: {identity!r}")
        result = dict(matches[nth])
        result["matches"] = len(matches)
        return result

    async def _wait_window(
        self, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        deadline = time.monotonic() + max(0, timeout)
        interval = max(0.05, float(params.get("interval", 0.2)))
        # A dialog that is still up means the action behind it never committed,
        # so waiting for a window to go away is as load-bearing as waiting for
        # one to appear.
        if params.get("absent"):
            while True:
                try:
                    window = await self._window(params)
                except ActionError:
                    return {"absent": True}
                if time.monotonic() >= deadline:
                    raise ActionError(
                        f"window still present: {window.get('title', '')!r}",
                        details={"id": window.get("id")},
                    )
                await asyncio.sleep(interval)
        while True:
            try:
                return await self._window(params)
            except ActionError:
                if time.monotonic() >= deadline:
                    raise
                await asyncio.sleep(interval)

    async def _window_action(
        self, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if action in {"wait_window", "assert_window"}:
            timeout = float(params.get("timeout", 3 if action == "wait_window" else 0))
            return await self._wait_window(params, timeout)
        try:
            window = await self._window(params)
        except ActionError:
            if params.get("if_exists"):
                return {"action": action, "window": None, "skipped": True}
            raise
        identifier = shlex.quote(window["id"])
        if action == "focus_window":
            command = f"wmctrl -ia {identifier}"
        elif action == "close_window":
            command = f"wmctrl -ic {identifier}"
        elif action == "minimize_window":
            command = f"wmctrl -ir {identifier} -b add,hidden"
        elif action == "fullscreen_window":
            operation = "add" if params.get("enabled", True) else "remove"
            command = f"wmctrl -ir {identifier} -b {operation},fullscreen"
        elif action in {"maximize_window", "restore_window"}:
            operation = "add" if action == "maximize_window" else "remove"
            command = (
                f"wmctrl -ir {identifier} -b {operation},maximized_vert,maximized_horz"
            )
        else:
            x = int(params.get("x", window["x"]))
            y = int(params.get("y", window["y"]))
            width = int(params.get("width", window["width"]))
            height = int(params.get("height", window["height"]))
            if action == "move_window":
                width, height = window["width"], window["height"]
            else:
                x, y = window["x"], window["y"]
            command = f"wmctrl -ir {identifier} -e 0,{x},{y},{width},{height}"
        await self._run(command)
        return {"action": action, "window": window}

    async def _workspaces(self) -> list[dict[str, Any]]:
        output = (await self._run("wmctrl -d")).decode(errors="replace")
        workspaces = []
        for line in output.splitlines():
            match = re.match(
                r"^(\d+)\s+([*-])\s+DG:\s+(\S+)\s+VP:\s+(\S+)\s+WA:\s+(\S+)\s+(\S+)\s+(.*)$",
                line,
            )
            if match:
                (
                    index,
                    marker,
                    geometry,
                    viewport,
                    workarea_position,
                    workarea_size,
                    name,
                ) = match.groups()
                workspaces.append(
                    {
                        "index": int(index),
                        "active": marker == "*",
                        "geometry": geometry,
                        "viewport": viewport,
                        "workarea": f"{workarea_position} {workarea_size}",
                        "name": name,
                    }
                )
        return workspaces

    async def _workspace_action(
        self, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        workspaces = await self._workspaces()
        if action == "list_workspaces":
            return {"workspaces": workspaces}
        if "index" in params:
            index = int(params["index"])
        else:
            wanted = str(params.get("name", "")).casefold()
            matches = [
                workspace
                for workspace in workspaces
                if wanted in workspace["name"].casefold()
            ]
            if not wanted or not matches:
                raise ActionError(f"workspace not found: {params.get('name')!r}")
            index = matches[int(params.get("nth", 0))]["index"]
        await self._run(f"wmctrl -s {index}")
        return {"index": index}

    async def act(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "list_applications":
            return {
                "applications": json.loads(
                    (await self._run("wayweaver-applications list")).decode()
                )
            }
        if action == "open_application":
            output = await self._run(
                "wayweaver-applications open", json.dumps(params).encode()
            )
            return json.loads(output)
        if action in self._WINDOW_ACTIONS:
            return await self._window_action(action, params)
        if action in self._WORKSPACE_ACTIONS:
            return await self._workspace_action(action, params)
        if action in {"move", "click", "double_click", "right_click"}:
            x, y = int(params["x"]), int(params["y"])
            script = await self._move_script(x, y, params.get("duration_ms"))
            if action != "move":
                button = 3 if action == "right_click" else int(params.get("button", 1))
                repeat = 2 if action == "double_click" else 1
                script += f"; xdotool click --clearmodifiers --repeat {repeat} --delay 120 {button}"
            await self._run(script)
            return {"x": x, "y": y, "action": action}
        if action == "drag":
            x1, y1, x2, y2 = (int(params[key]) for key in ("x1", "y1", "x2", "y2"))
            first = await self._move_script(x1, y1)
            points, duration = trajectory((x1, y1), (x2, y2), params.get("duration_ms"))
            delay = duration / max(1, len(points)) / 1000
            moves = "; ".join(
                f"xdotool mousemove {x} {y}; sleep {delay:.4f}" for x, y in points
            )
            button = int(params.get("button", 1))
            await self._run(
                f"{first}; xdotool mousedown {button}; {moves}; xdotool mouseup {button}"
            )
            return {"from": [x1, y1], "to": [x2, y2]}
        if action == "scroll":
            direction = params.get("direction", "down")
            buttons = {"up": 4, "down": 5, "left": 6, "right": 7}
            if direction not in buttons:
                raise ActionError(f"invalid scroll direction: {direction}")
            # X11 delivers wheel events to the window under the pointer, not the
            # focused one, so an unpositioned scroll silently drives whatever
            # happens to sit beneath the cursor -- a panel, the desktop, or an
            # unrelated window. Fall back to the active window's centre.
            point = None
            if "x" in params and "y" in params:
                point = (int(params["x"]), int(params["y"]))
            else:
                active = next(
                    (window for window in await self.windows() if window["active"]),
                    None,
                )
                if active is not None:
                    point = (
                        active["x"] + active["width"] // 2,
                        active["y"] + active["height"] // 2,
                    )
            prefix = ""
            if point is not None:
                prefix = await self._move_script(*point) + "; "
            amount = max(0, int(params.get("amount", 3)))
            await self._run(
                f"{prefix}xdotool click --repeat {amount} --delay 60 {buttons[direction]}"
            )
            return {"direction": direction, "amount": amount}
        if action == "type":
            text = str(params.get("text", ""))
            # `--` keeps text that begins with a dash from being read as an
            # xdotool option; shell quoting alone does not stop that.
            await self._run(
                f"xdotool type --clearmodifiers --delay {int(params.get('delay_ms', 15))} "
                f"-- {shlex.quote(text)}"
            )
            return {"characters": len(text)}
        if action in {"key", "hotkey"}:
            keys = key_expressions(params)
            key_spec = "+".join(keys) if len(keys) > 1 else keys[0]
            if "+" in key_spec:
                key_spec = "+".join(_chord_key(key) for key in key_spec.split("+"))
            _, stderr = await self._execute(
                f"xdotool key --clearmodifiers {shlex.quote(key_spec)}"
            )
            # xdotool reports an unresolvable keysym on stderr and still exits 0,
            # which would otherwise report a keystroke that never happened.
            message = stderr.decode(errors="replace").strip()
            if "No such key name" in message:
                raise ActionError(
                    f"X11 rejected key sequence {key_spec!r}: {message}",
                    details={"keys": keys, "key_spec": key_spec},
                )
            return {"keys": keys}
        raise ActionError(f"unsupported X11 action: {action}")

    @staticmethod
    def _frame_extents(output: str) -> dict[str, tuple[int, int]]:
        """Parse `id left,right,top,bottom` lines into id -> (left, top)."""
        extents: dict[str, tuple[int, int]] = {}
        for line in output.splitlines():
            identifier, _, values = line.partition(" ")
            numbers = [value for value in values.split(",") if value.strip()]
            if len(numbers) == 4:
                try:
                    extents[identifier.casefold()] = (
                        int(numbers[0]),
                        int(numbers[2]),
                    )
                except ValueError:
                    continue
        return extents

    async def windows(self) -> list[dict[str, Any]]:
        # wmctrl reports a window's position with its frame extents added on
        # top of the client area's own coordinates, so a decorated window reads
        # as starting below where it actually does -- 24px for this window
        # manager, which is exactly the height of an application's menu bar.
        # Anything scoped to the window rectangle then excludes the menu bar.
        output = (
            await self._run(
                "wmctrl -lpGx; printf '%s\\n' ---; "
                "wmctrl -l | cut -d' ' -f1 | while read -r id; do "
                "printf '%s %s\\n' \"$id\" \"$(xprop -id \"$id\" "
                "_NET_FRAME_EXTENTS 2>/dev/null | sed -n 's/.*= //p' "
                "| tr -d ' ')\"; done"
            )
        ).decode(errors="replace")
        listing, _, frames = output.partition("\n---\n")
        extents = self._frame_extents(frames)
        active_output = (
            (await self._run("xdotool getactivewindow 2>/dev/null || true"))
            .decode(errors="replace")
            .strip()
        )
        active = int(active_output) if active_output.isdigit() else None
        windows = []
        for line in listing.splitlines():
            parts = line.split(None, 9)
            if len(parts) == 10:
                (
                    window_id,
                    desktop,
                    pid,
                    x,
                    y,
                    width,
                    height,
                    window_class,
                    host,
                    title,
                ) = parts
                frame_left, frame_top = extents.get(window_id.casefold(), (0, 0))
                windows.append(
                    {
                        "id": window_id,
                        "desktop": int(desktop),
                        "pid": int(pid),
                        "x": int(x) - frame_left,
                        "y": int(y) - frame_top,
                        "width": int(width),
                        "height": int(height),
                        "class": window_class,
                        "host": host,
                        "title": title,
                        "active": active == int(window_id, 16),
                    }
                )
        return windows

    @staticmethod
    def _recorded_events(output: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        button_down: dict[int, dict[str, Any]] = {}
        key_down: dict[int, dict[str, Any]] = {}
        modifiers: set[str] = set()
        modifier_names = {
            "Shift_L": "SHIFT",
            "Shift_R": "SHIFT",
            "Control_L": "CTRL",
            "Control_R": "CTRL",
            "Alt_L": "ALT",
            "Alt_R": "ALT",
            "Meta_L": "META",
            "Meta_R": "META",
            "Super_L": "SUPER",
            "Super_R": "SUPER",
        }
        for line in output.splitlines():
            parts = line.split()
            if len(parts) == 5 and parts[0] in {"BUTTON_DOWN", "BUTTON_UP"}:
                try:
                    button, x, y, timestamp = map(int, parts[1:])
                except ValueError:
                    continue
                if parts[0] == "BUTTON_DOWN":
                    button_down[button] = {
                        "x": x,
                        "y": y,
                        "time": timestamp,
                    }
                    continue
                started = button_down.pop(button, {"x": x, "y": y, "time": timestamp})
                if button in {4, 5}:
                    events.append(
                        {
                            "kind": "scroll",
                            "direction": "up" if button == 4 else "down",
                            "x": x,
                            "y": y,
                            "time": timestamp,
                        }
                    )
                elif button == 1 and (
                    abs(x - started["x"]) > 5 or abs(y - started["y"]) > 5
                ):
                    events.append(
                        {
                            "kind": "drag",
                            "button": button,
                            "from": {"x": started["x"], "y": started["y"]},
                            "to": {"x": x, "y": y},
                            "time": timestamp,
                            "duration_ms": max(0, timestamp - started["time"]),
                        }
                    )
                else:
                    events.append(
                        {
                            "kind": "click",
                            "button": button,
                            "x": x,
                            "y": y,
                            "time": timestamp,
                            "duration_ms": max(0, timestamp - started["time"]),
                        }
                    )
            elif len(parts) == 4 and parts[0] in {"KEY_DOWN", "KEY_UP"}:
                try:
                    keycode = int(parts[1])
                    timestamp = int(parts[3])
                except ValueError:
                    continue
                name = parts[2]
                modifier = modifier_names.get(name)
                if parts[0] == "KEY_DOWN":
                    key_down[keycode] = {
                        "key": name,
                        "modifiers": sorted(modifiers),
                        "time": timestamp,
                    }
                    if modifier:
                        modifiers.add(modifier)
                    continue
                started = key_down.pop(keycode, None)
                if modifier:
                    modifiers.discard(modifier)
                elif started:
                    events.append(
                        {
                            "kind": "key",
                            "key": started["key"],
                            "modifiers": started["modifiers"],
                            "time": timestamp,
                            "duration_ms": max(0, timestamp - started["time"]),
                        }
                    )
        return sorted(events, key=lambda event: int(event["time"]))

    @staticmethod
    def _element_at(
        elements: list[dict[str, Any]], x: int, y: int
    ) -> dict[str, Any] | None:
        matches = []
        for element in elements:
            bounds = element.get("bounds")
            if not isinstance(bounds, dict):
                continue
            left = int(bounds.get("left", 0))
            top = int(bounds.get("top", 0))
            width = int(bounds.get("width", 0))
            height = int(bounds.get("height", 0))
            if (
                width > 0
                and height > 0
                and left <= x < left + width
                and top <= y < top + height
            ):
                identifiable = bool(
                    element.get("name")
                    or element.get("resource_id")
                    or element.get("actions")
                )
                depth = len(element.get("path", []))
                matches.append((width * height, not identifiable, -depth, element))
        return min(matches, key=lambda item: item[:3])[3] if matches else None

    @staticmethod
    def _recording_step(
        event: dict[str, Any], element: dict[str, Any] | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        recorded = dict(event)
        if event["kind"] == "key":
            modifiers = list(event.get("modifiers", []))
            key = str(event["key"])
            if modifiers:
                step = {
                    "operation": "keyboard.chord",
                    "params": {"keys": [*modifiers, key]},
                }
            elif len(key) == 1:
                step = {"operation": "keyboard.type", "params": {"text": key}}
            else:
                step = {"operation": "keyboard.press", "params": {"key": key}}
            recorded["semantic"] = True
            return recorded, step
        if event["kind"] == "scroll":
            recorded["semantic"] = False
            return (
                recorded,
                {
                    "operation": "pointer.scroll",
                    "params": {
                        "direction": event["direction"],
                        "point": {"x": event["x"], "y": event["y"]},
                        "space": "screen",
                    },
                },
            )
        if event["kind"] == "drag":
            recorded["semantic"] = False
            return (
                recorded,
                {
                    "operation": "pointer.drag",
                    "params": {
                        "from": event["from"],
                        "to": event["to"],
                        "space": "screen",
                        "button": "left",
                        "duration_ms": event["duration_ms"],
                    },
                },
            )
        role = str((element or {}).get("role", "")).casefold()
        selector = {
            key: value
            for key, value in {
                "resource_id": (element or {}).get("resource_id"),
                "name": (element or {}).get("name"),
                "role": (element or {}).get("role"),
            }.items()
            if value
        }
        semantic = (
            event["button"] == 1
            and bool(selector.get("name") or selector.get("resource_id"))
            and role not in {"application", "desktop", "frame"}
        )
        recorded["semantic"] = semantic
        if semantic:
            if "name" in selector:
                selector["exact"] = True
            recorded["selector"] = selector
            return (
                recorded,
                {
                    "operation": "element.activate",
                    "params": {"selector": selector},
                },
            )
        button = {1: "left", 2: "middle", 3: "right"}[event["button"]]
        return (
            recorded,
            {
                "operation": "pointer.click",
                "params": {
                    "point": {"x": event["x"], "y": event["y"]},
                    "space": "screen",
                    "button": button,
                    "count": 1,
                },
            },
        )

    @staticmethod
    def _recording_id(value: Any) -> str:
        recording_id = str(value)
        if not re.fullmatch(r"[0-9a-f]{32}", recording_id):
            raise ActionError("invalid recording_id", retryable=False)
        return recording_id

    async def _recording_start(self) -> dict[str, Any]:
        recording_id = uuid4().hex
        base = f"/tmp/wayweaver-recordings/{recording_id}"
        output = await self._run(
            "umask 077; install -d -m 0700 /tmp/wayweaver-recordings; "
            f": > {base}.events; : > {base}.error; "
            f"date +%s%3N > {base}.started; "
            f"nohup wayweaver-x11-record > {base}.events 2> {base}.error "
            f"</dev/null & printf '%s' \"$!\" > {base}.pid; "
            "sleep 0.05; "
            f'kill -0 "$(cat {base}.pid)"'
        )
        if output:
            raise ActionError("recorder returned unexpected startup output")
        return {"recording_id": recording_id, "status": "recording"}

    async def _recording_status(self, recording_id: str) -> dict[str, Any]:
        base = f"/tmp/wayweaver-recordings/{recording_id}"
        output = (
            await self._run(
                f"test -f {base}.pid || exit 4; pid=$(cat {base}.pid); "
                f'if kill -0 "$pid" 2>/dev/null '
                f"&& tr '\\0' ' ' < /proc/$pid/cmdline | grep -q wayweaver-x11-record; "
                "then status=recording; else status=stopped; fi; "
                f"printf '%s\\n' \"$status\"; wc -l < {base}.events"
            )
        ).decode()
        status, event_lines = output.splitlines()
        return {
            "recording_id": recording_id,
            "status": status,
            "event_lines": int(event_lines),
        }

    async def _recording_stop(
        self, recording_id: str, infer_elements: bool
    ) -> dict[str, Any]:
        base = f"/tmp/wayweaver-recordings/{recording_id}"
        output = (
            await self._run(
                f"test -f {base}.pid || exit 4; pid=$(cat {base}.pid); "
                'if kill -0 "$pid" 2>/dev/null; then kill -TERM "$pid" 2>/dev/null || :; fi; '
                "for attempt in $(seq 1 50); do "
                'kill -0 "$pid" 2>/dev/null || break; sleep 0.02; done; '
                'kill -KILL "$pid" 2>/dev/null || :; '
                f"printf 'STARTED %s\\n' \"$(cat {base}.started)\"; "
                "printf 'STOPPED %s\\n' \"$(date +%s%3N)\"; "
                f"cat {base}.events; "
                f"rm -f {base}.pid {base}.started {base}.events {base}.error"
            )
        ).decode(errors="replace")
        lines = output.splitlines()
        started = int(lines[0].split()[1])
        stopped = int(lines[1].split()[1])
        events = self._recorded_events("\n".join(lines[2:]))
        elements = []
        if events and infer_elements:
            try:
                tree = json.loads(
                    (
                        await self._run(
                            "export NO_AT_BRIDGE=0; wayweaver-atspi list",
                            json.dumps({"limit": 2000, "max_depth": 16}).encode(),
                        )
                    ).decode()
                )
                elements = tree.get("elements", [])
            except (ActionError, json.JSONDecodeError):
                elements = []
        recorded_events = []
        steps = []
        semantic_steps = 0
        for event in events:
            element = (
                self._element_at(elements, event["x"], event["y"])
                if event["kind"] == "click"
                else None
            )
            recorded, step = self._recording_step(event, element)
            recorded_events.append(recorded)
            steps.append(step)
            semantic_steps += int(recorded["semantic"])
        return {
            "recording_id": recording_id,
            "status": "stopped",
            "duration_ms": max(0, stopped - started),
            "events": recorded_events,
            "steps": steps,
            "semantic_steps": semantic_steps,
            "coordinate_steps": len(steps) - semantic_steps,
        }

    async def _recording_cancel(self, recording_id: str) -> dict[str, Any]:
        base = f"/tmp/wayweaver-recordings/{recording_id}"
        await self._run(
            f"test -f {base}.pid || exit 4; pid=$(cat {base}.pid); "
            'kill -TERM "$pid" 2>/dev/null || :; '
            f"rm -f {base}.pid {base}.started {base}.events {base}.error"
        )
        return {"recording_id": recording_id, "status": "cancelled"}

    async def _record(self, params: dict[str, Any]) -> dict[str, Any]:
        duration_ms = int(params["duration_ms"])
        started = await self._recording_start()
        await asyncio.sleep(duration_ms / 1000)
        result = await self._recording_stop(
            started["recording_id"], bool(params.get("infer_elements", True))
        )
        result["duration_ms"] = duration_ms
        return result

    async def perform(self, operation: str, params: dict[str, Any]) -> Any:
        if operation == "recording.capture":
            return await self._record(params)
        if operation == "recording.start":
            return await self._recording_start()
        if operation == "recording.status":
            return await self._recording_status(
                self._recording_id(params["recording_id"])
            )
        if operation == "recording.stop":
            return await self._recording_stop(
                self._recording_id(params["recording_id"]),
                bool(params.get("infer_elements", True)),
            )
        if operation == "recording.cancel":
            return await self._recording_cancel(
                self._recording_id(params["recording_id"])
            )
        if operation == "clipboard.read":
            text = (await self._run("wayweaver-clipboard read")).decode(
                errors="replace"
            )
            return {"text": text}
        if operation == "clipboard.write":
            text = str(params.get("text", ""))
            await self._run("wayweaver-clipboard write", text.encode())
            return {"characters": len(text)}
        return await super().perform(operation, params)

    async def raw(self, operation: str, params: dict[str, Any]) -> Any:
        if operation not in self.raw_operations:
            raise ActionError(f"unknown X11 raw operation: {operation}")
        arguments = params.get("args", [])
        if not isinstance(arguments, list) or not all(
            isinstance(value, str) for value in arguments
        ):
            raise ActionError("raw args must be a string array")
        command = " ".join([operation, *(shlex.quote(value) for value in arguments)])
        output = await self._run(
            command,
            str(params.get("stdin", "")).encode() if "stdin" in params else None,
        )
        return {"stdout": output.decode(errors="replace")}


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    transport = require_shell_transport("x11", config, adapters)
    return X11Adapter(name, config, transport)
