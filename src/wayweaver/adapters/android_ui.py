import asyncio
import re
import shlex
import time
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from typing import Any

from ..errors import ActionError

RunChecked = Callable[..., Awaitable[bytes]]
_BOUNDS = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")


def input_text_argument(text: str) -> str:
    """Encode text for `input text`, quoted for the shell adb runs it in.

    adb joins everything after `shell` into one string and hands it to the
    device shell, so an unquoted argument leaves every metacharacter in the
    text active: a quote breaks the command and a semicolon runs the remainder
    as a separate one. `input` itself treats a space as an argument boundary
    and reads %s as a space.
    """
    return shlex.quote(text.replace("%", "%25").replace(" ", "%s"))


def _boolean(value: str | None) -> bool:
    return value == "true"


def _describe(node: ET.Element, path: tuple[int, ...]) -> dict[str, Any]:
    attributes = node.attrib
    match = _BOUNDS.fullmatch(attributes.get("bounds", ""))
    left, top, right, bottom = (
        tuple(map(int, match.groups())) if match else (0, 0, 0, 0)
    )
    text = attributes.get("text", "")
    description = attributes.get("content-desc", "")
    resource_id = attributes.get("resource-id", "")
    class_name = attributes.get("class", "")
    name = description or text or resource_id.rsplit("/", 1)[-1]
    states = [
        state
        for state, present in {
            "checkable": _boolean(attributes.get("checkable")),
            "checked": _boolean(attributes.get("checked")),
            "clickable": _boolean(attributes.get("clickable")),
            "enabled": _boolean(attributes.get("enabled")),
            "focusable": _boolean(attributes.get("focusable")),
            "focused": _boolean(attributes.get("focused")),
            "long-clickable": _boolean(attributes.get("long-clickable")),
            "password": _boolean(attributes.get("password")),
            "scrollable": _boolean(attributes.get("scrollable")),
            "selected": _boolean(attributes.get("selected")),
        }.items()
        if present
    ]
    actions = []
    if "clickable" in states:
        actions.append("click")
    if "long-clickable" in states:
        actions.append("long-click")
    if "focusable" in states:
        actions.append("focus")
    return {
        "path": list(path),
        "name": name,
        "text": text,
        "role": class_name.rsplit(".", 1)[-1].casefold(),
        "description": description,
        "resource_id": resource_id,
        "class": class_name,
        "package": attributes.get("package", ""),
        "states": states,
        "actions": actions,
        "bounds": {
            "left": left,
            "top": top,
            "width": max(0, right - left),
            "height": max(0, bottom - top),
        },
    }


def parse_hierarchy(xml: bytes) -> list[dict[str, Any]]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as error:
        raise ActionError(f"invalid UIAutomator hierarchy: {error}") from error
    result = []

    def visit(node: ET.Element, path: tuple[int, ...]) -> None:
        if node.tag == "node":
            result.append(_describe(node, path))
        for index, child in enumerate(node):
            visit(child, (*path, index))

    visit(root, ())
    return result


def _matches(element: dict[str, Any], options: dict[str, Any]) -> bool:
    exact = bool(options.get("exact"))
    fields = {
        "name": options.get("name"),
        "text": options.get("text"),
        "role": options.get("role"),
        "resource_id": options.get("resource_id", options.get("id")),
        "class": options.get("class"),
        "package": options.get("package"),
    }
    used = False
    for field, wanted in fields.items():
        if wanted is None or wanted == "":
            continue
        used = True
        actual = str(element.get(field, ""))
        wanted_text = str(wanted)
        if exact:
            if actual.casefold() != wanted_text.casefold():
                return False
        elif wanted_text.casefold() not in actual.casefold():
            return False
    waiting_for_disabled = (
        str(options.get("state", "")).casefold() == "enabled"
        and options.get("value", True) is False
    )
    if (
        not options.get("include_disabled", False)
        and not waiting_for_disabled
        and "enabled" not in element["states"]
    ):
        return False
    return used


class AndroidUI:
    def __init__(self, run: RunChecked, path: str = "/sdcard/window.xml"):
        self.run = run
        self.path = path

    async def elements(self) -> list[dict[str, Any]]:
        await self.run("shell", "uiautomator", "dump", self.path)
        xml = await self.run("exec-out", "cat", self.path)
        return parse_hierarchy(xml)

    async def find(self, options: dict[str, Any]) -> tuple[dict[str, Any], int]:
        matches = [
            element for element in await self.elements() if _matches(element, options)
        ]
        nth = int(options.get("nth", 0))
        nth = nth if nth >= 0 else len(matches) + nth
        if not 0 <= nth < len(matches):
            raise ActionError(f"Android element not found: {options!r}")
        result = dict(matches[nth])
        result["matches"] = len(matches)
        return result, len(matches)

    def _has_state(self, element: dict[str, Any], options: dict[str, Any]) -> bool:
        state = str(options.get("state", ""))
        if not state:
            return True
        expected = bool(options.get("value", True))
        return (state in element["states"]) is expected

    async def wait(self, options: dict[str, Any]) -> dict[str, Any]:
        deadline = time.monotonic() + max(0.0, float(options.get("timeout", 5)))
        interval = max(0.05, float(options.get("interval", 0.25)))
        last_error = None
        while True:
            try:
                element, _ = await self.find(options)
                if self._has_state(element, options):
                    return element
            except ActionError as error:
                last_error = error
            if time.monotonic() >= deadline:
                if last_error:
                    raise last_error
                raise ActionError(f"Android element state not reached: {options!r}")
            await asyncio.sleep(interval)

    async def perform(self, operation: str, options: dict[str, Any]) -> Any:
        if operation == "element.list":
            limit = max(1, int(options.get("limit", 500)))
            elements = await self.elements()
            return {"elements": elements[:limit], "truncated": len(elements) > limit}
        if operation == "element.wait":
            return await self.wait(options)
        if operation == "element.focused":
            # With no selector to fall through to, this previously reached
            # find() with empty options and reported "element not found" --
            # the wrong error for a question the hierarchy can answer.
            for element in await self.elements():
                if "focused" in element["states"]:
                    return element
            raise ActionError("no Android element currently has focus")
        element, _ = await self.find(options)
        if operation in {"element.find", "element.assert", "element.read"}:
            return element
        bounds = element["bounds"]
        x = bounds["left"] + bounds["width"] // 2
        y = bounds["top"] + bounds["height"] // 2
        if bounds["width"] <= 0 or bounds["height"] <= 0:
            raise ActionError("Android element has no actionable bounds")
        if operation in {"element.activate", "element.focus"}:
            if options.get("action") == "long-click":
                await self.run(
                    "shell", "input", "swipe", str(x), str(y), str(x), str(y), "800"
                )
                element["invoked"] = "long-click"
            else:
                await self.run("shell", "input", "tap", str(x), str(y))
                element["invoked"] = (
                    "click" if operation == "element.activate" else "focus"
                )
            element["point"] = {"x": x, "y": y}
            return element
        if operation == "element.set_value":
            if "text" not in options:
                raise ActionError("Android element.set_value requires text")
            await self.run("shell", "input", "tap", str(x), str(y))
            if options.get("clear", True):
                current = str(element.get("text", ""))
                await self.run("shell", "input", "keyevent", "KEYCODE_MOVE_END")
                if current:
                    await self.run(
                        "shell", "input", "keyevent", *(["KEYCODE_DEL"] * len(current))
                    )
            text = str(options["text"])
            if text:
                await self.run("shell", "input", "text", input_text_argument(text))
            element["value"] = text
            return element
        raise ActionError(f"unsupported Android element operation: {operation}")
