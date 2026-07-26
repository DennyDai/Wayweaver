import asyncio
import json
import shlex
from typing import Any

from ..errors import ActionError
from ..image import png_size
from ..motion import trajectory
from ..runtime import linux_path_export
from ..types import Capability, Frame
from .base import Adapter, key_expressions, require_shell_transport

from .wayland_backends import GnomeBackend, KDotoolBackend, SwayBackend


# The canonical contract numbers buttons 1=left, 2=middle, 3=right, matching
# xdotool. ydotool numbers them 0=left, 1=right, 2=middle, so the contract value
# cannot be added to 0xC0 directly: doing so turned a requested left click into
# a right click and a requested right click into an undefined button.
_YDOTOOL_BUTTONS = {1: 0x00, 2: 0x02, 3: 0x01}


class WaylandAdapter(Adapter):
    kind = "wayland"
    raw_operations = {
        "grim": "Call grim with an argument list",
        "swaymsg": "Call swaymsg with an argument list",
        "wtype": "Call wtype with an argument list",
        "ydotool": "Call ydotool with an argument list",
        "kdotool": "Call kdotool with an argument list",
        "gdbus": "Call gdbus with an argument list",
        "spectacle": "Call Spectacle with an argument list",
        "gnome-screenshot": "Call GNOME Screenshot with an argument list",
    }

    def __init__(self, name: str, config: dict[str, Any], transport: Adapter):
        super().__init__(name, config)
        self.transport = transport
        configured_display = config.get("display")
        self.display = (
            str(configured_display) if configured_display is not None else None
        )
        self.capabilities = frozenset()
        self.raw_operations = dict(type(self).raw_operations)
        self.pointer = (0, 0)
        self.compositor = None
        self.capture_tool: str | None = None

    def _command(self, body: str) -> str:
        environment = linux_path_export()
        if self.display is not None:
            environment += f"export WAYLAND_DISPLAY={shlex.quote(self.display)}; "
        return (
            f'{environment}export XDG_RUNTIME_DIR="${{XDG_RUNTIME_DIR:-/run/user/$(id -u)}}"; '
            'export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=$XDG_RUNTIME_DIR/bus}"; '
            # swaymsg locates the compositor through SWAYSOCK, which a shell
            # opened from outside the session never inherits, so sway IPC --
            # and with it every window and workspace operation -- failed with
            # "Unable to retrieve socket path" even on a healthy session.
            'if [ -z "${SWAYSOCK:-}" ]; then '
            'sway_socket=$(ls -1t "$XDG_RUNTIME_DIR"/sway-ipc.*.sock 2>/dev/null | head -1); '
            '[ -n "$sway_socket" ] && export SWAYSOCK="$sway_socket"; fi; '
            f"{body}"
        )

    async def _run(self, body: str, stdin: bytes | None = None) -> bytes:
        code, stdout, stderr = await self.transport.shell(self._command(body), stdin)
        if code:
            raise ActionError(
                stderr.decode(errors="replace").strip()
                or f"remote command exited {code}"
            )
        return stdout

    async def available(self) -> tuple[bool, str | None]:
        available, reason = await self.transport.available()
        if not available:
            return False, reason
        try:
            output = (
                (
                    await self._run(
                        'test -S "$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY" || exit 2; '
                        "for tool in wayweaver-applications gdbus gnome-screenshot grim "
                        "kdotool spectacle swaymsg wl-copy wl-paste wtype ydotool; do "
                        'if command -v "$tool" >/dev/null; then printf \'%s\\n\' "$tool"; fi; done'
                    )
                )
                .decode()
                .splitlines()
            )
        except ActionError as error:
            return False, str(error)
        tools = set(output)
        capabilities = set()
        if "wayweaver-applications" in tools:
            capabilities.add(Capability.APPLICATIONS)
        for tool in ("grim", "spectacle", "gnome-screenshot"):
            if tool in tools:
                self.capture_tool = tool
                capabilities.add(Capability.CAPTURE)
                break
        requested = str(self.config.get("compositor", "auto")).casefold()
        candidates = []
        if requested in {"auto", "sway"} and "swaymsg" in tools:
            candidates.append(SwayBackend(self._run))
        if requested in {"auto", "kde"} and "kdotool" in tools:
            candidates.append(KDotoolBackend(self._run))
        if requested in {"auto", "gnome"} and "gdbus" in tools:
            candidates.append(GnomeBackend(self._run))
        self.compositor = None
        for candidate in candidates:
            try:
                await candidate.probe()
                self.compositor = candidate
                capabilities.update({Capability.WINDOWS, Capability.WORKSPACES})
                break
            except ActionError:
                continue
        if {"wl-copy", "wl-paste"} <= tools:
            capabilities.add(Capability.CLIPBOARD)
        if "wtype" in tools:
            capabilities.add(Capability.KEYBOARD)
        if "ydotool" in tools:
            capabilities.add(Capability.POINTER)
        self.capabilities = frozenset(capabilities)
        self.raw_operations = {
            name: description
            for name, description in type(self).raw_operations.items()
            if name in tools
        }
        return (
            (True, None)
            if capabilities
            else (False, "Wayland session has no supported control tools")
        )

    async def capture(self, params: dict[str, Any] | None = None) -> Frame:
        params = params or {}
        if self.capture_tool == "grim":
            command = "grim"
            if region := params.get("region"):
                if not isinstance(region, list) or len(region) != 4:
                    raise ActionError("region must be [left, top, right, bottom]")
                left, top, right, bottom = map(int, region)
                command += (
                    f" -g {shlex.quote(f'{left},{top} {right - left}x{bottom - top}')}"
                )
            png = await self._run(command + " -")
        else:
            if params.get("region"):
                raise ActionError(
                    f"{self.capture_tool or 'Wayland'} capture does not support regions"
                )
            if self.capture_tool == "spectacle":
                screenshot = 'spectacle -b -n -o "$tmp" >/dev/null'
            elif self.capture_tool == "gnome-screenshot":
                screenshot = 'gnome-screenshot -f "$tmp"'
            else:
                raise ActionError("Wayland capture is unavailable")
            png = await self._run(
                "tmp=$(mktemp --suffix=.png); trap 'rm -f \"$tmp\"' EXIT; "
                f'{screenshot} && cat "$tmp"'
            )
        width, height = png_size(png)
        return Frame(png, width, height, self.kind)

    async def _move(
        self, target: tuple[int, int], duration_ms: int | None = None
    ) -> None:
        points, duration = trajectory(self.pointer, target, duration_ms)
        await self._run(
            f"ydotool mousemove --absolute -x {self.pointer[0]} -y {self.pointer[1]}"
        )
        delay = duration / max(1, len(points)) / 1000
        for x, y in points:
            await self._run(f"ydotool mousemove --absolute -x {x} -y {y}")
            if delay:
                await asyncio.sleep(delay)
        self.pointer = target

    async def act(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action in {"move", "click", "double_click", "right_click"}:
            target = int(params["x"]), int(params["y"])
            await self._move(target, params.get("duration_ms"))
            if action != "move":
                button = 3 if action == "right_click" else int(params.get("button", 1))
                code = 0xC0 + _YDOTOOL_BUTTONS.get(button, 0x00)
                for _ in range(2 if action == "double_click" else 1):
                    await self._run(f"ydotool click {code:#x}")
                    await asyncio.sleep(0.12)
            return {"x": target[0], "y": target[1], "action": action}
        if action == "drag":
            start = int(params["x1"]), int(params["y1"])
            end = int(params["x2"]), int(params["y2"])
            await self._move(start)
            await self._run("ydotool click 0x40")
            await self._move(end, params.get("duration_ms"))
            await self._run("ydotool click 0x80")
            return {"from": list(start), "to": list(end)}
        if action == "type":
            text = str(params.get("text", ""))
            await self._run("wtype -- " + shlex.quote(text))
            return {"characters": len(text)}
        if action in {"key", "hotkey"}:
            values = key_expressions(params)
            for expression in values:
                parts = str(expression).split("+")
                modifiers = parts[:-1]
                command = ["wtype"]
                for modifier in modifiers:
                    command += ["-M", modifier]
                command += ["-k", parts[-1]]
                for modifier in reversed(modifiers):
                    command += ["-m", modifier]
                await self._run(" ".join(shlex.quote(value) for value in command))
            return {"keys": values}
        if action == "list_applications":
            return {
                "applications": json.loads(
                    (await self._run("wayweaver-applications list")).decode()
                )
            }
        if action == "open_application":
            return json.loads(
                await self._run(
                    "wayweaver-applications open", json.dumps(params).encode()
                )
            )
        if action in {
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
        }:
            return await self._window_action(action, params)
        if action == "list_workspaces":
            if self.compositor is None:
                raise ActionError("Wayland compositor metadata is unavailable")
            return {"workspaces": await self.compositor.workspaces()}
        if action == "switch_workspace":
            if self.compositor is None:
                raise ActionError("Wayland compositor metadata is unavailable")
            workspace = params.get("name", params.get("index"))
            await self.compositor.switch_workspace(workspace)
            return {"workspace": workspace}
        raise ActionError(f"unsupported Wayland action: {action}")

    async def windows(self) -> list[dict[str, Any]]:
        if self.compositor is None:
            raise ActionError("Wayland compositor metadata is unavailable")
        return await self.compositor.windows()

    def _matching_windows(
        self,
        windows: list[dict[str, Any]],
        params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        identity = {
            key: params[key]
            for key in ("id", "title", "class", "pid", "active")
            if key in params
        }
        if not identity:
            raise ActionError("window action requires id, title, class, pid, or active")
        matches = windows
        if identifier := identity.get("id"):
            matches = [window for window in matches if window["id"] == str(identifier)]
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
        if "pid" in identity:
            matches = [
                window for window in matches if window["pid"] == int(identity["pid"])
            ]
        if "active" in identity:
            matches = [
                window
                for window in matches
                if window["active"] is bool(identity["active"])
            ]
        return matches

    async def _window_action(
        self, action: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + float(
            params.get("timeout", 3 if action == "wait_window" else 0)
        )
        while True:
            windows = await self.windows()
            matches = self._matching_windows(windows, params)
            if matches:
                window = matches[int(params.get("nth", 0))]
                break
            if asyncio.get_running_loop().time() >= deadline:
                if params.get("if_exists"):
                    return {"action": action, "window": None, "skipped": True}
                raise ActionError(f"Wayland window not found: {params!r}")
            await asyncio.sleep(0.2)
        if self.compositor is None:
            raise ActionError("Wayland compositor metadata is unavailable")
        await self.compositor.window_action(action, window, params)
        return {"window": window, "matches": len(matches)}

    async def perform(self, operation: str, params: dict[str, Any]) -> Any:
        if operation == "clipboard.read":
            return {
                "text": (await self._run("wl-paste --no-newline")).decode(
                    errors="replace"
                )
            }
        if operation == "clipboard.write":
            text = str(params.get("text", ""))
            # Wayland has no clipboard manager: wl-copy must stay alive to
            # serve the selection, and it inherits stdout, so the calling shell
            # never sees end-of-file and the operation hangs. Detach it and
            # close its streams so only the copy outlives the command.
            await self._run(
                'tmp=$(mktemp); cat > "$tmp"; '
                "setsid sh -c 'wl-copy < \"$1\"; rm -f \"$1\"' sh \"$tmp\" "
                "</dev/null >/dev/null 2>&1 & exit 0",
                text.encode(),
            )
            return {"characters": len(text)}
        return await super().perform(operation, params)

    async def raw(self, operation: str, params: dict[str, Any]) -> Any:
        if operation not in self.raw_operations:
            raise ActionError(f"unknown Wayland raw operation: {operation}")
        arguments = params.get("args", [])
        if not isinstance(arguments, list) or not all(
            isinstance(value, str) for value in arguments
        ):
            raise ActionError("raw args must be a string array")
        output = await self._run(
            " ".join([operation, *(shlex.quote(value) for value in arguments)])
        )
        return {"stdout": output.decode(errors="replace")}


def create(name: str, config: dict[str, Any], adapters: dict[str, Adapter]) -> Adapter:
    transport = require_shell_transport("wayland", config, adapters)
    return WaylandAdapter(name, config, transport)
