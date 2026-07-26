import json
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

from wayweaver.adapters.adb import ADBAdapter
from wayweaver.adapters.android_ui import AndroidUI, parse_hierarchy
from wayweaver.adapters.atspi import ATSPIAdapter
from wayweaver.adapters.base import Adapter
from wayweaver.adapters.uia import UIAAdapter
from wayweaver.adapters.wayland_backends import GnomeBackend, KDotoolBackend
from wayweaver.errors import ActionError
from wayweaver.types import Capability


ANDROID_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<hierarchy rotation="0">
  <node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="demo" content-desc="" checkable="false" checked="false" clickable="false" enabled="true" focusable="false" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[0,0][1080,1920]">
    <node index="0" text="Delete" resource-id="demo:id/delete" class="android.widget.Button" package="demo" content-desc="Delete item" checkable="false" checked="false" clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="true" password="false" selected="false" bounds="[800,1700][1040,1800]" />
  </node>
</hierarchy>"""


class FakeTransport(Adapter):
    kind = "fake"
    capabilities = frozenset({Capability.SHELL})

    def __init__(self, responses=None):
        super().__init__("transport", {})
        self.responses = responses or []
        self.calls = []

    async def available(self):
        return True, None

    async def shell(self, command, stdin=None):
        self.calls.append((command, stdin))
        response = self.responses.pop(0) if self.responses else {"available": True}
        return 0, json.dumps(response).encode(), b""


class UIAAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_routes_state_wait_to_powershell_helper(self):
        transport = FakeTransport([{"name": "Save", "states": ["enabled"]}])
        adapter = UIAAdapter(
            "uia",
            {"transport": "local", "command": "C:\\tools\\wayweaver-uia.ps1"},
            transport,
        )

        result = await adapter.perform(
            "element.wait",
            {"name": "Save", "state": "enabled", "timeout": 3},
        )

        self.assertEqual(result["name"], "Save")
        command, stdin = transport.calls[0]
        self.assertEqual(command, "C:\\tools\\wayweaver-uia.ps1 -Action wait-state")
        self.assertEqual(
            json.loads(stdin),
            {"name": "Save", "state": "enabled", "timeout": 3},
        )


class ATSPIAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_restarts_helper_to_refresh_remote_accessibility_cache(self):
        adapter = ATSPIAdapter("atspi", {"wait_min_interval": 0}, FakeTransport())
        adapter._call = AsyncMock(
            side_effect=[
                ActionError("stale accessibility cache"),
                {"name": "Save", "states": ["enabled"]},
            ]
        )

        result = await adapter.perform(
            "element.wait",
            {"name": "Save", "timeout": 2, "interval": 0.05},
        )

        self.assertEqual(result["name"], "Save")
        self.assertEqual(adapter._call.await_count, 2)
        adapter._call.assert_awaited_with(
            "assert",
            {"name": "Save", "timeout": 2, "interval": 0.05},
        )


class AndroidUITests(unittest.IsolatedAsyncioTestCase):
    def test_parses_resource_identity_state_actions_and_bounds(self):
        elements = parse_hierarchy(ANDROID_XML)

        button = elements[1]
        self.assertEqual(button["name"], "Delete item")
        self.assertEqual(button["resource_id"], "demo:id/delete")
        self.assertEqual(button["role"], "button")
        self.assertEqual(
            button["bounds"], {"left": 800, "top": 1700, "width": 240, "height": 100}
        )
        self.assertIn("long-click", button["actions"])

    async def test_activates_freshly_resolved_element_by_center(self):
        calls = []

        async def run(*args):
            calls.append(args)
            if args[:2] == ("exec-out", "cat"):
                return ANDROID_XML
            return b""

        ui = AndroidUI(run)
        found = await ui.perform(
            "element.find",
            {"text": "Delete", "exact": True},
        )
        result = await ui.perform(
            "element.activate",
            {"resource_id": "demo:id/delete", "exact": True},
        )

        self.assertEqual(found["resource_id"], "demo:id/delete")
        self.assertEqual(result["point"], {"x": 920, "y": 1750})
        self.assertEqual(calls[-1], ("shell", "input", "tap", "920", "1750"))

    async def test_adb_advertises_elements_only_when_uiautomator_exists(self):
        adapter = ADBAdapter("android", {})
        adapter._run = AsyncMock(
            side_effect=[
                (0, b"device\n", b""),
                (1, b"", b"not found"),
                (0, b"device\n", b""),
                (0, b"/system/bin/uiautomator\n", b""),
            ]
        )

        with patch("wayweaver.adapters.adb.shutil.which", return_value="/usr/bin/adb"):
            first, _ = await adapter.available()
            without = adapter.capabilities
            second, _ = await adapter.available()
            with_uia = adapter.capabilities

        self.assertTrue(first and second)
        self.assertNotIn(Capability.ELEMENTS, without)
        self.assertIn(Capability.ELEMENTS, with_uia)


class WaylandBackendTests(unittest.IsolatedAsyncioTestCase):
    async def test_gnome_bridge_decodes_gvariant_and_dispatches_window_action(self):
        calls = []

        async def run(command, stdin=None):
            calls.append(command)
            if command.endswith("ListWindows"):
                return (
                    b"""('{"windows":[{"id":"7","title":"Editor","active":true}]}',)"""
                )
            return b"""('{"ok":true}',)"""

        backend = GnomeBackend(run)
        await backend.probe()
        windows = await backend.windows()
        await backend.window_action("focus_window", windows[0], {})

        self.assertEqual(windows[0]["title"], "Editor")
        self.assertIn("WindowAction", calls[-1])
        self.assertIn("focus_window", calls[-1])

    async def test_kdotool_returns_window_geometry_and_workspace_state(self):
        async def run(command, stdin=None):
            if "search --name" in command:
                return b"abc\n"
            if "getactivewindow" in command:
                return b"abc\n"
            if "getwindowname" in command:
                return b"Editor\norg.demo.Editor\n42\nWindow abc\n Position: 10,20\n Geometry: 800x600\n2\n"
            if command == "kdotool get_desktop":
                return b"2\n"
            if command == "kdotool get_num_desktops":
                return b"3\n"
            return b""

        backend = KDotoolBackend(run)
        windows = await backend.windows()
        workspaces = await backend.workspaces()

        self.assertEqual(
            windows[0],
            {
                "id": "abc",
                "title": "Editor",
                "class": "org.demo.Editor",
                "pid": 42,
                "desktop": 2,
                "x": 10,
                "y": 20,
                "width": 800,
                "height": 600,
                "active": True,
            },
        )
        self.assertEqual(
            [value["active"] for value in workspaces], [False, True, False]
        )


if __name__ == "__main__":
    unittest.main()


class KeyVocabularyParityTests(unittest.IsolatedAsyncioTestCase):
    """Every canonical key name must reach every backend that can express it.

    A name a backend does not know is not an error the caller sees: it is
    dropped, the action reports success, and the desktop never changed. This
    walks the whole advertised vocabulary through each backend rather than
    comparing tables, because several backends normalize a name before looking
    it up.
    """

    @staticmethod
    def canonical():
        from wayweaver.contracts import _KEY_ALIASES

        return sorted(set(_KEY_ALIASES.values()))

    def test_vnc_expresses_every_canonical_key(self):
        from wayweaver.adapters.vnc import VNCAdapter

        adapter = VNCAdapter("vnc", {"host": "h"})
        for key in self.canonical():
            with self.subTest(key=key):
                self.assertIsInstance(adapter._keysym(key), int)

    def test_rdp_expresses_every_canonical_key(self):
        from wayweaver.adapters.rdp import _VIRTUAL_KEYS

        for key in self.canonical():
            with self.subTest(key=key):
                self.assertTrue(len(key) == 1 or key.casefold() in _VIRTUAL_KEYS)

    async def test_cdp_sends_a_virtual_key_code_for_every_canonical_key(self):
        from wayweaver.adapters.cdp import CDPAdapter

        # Without a virtual key code the browser accepts the event and the page
        # ignores it, which is the silent failure this vocabulary exists to stop.
        adapter = CDPAdapter("cdp", {"url": "http://localhost"})
        events = []

        async def browser(method, params):
            events.append(params)
            return {}

        adapter.browser = browser
        for key in self.canonical():
            with self.subTest(key=key):
                events.clear()
                await adapter.act("key", {"key": key})
                self.assertTrue(
                    any(item.get("type") == "rawKeyDown" for item in events),
                    f"{key} reached the page without a virtual key code",
                )

    def test_android_maps_the_keys_the_platform_defines(self):
        from wayweaver.adapters.adb import _KEYEVENTS

        # Android has no keycode past F12, so those stay absent deliberately
        # and are refused by name at the call site.
        missing = [
            key
            for key in self.canonical()
            if key.casefold() not in _KEYEVENTS and len(key) > 1
        ]
        self.assertEqual(missing, [f"F{index}" for index in range(13, 25)])


class AndroidShellQuotingTests(unittest.IsolatedAsyncioTestCase):
    """adb joins everything after `shell` into one command string.

    An unquoted argument therefore leaves the text's own metacharacters active
    on the device.
    """

    def adapter(self):
        adapter = ADBAdapter("adb", {})
        sent = []

        async def checked(*args):
            sent.append(args)
            return b""

        adapter._checked = checked
        return adapter, sent

    async def test_typed_text_cannot_run_a_second_command(self):
        adapter, sent = self.adapter()
        await adapter.act("type", {"text": "x; rm -rf /"})
        argument = sent[-1][-1]
        self.assertTrue(argument.startswith("'") and argument.endswith("'"))
        self.assertNotIn("; rm", argument.strip("'").replace("%s", " ")[:2])

    async def test_typed_text_keeps_a_quote_intact(self):
        adapter, sent = self.adapter()
        await adapter.act("type", {"text": "it's"})
        self.assertEqual(sent[-1][-1], """'it'"'"'s'""")

    async def test_shell_command_keeps_its_own_arguments(self):
        # `sh -c "ls -la"` arrived as `sh -c ls -la`, which runs ls with -la
        # as $0 rather than as a flag.
        adapter = ADBAdapter("adb", {})
        seen = []

        async def run(*args, stdin=None):
            seen.append(args)
            return 0, b"", b""

        adapter._run = run
        await adapter.shell("ls -la /sdcard")
        self.assertEqual(seen[-1], ("shell", "sh", "-c", "'ls -la /sdcard'"))

    async def test_an_unknown_key_is_refused_not_forwarded(self):
        adapter, sent = self.adapter()
        with self.assertRaises(ActionError):
            await adapter.act("key", {"key": "F13"})
        self.assertEqual(sent, [])

    async def test_a_chord_is_refused_rather_than_pressed_in_sequence(self):
        adapter, sent = self.adapter()
        with self.assertRaises(ActionError) as caught:
            await adapter.act("hotkey", {"keys": ["ctrl+a"]})
        self.assertIn("chord", str(caught.exception))
        self.assertEqual(sent, [])


class RDPSessionTests(unittest.IsolatedAsyncioTestCase):
    """A cached connection must not outlive the session it belongs to."""

    def adapter(self):
        from wayweaver.adapters.rdp import RDPAdapter

        adapter = RDPAdapter("rdp", {"url": "rdp://host"})
        adapter.connection = object()
        return adapter

    async def test_a_failed_send_discards_the_connection(self):
        adapter = self.adapter()
        with self.assertRaises(ActionError):
            adapter._fail("connection reset")
        self.assertIsNone(adapter.connection)

    async def test_availability_reports_a_host_it_cannot_reach(self):
        # Availability used to follow from the library importing, so an
        # unreachable host looked usable and routing preferred it over an
        # adapter that worked.
        adapter = self.adapter()
        adapter.connection = None

        async def refuse():
            raise ActionError("connection refused")

        adapter._ensure = refuse
        with patch.dict(sys.modules, {"aardwolf": types.ModuleType("aardwolf")}):
            available, reason = await adapter.available()
        self.assertFalse(available)
        self.assertIn("refused", reason)

    async def test_availability_still_reports_a_missing_library(self):
        adapter = self.adapter()
        with patch.dict(sys.modules, {"aardwolf": None}):
            available, reason = await adapter.available()
        self.assertFalse(available)
        self.assertIn("wayweaver-agent[rdp]", reason)
