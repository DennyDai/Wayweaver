import asyncio
import json
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, call, patch

from types import SimpleNamespace
from wayweaver.adapters.base import Adapter
from wayweaver.adapters.cdp import CDPAdapter
from wayweaver.adapters.local import LocalAdapter
from wayweaver.adapters.ssh import SSHAdapter
from wayweaver.adapters.vnc import VNCAdapter
from wayweaver.adapters.wayland import WaylandAdapter
from wayweaver.controller import Controller
from wayweaver.config import TargetConfig
from wayweaver.contracts import validate_params
from wayweaver.errors import (
    ActionError,
    CapabilityError,
    ConfigError,
    ContractError,
    ProtocolError,
    SurfaceError,
    error_payload,
)
from wayweaver.operations import API_VERSION, OPERATIONS
from wayweaver.router import Router, _REPROBE_AFTER_MISS
from wayweaver.sequence import normalize
from wayweaver.types import Frame
from wayweaver.types import Capability
from wayweaver.vision import Word


class StubAdapter(Adapter):
    def __init__(
        self, name, kind, capabilities, *, available=True, raw=None, failure=None
    ):
        super().__init__(name, {})
        self.kind = kind
        self.capabilities = frozenset(capabilities)
        self._is_available = available
        self.raw_operations = raw or {}
        self.failure = failure

    async def available(self):
        return self._is_available, None if self._is_available else "missing"

    async def perform(self, operation, params):
        if self.failure:
            raise ActionError(self.failure)
        return {"operation": operation, "params": params}

    async def raw(self, operation, params):
        return {"operation": operation, "params": params}


class LocalAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_executes_in_configured_environment_and_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = LocalAdapter(
                "local",
                {
                    "cwd": directory,
                    "environment": {"WAYWEAVER_LOCAL_TEST": "native"},
                },
            )
            available, reason = await adapter.available()
            code, stdout, stderr = await adapter.shell(
                'printf "%s:%s:" "$WAYWEAVER_LOCAL_TEST" "$PWD"; cat',
                b"payload",
            )

        self.assertTrue(available)
        self.assertIsNone(reason)
        self.assertEqual(code, 0)
        self.assertEqual(
            stdout.decode(),
            f"native:{Path(directory)}:payload",
        )
        self.assertEqual(stderr, b"")

    async def test_shell_result_distinguishes_transport_and_command_success(self):
        adapter = LocalAdapter("local", {})

        failed = await adapter.perform(
            "shell.execute",
            {"command": "exit 7"},
        )
        accepted = await adapter.perform(
            "shell.execute",
            {"command": "exit 7", "allowed_exit_codes": [0, 7]},
        )

        self.assertFalse(failed["success"])
        self.assertEqual(failed["exit_code"], 7)
        self.assertTrue(accepted["success"])
        with self.assertRaisesRegex(ActionError, "shell command exited 7"):
            await adapter.perform(
                "shell.execute",
                {"command": "exit 7", "check": True},
            )

    def test_router_resolves_desktop_adapter_through_local_transport(self):
        router = Router(
            TargetConfig(
                "host",
                ("x11", "local"),
                {
                    "x11": {
                        "kind": "x11",
                        "transport": "local",
                        "display": ":0",
                    },
                    "local": {"kind": "local"},
                },
            )
        )

        self.assertIs(router.adapters["x11"].transport, router.adapters["local"])

    def test_desktop_adapter_requires_shell_capable_transport(self):
        with self.assertRaisesRegex(ConfigError, "shell-capable transport"):
            Router(
                TargetConfig(
                    "broken",
                    (),
                    {
                        "x11": {
                            "kind": "x11",
                            "transport": "missing",
                        },
                    },
                )
            )


class SSHAdapterTests(unittest.IsolatedAsyncioTestCase):
    def test_defaults_to_accepting_only_new_host_keys(self):
        adapter = SSHAdapter("ssh", {"host": "example"})
        args, _, temporary = adapter._argv()

        self.assertIn("StrictHostKeyChecking=accept-new", args)
        self.assertNotIn("UserKnownHostsFile=/dev/null", args)
        self.assertIsNone(temporary)
        self.assertIn("ControlMaster=auto", args)
        self.assertIn("ControlPersist=60", args)
        self.assertTrue(any(value.startswith("ControlPath=") for value in args))

    async def test_availability_requires_a_successful_remote_probe(self):
        adapter = SSHAdapter("ssh", {"host": "example"})
        adapter.shell = AsyncMock(return_value=(255, b"", b"permission denied"))

        with patch("wayweaver.adapters.ssh.shutil.which", return_value="/usr/bin/ssh"):
            available, reason = await adapter.available()
            cached = await adapter.available()

        self.assertFalse(available)
        self.assertEqual(reason, "permission denied")
        self.assertEqual(cached, (False, "permission denied"))
        adapter.shell.assert_awaited_once_with("true")


class CDPTransportTests(unittest.TestCase):
    def adapter(self, socket):
        adapter = CDPAdapter.__new__(CDPAdapter)
        adapter.config = {}
        adapter.timeout = 1.0
        adapter._socket = socket
        adapter._next_id = 0
        adapter._lock = threading.Lock()
        return adapter

    def test_transport_failure_discards_the_socket(self):
        class DeadSocket:
            closed = False

            def send(self, payload):
                raise OSError("connection reset")

            class sock:  # noqa: N801 - mirrors the real attribute
                @staticmethod
                def close():
                    DeadSocket.closed = True

        adapter = self.adapter(DeadSocket())

        with self.assertRaises(OSError):
            adapter._call("Runtime.evaluate", {})

        # Left in place, the next call would reuse a dead socket forever.
        self.assertIsNone(adapter._socket)
        self.assertTrue(DeadSocket.closed)

    def test_a_method_error_keeps_the_socket(self):
        class LiveSocket:
            def send(self, payload):
                self.sent = json.loads(payload)

            def receive(self):
                return json.dumps(
                    {"id": self.sent["id"], "error": {"message": "no such method"}}
                ).encode()

            sock = None

        socket = LiveSocket()
        adapter = self.adapter(socket)

        with self.assertRaises(ProtocolError):
            adapter._call("No.SuchMethod", {})

        self.assertIs(adapter._socket, socket)

    def test_navigation_keys_carry_a_virtual_key_code(self):
        # Without a virtual key code Chrome delivers the event but performs no
        # default action, so navigation keys silently did nothing.
        sent = []

        class RecordingSocket:
            def send(self, payload):
                message = json.loads(payload)
                self.id = message["id"]
                if message["method"] == "Input.dispatchKeyEvent":
                    sent.append(message["params"])

            def receive(self):
                return json.dumps({"id": self.id, "result": {}}).encode()

            sock = None

        adapter = self.adapter(RecordingSocket())
        asyncio.run(adapter.act("key", {"keys": ["Page_Down"]}))

        self.assertEqual(sent[0]["key"], "PageDown")
        self.assertEqual(sent[0]["windowsVirtualKeyCode"], 34)
        self.assertEqual(sent[0]["type"], "rawKeyDown")

    def test_text_keys_do_not_claim_a_virtual_key_code(self):
        sent = []

        class RecordingSocket:
            def send(self, payload):
                message = json.loads(payload)
                self.id = message["id"]
                if message["method"] == "Input.dispatchKeyEvent":
                    sent.append(message["params"])

            def receive(self):
                return json.dumps({"id": self.id, "result": {}}).encode()

            sock = None

        adapter = self.adapter(RecordingSocket())
        asyncio.run(adapter.act("key", {"keys": ["a"]}))

        self.assertEqual(sent[0]["type"], "keyDown")
        self.assertNotIn("windowsVirtualKeyCode", sent[0])

    def test_out_of_band_messages_are_skipped(self):
        class EventSocket:
            def send(self, payload):
                self.sent = json.loads(payload)
                self.replies = [
                    json.dumps({"method": "Page.loadEventFired"}).encode(),
                    json.dumps({"id": self.sent["id"] - 1, "result": {"stale": True}}).encode(),
                    json.dumps({"id": self.sent["id"], "result": {"value": 7}}).encode(),
                ]

            def receive(self):
                return self.replies.pop(0)

            sock = None

        adapter = self.adapter(EventSocket())

        self.assertEqual(adapter._call("Runtime.evaluate", {}), {"value": 7})


class ElementPointTimeoutTests(unittest.IsolatedAsyncioTestCase):
    """element.point must honor the timeout its contract always accepted.

    The schema admitted timeout_ms and the implementation ignored it, so a
    resolve issued right after a navigation failed on a form that
    legitimately did not exist yet, and every caller grew its own retry loop.
    """

    def controller(self, appears_after):
        from wayweaver.controller import Controller

        controller = Controller.__new__(Controller)

        class Elements:
            name = "atspi"

            def __init__(self):
                self.queries = 0

            async def perform(self, _operation, _params):
                self.queries += 1
                if self.queries <= appears_after:
                    raise ActionError("accessible element not found")
                return {"bounds": {"left": 100, "top": 200,
                                   "width": 50, "height": 20}}

        elements = Elements()

        class FakeRouter:
            async def select(self, _requirement):
                return elements

        controller.router = lambda target: FakeRouter()

        async def capture(target, params=None):
            return SimpleNamespace(width=1600, height=900), "x11"

        controller.capture = capture

        async def hint(target):
            return None

        controller._application_hint = hint
        return controller, elements

    async def test_resolution_retries_until_the_element_exists(self):
        controller, elements = self.controller(appears_after=3)
        result = await controller._element_point(
            "desktop", {"timeout": 5, "interval": 0.05}, allow_fallback=False
        )
        self.assertEqual(result["tier"], "semantic")
        self.assertGreater(elements.queries, 3)

    async def test_without_a_timeout_one_miss_is_final(self):
        controller, elements = self.controller(appears_after=1)
        with self.assertRaises(CapabilityError):
            await controller._element_point("desktop", {}, allow_fallback=False)


class LocateFailureContextTests(unittest.IsolatedAsyncioTestCase):
    """A locator that scoped itself must say so when it finds nothing.

    Scoping to the active window is an optimization the caller did not ask
    for, so "text not found" reads as "the text is not on screen" when the
    truth is that a dialog took focus and the search never looked at the
    window the caller had in mind.
    """

    def controller(self, title):
        from wayweaver.controller import Controller

        controller = Controller.__new__(Controller)
        controller.config = SimpleNamespace(cache_dir=Path("/tmp/wayweaver-test"))
        controller._ocr_cache = {}

        class Desktop:
            name = "x11"

            async def capture(self, params=None):
                return Frame(b"", 1600, 900, "x11")

        class FakeRouter:
            async def select(self, requirement):
                return Desktop()

        controller.router = lambda target: FakeRouter()

        async def region(target, window):
            if title is None:
                raise CapabilityError("no window metadata")
            return [0, 51, 1600, 900], title

        controller._window_region = region

        async def ocr(target, frame):
            return []

        controller._ocr = ocr
        return controller

    async def test_the_searched_window_is_named(self):
        controller = self.controller("Defined Strings [CodeBrowser]")
        with self.assertRaises(ActionError) as caught:
            await controller.locate("desktop", {"text": "Analysis", "timeout": 0})
        message = str(caught.exception)
        self.assertIn("Defined Strings [CodeBrowser]", message)
        self.assertIn("window", message)
        self.assertEqual(
            caught.exception.details["searched_window"],
            "Defined Strings [CodeBrowser]",
        )

    async def test_a_target_without_windows_says_nothing_extra(self):
        # Pixel-only surfaces never scoped, so there is no window to blame.
        controller = self.controller(None)
        with self.assertRaises(ActionError) as caught:
            await controller.locate("desktop", {"text": "Analysis", "timeout": 0})
        self.assertNotIn("searched the active window", str(caught.exception))


class ScrollBoundaryTests(unittest.IsolatedAsyncioTestCase):
    """Reachable means inside the window showing the element, not on the screen.

    A window shorter than the screen leaves a band where a point is numerically
    on the surface and physically on whatever sits beneath the window. Measured
    live: a field at desktop y=899 under a window ending at 890 on a 900px
    screen was ruled reachable, and the click landed on the panel below.
    """

    def controller(self, window_region):
        from wayweaver.controller import Controller

        controller = Controller.__new__(Controller)

        class Elements:
            name = "atspi"

            def __init__(self):
                self.position = 880
                self.queries = 0

            async def perform(self, _operation, _params):
                self.queries += 1
                return {"bounds": {"left": 394, "top": self.position,
                                   "width": 216, "height": 39}}

        class Scroller:
            def __init__(self, elements):
                self.elements = elements
                self.scrolls = []

            async def act(self, _action, params):
                self.scrolls.append(params)
                self.elements.position = 500

        elements = Elements()
        scroller = Scroller(elements)

        class FakeRouter:
            async def select(self, requirement):
                return scroller if requirement == Capability.SCROLL else elements

        controller.router = lambda target: FakeRouter()

        async def capture(target, params=None):
            return SimpleNamespace(width=1600, height=900), "x11"

        controller.capture = capture

        async def region(target, window):
            if window_region is None:
                raise CapabilityError("no window metadata")
            return window_region, "Test Window"

        controller._window_region = region

        async def hint(target):
            return None

        controller._application_hint = hint
        return controller, elements, scroller

    async def test_a_point_below_the_window_triggers_a_scroll(self):
        controller, elements, scroller = self.controller([10, 37, 1060, 890])
        result = await controller._element_point(
            "desktop", {"scroll": 2}, allow_fallback=False
        )
        self.assertEqual(len(scroller.scrolls), 1)
        self.assertEqual(scroller.scrolls[0]["direction"], "down")
        self.assertEqual(result["point"]["y"], 500 + 39 // 2)

    async def test_without_window_metadata_the_screen_still_bounds(self):
        # Pixel-only targets report no windows; the screen is all there is.
        controller, elements, scroller = self.controller(None)
        result = await controller._element_point(
            "desktop", {"scroll": 2}, allow_fallback=False
        )
        self.assertEqual(scroller.scrolls, [])
        self.assertEqual(result["point"]["y"], 880 + 39 // 2)

    async def test_a_point_above_the_window_scrolls_up(self):
        controller, elements, scroller = self.controller([10, 400, 1060, 890])
        elements.position = 200
        await controller._element_point("desktop", {"scroll": 2},
                                        allow_fallback=False)
        self.assertEqual(scroller.scrolls[0]["direction"], "up")


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    """Cleanup that stops at the first failure leaks everything after it.

    A multiplexed SSH socket, a helper process and a browser connection all
    outlived the run because one unrelated adapter raised on close.
    """

    class Closer(StubAdapter):
        def __init__(self, name, fail=False):
            super().__init__(name, name, set())
            self.fail = fail
            self.closed = False

        async def close(self):
            if self.fail:
                raise RuntimeError(f"{self.name} refused to close")
            self.closed = True

    def router(self, adapters):
        router = Router.__new__(Router)
        router.adapters = {adapter.name: adapter for adapter in adapters}
        return router

    async def test_one_failure_does_not_strand_the_others(self):
        first = self.Closer("first")
        broken = self.Closer("broken", fail=True)
        last = self.Closer("last")
        await self.router([first, broken, last]).close()
        self.assertTrue(first.closed)
        self.assertTrue(last.closed)

    async def test_every_router_is_closed(self):
        from wayweaver.controller import Controller

        broken = self.Closer("broken", fail=True)
        healthy = self.Closer("healthy")
        controller = Controller.__new__(Controller)
        controller.routers = {
            "a": self.router([broken]),
            "b": self.router([healthy]),
        }
        await controller.close()
        self.assertTrue(healthy.closed)


class ProbeRecoveryTests(unittest.IsolatedAsyncioTestCase):
    """A momentary probe failure must not be cached for the whole TTL.

    An adapter that dropped off its bus for a second stayed unavailable for
    thirty, so a caller waiting on it retried against the same stale snapshot
    and could never succeed -- observed live as element.point failing for its
    full timeout while the accessibility layer had already recovered.
    """

    class Flaky(StubAdapter):
        def __init__(self):
            super().__init__("atspi", "atspi", {Capability.ELEMENTS})
            self.healthy = False
            self.probes = 0

        async def available(self):
            self.probes += 1
            return self.healthy, None if self.healthy else "bus lost"

    def router(self, adapter):
        router = Router.__new__(Router)
        router.target = TargetConfig("desktop", ("atspi",), {}, 30.0)
        router.adapters = {adapter.name: adapter}
        router.status = {}
        router.probed_at = 0.0
        return router

    async def test_recovery_is_noticed_before_the_ttl_expires(self):
        adapter = self.Flaky()
        router = self.router(adapter)
        self.assertIsNone(await router._available({Capability.ELEMENTS}))
        adapter.healthy = True
        # The TTL is 30s; without re-probing on a miss this stays None.
        router.probed_at -= _REPROBE_AFTER_MISS + 0.1
        found = await router._available({Capability.ELEMENTS})
        self.assertIs(found, adapter)

    async def test_a_burst_of_misses_does_not_become_a_burst_of_probes(self):
        adapter = self.Flaky()
        router = self.router(adapter)
        for _ in range(5):
            self.assertIsNone(await router._available({Capability.ELEMENTS}))
        # One initial probe plus at most one re-probe for the whole burst.
        self.assertLessEqual(adapter.probes, 2)

    async def test_a_healthy_adapter_is_never_re_probed(self):
        adapter = self.Flaky()
        adapter.healthy = True
        router = self.router(adapter)
        for _ in range(4):
            await router._available({Capability.ELEMENTS})
        self.assertEqual(adapter.probes, 1)


class ElementSearchScopeTests(unittest.IsolatedAsyncioTestCase):
    """Element searches are scoped to the active application when possible.

    An unscoped search walks every application registered on the desktop.
    Measured against a live session with a large Java toolkit running, that was
    11 seconds versus 30ms for the same search inside one application.
    """

    class Elements:
        def __init__(self, answers):
            self.answers, self.queries = answers, []

        async def perform(self, _operation, params):
            self.queries.append(params)
            reply = self.answers.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

    async def test_the_hint_scopes_the_search(self):
        elements = self.Elements([{"bounds": {"left": 1, "top": 2, "width": 3, "height": 4}}])
        await Controller._find_element(elements, {"name": "Username"}, "Google Chrome")
        self.assertEqual(elements.queries[0]["application"], "Google Chrome")
        self.assertTrue(elements.queries[0]["include_offscreen"])

    async def test_a_hint_that_names_nothing_falls_back_to_the_desktop(self):
        # Scoping is only ever a hint; a title naming no application, or naming
        # two, must not turn a findable element into a failure.
        elements = self.Elements([
            ActionError("accessible application not found: 'Attention'"),
            {"bounds": {"left": 1, "top": 2, "width": 3, "height": 4}},
        ])
        found = await Controller._find_element(
            elements, {"name": "Username"}, "Attention"
        )
        self.assertEqual(len(elements.queries), 2)
        self.assertNotIn("application", elements.queries[1])
        self.assertEqual(found["bounds"]["top"], 2)

    async def test_an_explicit_application_is_never_overridden(self):
        elements = self.Elements([{"bounds": {"left": 1, "top": 2, "width": 3, "height": 4}}])
        await Controller._find_element(
            elements, {"name": "Username", "application": "Thunar"}, "Google Chrome"
        )
        self.assertEqual(elements.queries[0]["application"], "Thunar")


class ScrollSettleTests(unittest.IsolatedAsyncioTestCase):
    """A scrolled element is only settled once its position holds still.

    Browsers animate scrolling, so an early reading catches a position the
    element is still moving through and the click lands on whatever it passes
    over. Counting readings instead of elapsed time ties the answer to query
    speed, which is how scoping searches to one application reintroduced the
    miss it was meant to prevent.
    """

    class Moving:
        def __init__(self, positions):
            self.positions = list(positions)

        async def perform(self, _operation, _params):
            top = self.positions[0]
            if len(self.positions) > 1:
                self.positions.pop(0)
            return {"bounds": {"left": 0, "top": top, "width": 10, "height": 10}}

    def controller(self):
        return Controller.__new__(Controller)

    async def test_a_still_moving_element_is_not_reported_settled(self):
        # Every reading differs, so it never holds still; the deadline decides.
        elements = self.Moving(range(100, 100000, 7))
        moved = await self.controller()._await_moved(
            elements, {}, {"left": 0, "top": 100}, timeout=0.4
        )
        self.assertTrue(moved)  # it did move, but only the deadline ended it

    async def test_an_element_that_never_moves_reports_no_movement(self):
        elements = self.Moving([500])
        moved = await self.controller()._await_moved(
            elements, {}, {"left": 0, "top": 500}, timeout=0.4
        )
        self.assertFalse(moved)

    async def test_a_settled_element_is_reported_promptly(self):
        elements = self.Moving([700])
        start = time.monotonic()
        moved = await self.controller()._await_moved(
            elements, {}, {"left": 0, "top": 100}, timeout=3.0
        )
        self.assertTrue(moved)
        self.assertLess(time.monotonic() - start, 1.5)


class ShellSessionStreamTests(unittest.IsolatedAsyncioTestCase):
    """A session reply must not be capped at asyncio's default line limit.

    Accessibility trees are the largest thing a helper returns and a browser's
    runs past 64 KiB, so the default made `readline` fail on exactly the
    applications worth inspecting.
    """

    class FakeStdout:
        def __init__(self, error=None, line=b""):
            self.error, self.line = error, line

        async def readline(self):
            if self.error is not None:
                raise self.error
            return self.line

    class FakeProcess:
        def __init__(self, stdout):
            self.stdout = stdout
            self.returncode = None
            self.stdin = None

    def session(self, stdout):
        from wayweaver.adapters.base import ShellSession

        return ShellSession(self.FakeProcess(stdout))

    async def test_an_over_long_reply_names_the_limit_it_hit(self):
        from wayweaver.adapters.base import SESSION_STREAM_LIMIT

        # readline turns an over-long line into a bare ValueError whose text
        # mentions no limit and no remedy.
        session = self.session(self.FakeStdout(error=ValueError("chunk too long")))
        with self.assertRaises(ActionError) as caught:
            await session.banner(1)
        self.assertIn(str(SESSION_STREAM_LIMIT), str(caught.exception))
        self.assertIn("max_depth", str(caught.exception))

    async def test_the_limit_exceeds_a_browser_accessibility_tree(self):
        from wayweaver.adapters.base import SESSION_STREAM_LIMIT

        # A measured Chrome tree came to 106,851 bytes on one page.
        self.assertGreater(SESSION_STREAM_LIMIT, 1_000_000)

    async def test_sessions_are_opened_with_the_raised_limit(self):
        import asyncio as _asyncio

        from wayweaver.adapters.base import SESSION_STREAM_LIMIT
        from wayweaver.adapters.local import LocalAdapter

        seen = {}

        async def spy(*args, **kwargs):
            seen.update(kwargs)
            raise RuntimeError("stop here")

        original = _asyncio.create_subprocess_exec
        _asyncio.create_subprocess_exec = spy
        try:
            with self.assertRaises(RuntimeError):
                await LocalAdapter("local", {}).open_session("true")
        finally:
            _asyncio.create_subprocess_exec = original
        self.assertEqual(seen.get("limit"), SESSION_STREAM_LIMIT)


class ATSPISessionTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, transport):
        from wayweaver.adapters.atspi import ATSPIAdapter

        return ATSPIAdapter("atspi", {"display": ":1"}, transport)

    class FakeSession:
        def __init__(self, replies, hello=None):
            self.replies = list(replies)
            self.requests = []
            self.closed = False
            self.hello = {"ready": True} if hello is None else hello

        async def banner(self, timeout):
            return self.hello

        async def request(self, payload, timeout):
            self.requests.append(payload)
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        async def close(self):
            self.closed = True

    class FakeTransport(StubAdapter):
        def __init__(self, session):
            super().__init__("ssh", "ssh", {Capability.SHELL})
            self.session = session
            self.shell_calls = 0

        async def open_session(self, command):
            return self.session

        async def shell(self, command, stdin=None):
            self.shell_calls += 1
            return 0, b'{"fallback": true}', b""

    async def test_reuses_one_session_across_calls(self):
        session = self.FakeSession([
            {"ok": True, "result": {"name": "Save"}},
            {"ok": True, "result": {"name": "Open"}},
        ])
        transport = self.FakeTransport(session)
        adapter = self.adapter(transport)

        first = await adapter._call("find", {"name": "Save"})
        second = await adapter._call("find", {"name": "Open"})

        self.assertEqual(first["name"], "Save")
        self.assertEqual(second["name"], "Open")
        # The greeting is consumed separately, so each call sends exactly one
        # request and receives its own reply.
        self.assertEqual(len(session.requests), 2)
        self.assertEqual(transport.shell_calls, 0)

    async def test_a_broken_session_is_discarded_not_reused(self):
        session = self.FakeSession([OSError("pipe closed")])
        transport = self.FakeTransport(session)
        adapter = self.adapter(transport)

        with self.assertRaises(OSError):
            await adapter._call("find", {"name": "Save"})

        self.assertIsNone(adapter._session)
        self.assertTrue(session.closed)

    async def test_falls_back_when_the_transport_has_no_sessions(self):
        transport = self.FakeTransport(None)
        adapter = self.adapter(transport)

        result = await adapter._call("find", {"name": "Save"})

        self.assertTrue(result["fallback"])
        self.assertEqual(transport.shell_calls, 1)

    async def test_helper_errors_surface_from_the_session(self):
        session = self.FakeSession([{"ok": False, "error": "accessible element not found"}])
        adapter = self.adapter(self.FakeTransport(session))

        with self.assertRaisesRegex(ActionError, "not found"):
            await adapter._call("find", {"name": "Nope"})


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def router(self, adapters, prefer=()):
        router = object.__new__(Router)
        router.target = TargetConfig("test", tuple(prefer), {})
        router.adapters = {adapter.name: adapter for adapter in adapters}
        router.status = {}
        router.probed_at = 0.0
        return router

    async def test_semantic_operation_falls_back_only_when_declared(self):
        semantic = StubAdapter("atspi", "atspi", {Capability.ELEMENTS}, available=False)
        visual = StubAdapter("x11", "x11", {Capability.CAPTURE, Capability.POINTER})
        router = self.router([semantic, visual], ("atspi", "x11"))

        adapter, fallback = await router.select_operation("element.activate")
        self.assertIs(adapter, visual)
        self.assertTrue(fallback)
        with self.assertRaises(CapabilityError):
            await router.select_operation("element.list")

    async def test_raw_operations_are_opt_in_and_availability_gated(self):
        available = StubAdapter("live", "test", set(), raw={"inspect": "Inspect state"})
        unavailable = StubAdapter(
            "dead", "test", set(), available=False, raw={"mutate": "Mutate state"}
        )
        router = self.router([available, unavailable])

        self.assertNotIn("raw:live", await router.operation_routes())
        routes = await router.operation_routes(include_raw=True)
        self.assertEqual(routes["raw:live"]["operations"], {"inspect": "Inspect state"})
        self.assertNotIn("raw:dead", routes)

    async def test_reuses_adapter_status_within_target_probe_ttl(self):
        adapter = StubAdapter("x11", "x11", {Capability.CAPTURE})
        adapter.available = AsyncMock(return_value=(True, None))
        router = self.router([adapter], ("x11",))
        router.target = TargetConfig("test", ("x11",), {}, probe_ttl=30)

        await router.probe()
        router.probed_at = time.monotonic()
        await router.select(Capability.CAPTURE)
        await router.select(Capability.CAPTURE)

        adapter.available.assert_awaited_once_with()

    async def test_reprobes_adapter_status_after_target_probe_ttl(self):
        adapter = StubAdapter("x11", "x11", {Capability.CAPTURE})
        adapter.available = AsyncMock(return_value=(True, None))
        router = self.router([adapter], ("x11",))
        router.target = TargetConfig("test", ("x11",), {}, probe_ttl=30)

        await router.probe()
        router.probed_at = time.monotonic() - 31
        await router.select(Capability.CAPTURE)

        self.assertEqual(adapter.available.await_count, 2)


class ControllerTests(unittest.IsolatedAsyncioTestCase):
    def _visual_controller(self, directory, capture):
        visual = StubAdapter("x11", "x11", {Capability.CAPTURE})
        visual.capture = capture
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {})
        router.adapters = {"x11": visual}
        router.status = {}
        router.probed_at = 0.0
        controller = Controller(SimpleNamespace(cache_dir=Path(directory)))
        controller.routers = {"test": router}
        return controller, visual

    async def test_regional_locator_never_recognizes_the_whole_screen(self):
        capture = AsyncMock(return_value=Frame(b"crop", 300, 300, "x11"))
        with tempfile.TemporaryDirectory() as directory:
            controller, _ = self._visual_controller(directory, capture)
            controller._ocr = AsyncMock(return_value=[])

            with self.assertRaises(ActionError):
                await controller.locate(
                    "test",
                    {"text": "Missing", "region": [600, 400, 900, 700], "timeout": 0},
                )

        self.assertEqual(
            capture.await_args_list,
            [call({"region": [600, 400, 900, 700]})],
        )

    async def test_regional_locator_widens_only_when_asked(self):
        capture = AsyncMock(return_value=Frame(b"crop", 300, 300, "x11"))
        with tempfile.TemporaryDirectory() as directory:
            controller, _ = self._visual_controller(directory, capture)
            controller._ocr = AsyncMock(return_value=[])

            with self.assertRaises(ActionError):
                await controller.locate(
                    "test",
                    {
                        "text": "Missing",
                        "region": [600, 400, 900, 700],
                        "full_screen_fallback": True,
                        "timeout": 0,
                    },
                )

        self.assertEqual(
            capture.await_args_list,
            [call({"region": [600, 400, 900, 700]}), call(None)],
        )

    async def test_locator_widens_when_the_surface_cannot_crop(self):
        capture = AsyncMock(
            side_effect=[
                ActionError("VNC capture does not support a window or region"),
                Frame(b"screen", 1600, 900, "vnc"),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            controller, _ = self._visual_controller(directory, capture)
            controller._ocr = AsyncMock(
                return_value=[
                    Word("Next", "next", 96.2, 700, 500, 50, 20, ("1", "1", "1", "1"))
                ]
            )

            result = await controller.locate(
                "test",
                {"text": "Next", "region": [600, 400, 900, 700], "timeout": 0},
            )

        self.assertEqual(result["ocr_scope"], "screen")
        self.assertEqual(result["x"], 725)
        self.assertEqual(
            capture.await_args_list,
            [call({"region": [600, 400, 900, 700]}), call(None)],
        )

    def _layered_controller(self, *, bounds, surface=(1600, 900), elements=True):
        """A target whose element tree and pixel surface come from different adapters."""

        class ElementAdapter(StubAdapter):
            async def perform(self, operation, params):
                if bounds is None:
                    raise ActionError("accessible element not found")
                return {"name": "Save", "role": "push button", "bounds": bounds}

        class PixelAdapter(StubAdapter):
            async def capture(self, params=None):
                return Frame(b"png", surface[0], surface[1], "vnc")

        pixels = PixelAdapter("vnc", "vnc", {Capability.CAPTURE, Capability.POINTER})
        adapters = {"vnc": pixels}
        if elements:
            adapters["atspi"] = ElementAdapter("atspi", "atspi", {Capability.ELEMENTS})
        router = object.__new__(Router)
        router.target = TargetConfig("test", tuple(adapters), {})
        router.adapters = adapters
        router.status = {}
        router.probed_at = 0.0
        controller = object.__new__(Controller)
        controller.routers = {"test": router}
        controller.observations = {}
        return controller

    def _typing_controller(self, focused):
        class ElementAdapter(StubAdapter):
            async def perform(self, operation, params):
                if operation == "element.focused":
                    return focused
                return await super().perform(operation, params)

        class KeyboardAdapter(StubAdapter):
            async def perform(self, operation, params):
                return {"characters": len(params.get("text", ""))}

        adapters = {
            "x11": KeyboardAdapter("x11", "x11", {Capability.KEYBOARD}),
            "atspi": ElementAdapter("atspi", "atspi", {Capability.ELEMENTS}),
        }
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11", "atspi"), {})
        router.adapters = adapters
        router.status = {}
        router.probed_at = 0.0
        controller = object.__new__(Controller)
        controller.routers = {"test": router}
        controller.observations = {}
        return controller

    async def test_typing_verification_confirms_the_editable_target(self):
        controller = self._typing_controller(
            {"role": "entry", "name": "Username", "text": "hello there"}
        )

        result = await controller.perform(
            "test", "keyboard.type", {"text": "hello", "verify": True}
        )

        self.assertEqual(result["data"]["verification"]["typed_into"], "entry")

    async def test_typing_verification_catches_text_that_went_nowhere(self):
        # A click that missed lands focus on the document, and every layer
        # still reports the keystrokes as delivered.
        controller = self._typing_controller(
            {"role": "document web", "name": "The Internet", "text": ""}
        )

        with self.assertRaises(ActionError) as raised:
            await controller.perform(
                "test", "keyboard.type", {"text": "into-the-void", "verify": True}
            )

        self.assertIn("did not reach an editable control", str(raised.exception))
        self.assertEqual(raised.exception.details["focused_role"], "document web")

    async def test_window_scoping_explains_itself_on_a_pixel_only_target(self):
        # VNC and RDP report no window metadata; a bare "lacks [windows]" gives
        # the caller nothing to trace back to the locator option that caused it.
        controller = self._layered_controller(
            bounds={"left": 1, "top": 1, "width": 2, "height": 2}, elements=False
        )

        with self.assertRaises(CapabilityError) as raised:
            await controller.locate("test", {"text": "Save", "window": "active"})

        self.assertIn("window scoping", str(raised.exception))
        self.assertIn("region", str(raised.exception))

    async def test_element_point_prefers_the_semantic_layer(self):
        controller = self._layered_controller(
            bounds={"left": 100, "top": 200, "width": 60, "height": 20}
        )

        result = await controller.perform(
            "test", "element.point", {"selector": {"name": "Save"}}
        )

        data = result["data"]
        self.assertEqual(data["tier"], "semantic")
        self.assertEqual(data["source"], "atspi")
        self.assertEqual(data["point"], {"x": 130, "y": 210})
        # The point is validated against the surface that will act on it.
        self.assertEqual(data["surface"]["adapter"], "vnc")

    async def test_element_point_falls_back_to_the_visual_layer(self):
        controller = self._layered_controller(bounds=None, elements=False)

        async def locate(target, locator):
            return {"box": {"left": 10, "top": 20, "width": 40, "height": 10},
                    "text": "Save", "confidence": 92.0}

        controller.locate = locate
        result = await controller.perform(
            "test", "element.point", {"selector": {"text": "Save"}}
        )

        self.assertEqual(result["data"]["tier"], "visual")
        self.assertEqual(result["data"]["point"], {"x": 30, "y": 25})

    async def test_element_point_refuses_a_visual_guess_when_told_to(self):
        controller = self._layered_controller(bounds=None)

        with self.assertRaises(CapabilityError):
            await controller.perform(
                "test", "element.point",
                {"selector": {"name": "Save"}, "allow_fallback": False},
            )

    async def test_element_point_rejects_a_point_off_the_action_surface(self):
        # The side channel describes a desktop larger than the framebuffer that
        # would receive the click; acting on it would land somewhere else.
        controller = self._layered_controller(
            bounds={"left": 1900, "top": 1100, "width": 40, "height": 20},
            surface=(1600, 900),
        )

        with self.assertRaises(SurfaceError) as raised:
            await controller.perform(
                "test", "element.point", {"selector": {"name": "Save"}}
            )

        self.assertIn("outside", str(raised.exception))

    def _clipboard_controller(self, copied):
        """Controller wired to an adapter whose clipboard yields `copied` after a copy chord."""

        class ClipboardAdapter(StubAdapter):
            def __init__(self):
                super().__init__(
                    "x11", "x11", {Capability.CLIPBOARD, Capability.KEYBOARD}
                )
                self.clipboard = "previous contents"
                self.chords = []

            async def act(self, action, params):
                self.chords.append(params["keys"][0])
                if params["keys"][0] == "ctrl+c" and copied is not None:
                    self.clipboard = copied
                return {"keys": params["keys"]}

            async def perform(self, operation, params):
                if operation == "clipboard.read":
                    return {"text": self.clipboard}
                if operation == "clipboard.write":
                    self.clipboard = params["text"]
                    return {"characters": len(params["text"])}
                return await super().perform(operation, params)

        adapter = ClipboardAdapter()
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {})
        router.adapters = {"x11": adapter}
        router.status = {}
        router.probed_at = 0.0
        controller = object.__new__(Controller)
        controller.routers = {"test": router}
        controller.observations = {}
        return controller, adapter

    async def test_copy_returns_the_selection_as_exact_text(self):
        controller, adapter = self._clipboard_controller("yes-usage = yes [STRING]...")

        result = await controller.perform("test", "clipboard.copy", {"select_all": True})

        self.assertEqual(result["data"]["text"], "yes-usage = yes [STRING]...")
        self.assertEqual(result["data"]["characters"], 27)
        self.assertEqual(adapter.chords, ["ctrl+a", "ctrl+c"])

    async def test_copy_that_never_reaches_the_widget_is_an_error(self):
        # `copied` None means the chord goes nowhere, as when focus sits on a
        # widget with no selection; the stale clipboard must not be returned.
        controller, _ = self._clipboard_controller(None)

        with self.assertRaises(ActionError) as raised:
            await controller.perform("test", "clipboard.copy", {"settle_ms": 0})

        self.assertIn("never changed", str(raised.exception))

    async def test_copy_can_restore_the_previous_clipboard(self):
        controller, adapter = self._clipboard_controller("copied value")

        result = await controller.perform("test", "clipboard.copy", {"restore": True})

        self.assertEqual(result["data"]["text"], "copied value")
        self.assertEqual(adapter.clipboard, "previous contents")

    async def test_small_regions_are_captured_wide_enough_to_recognize(self):
        capture = AsyncMock(return_value=Frame(b"crop", 240, 120, "x11"))
        with tempfile.TemporaryDirectory() as directory:
            controller, _ = self._visual_controller(directory, capture)
            controller._ocr = AsyncMock(
                return_value=[
                    # Sits inside the padding, outside the requested region.
                    Word("Next", "next", 96.2, 10, 10, 40, 16, ("1", "1", "1", "1")),
                    # Sits inside the requested region, which starts at (84, 30).
                    Word("Next", "next", 96.2, 100, 40, 40, 16, ("1", "2", "1", "1")),
                ]
            )

            result = await controller.locate(
                "test",
                {"text": "Next", "region": [700, 400, 772, 460], "timeout": 0},
            )

        # 72x60 requested, padded to the 240x120 minimum around the same centre.
        self.assertEqual(capture.await_args_list, [call({"region": [616, 370, 856, 490]})])
        self.assertEqual(result["region"], [700, 400, 772, 460])
        # The padding-only match is rejected; the in-region match rebases to screen.
        self.assertEqual((result["x"], result["y"]), (736, 418))

    async def test_locator_scrolls_until_the_text_comes_into_view(self):
        visual = StubAdapter("x11", "x11", {Capability.CAPTURE, Capability.SCROLL})
        visual.capture = AsyncMock(return_value=Frame(b"screen", 1600, 900, "x11"))
        visual.act = AsyncMock(return_value={"direction": "down", "amount": 4})
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {})
        router.adapters = {"x11": visual}
        router.status = {}
        router.probed_at = 0.0
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(SimpleNamespace(cache_dir=Path(directory)))
            controller.routers = {"test": router}
            # The word only appears once the page has been scrolled twice.
            controller._ocr = AsyncMock(
                side_effect=[
                    [],
                    [],
                    [Word("Username", "username", 96.0, 700, 500, 80, 20,
                          ("1", "1", "1", "1"))],
                ]
            )

            result = await controller.locate(
                "test", {"text": "Username", "scroll": 5, "interval": 0.01}
            )

        self.assertEqual(result["scrolled"], 2)
        self.assertEqual(visual.act.await_count, 2)
        self.assertEqual(visual.act.await_args_list[0].args[0], "scroll")

    async def test_locator_stops_after_the_scroll_budget(self):
        visual = StubAdapter("x11", "x11", {Capability.CAPTURE, Capability.SCROLL})
        visual.capture = AsyncMock(return_value=Frame(b"screen", 1600, 900, "x11"))
        visual.act = AsyncMock(return_value={})
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {})
        router.adapters = {"x11": visual}
        router.status = {}
        router.probed_at = 0.0
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(SimpleNamespace(cache_dir=Path(directory)))
            controller.routers = {"test": router}
            controller._ocr = AsyncMock(return_value=[])

            with self.assertRaises(ActionError):
                await controller.locate(
                    "test", {"text": "Nowhere", "scroll": 3, "interval": 0.01, "timeout": 0}
                )

        self.assertEqual(visual.act.await_count, 3)

    async def test_identical_frames_are_recognized_once(self):
        words = [Word("Next", "next", 96.2, 700, 500, 50, 20, ("1", "1", "1", "1"))]
        with tempfile.TemporaryDirectory() as directory:
            controller, _ = self._visual_controller(directory, AsyncMock())
            frame = Frame(b"identical-png", 1600, 900, "x11")
            with patch(
                "wayweaver.controller.recognize", AsyncMock(return_value=words)
            ) as recognize:
                first = await controller._ocr("test", frame)
                second = await controller._ocr("test", frame)
                other = await controller._ocr(
                    "test", Frame(b"different-png", 1600, 900, "x11")
                )

        self.assertEqual(recognize.await_count, 2)
        self.assertEqual(first, words)
        self.assertEqual(second, words)
        self.assertEqual(other, words)
        self.assertIsNot(first, second)

    async def test_region_locate_crops_ocr_and_rebases_screen_coordinates(self):
        visual = StubAdapter("x11", "x11", {Capability.CAPTURE})
        visual.capture = AsyncMock(return_value=Frame(b"crop", 300, 300, "x11"))
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {})
        router.adapters = {"x11": visual}
        router.status = {}
        router.probed_at = 0.0
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(SimpleNamespace(cache_dir=Path(directory)))
            controller.routers = {"test": router}
            controller._ocr = AsyncMock(
                return_value=[
                    Word(
                        "Next",
                        "next",
                        96.2,
                        20,
                        40,
                        50,
                        20,
                        ("1", "1", "1", "1"),
                    )
                ]
            )

            result = await controller.locate(
                "test",
                {"text": "Next", "region": [600, 400, 900, 700]},
            )

        visual.capture.assert_awaited_once_with({"region": [600, 400, 900, 700]})
        self.assertEqual(result["ocr_scope"], "region")
        self.assertEqual(result["x"], 645)
        self.assertEqual(result["y"], 450)
        self.assertEqual(result["box"]["left"], 620)
        self.assertEqual(result["box"]["top"], 440)

    def _fallback_controller(self, *, semantic_available=True):
        semantic = StubAdapter(
            "atspi", "atspi", {Capability.ELEMENTS},
            available=semantic_available,
            failure="accessible element not found" if semantic_available else None,
        )
        visual = StubAdapter("x11", "x11", {Capability.CAPTURE, Capability.POINTER})
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("atspi", "x11"), {})
        router.adapters = {"atspi": semantic, "x11": visual}
        router.status = {}
        router.probed_at = 0.0
        controller = object.__new__(Controller)
        controller.routers = {"test": router}
        controller.observations = {}

        async def locate(target, locator, click):
            return {"target": target, "text": locator["text"], "clicked": click}

        controller.locate = locate
        return controller

    async def test_allow_fallback_false_refuses_a_silent_visual_downgrade(self):
        # The semantic adapter is present but fails; without the flag this
        # quietly becomes an OCR click.
        controller = self._fallback_controller()

        with self.assertRaises(ActionError):
            await controller.perform(
                "test", "element.activate",
                {"selector": {"name": "Save"}, "allow_fallback": False},
            )

    async def test_allow_fallback_false_refuses_routing_to_a_visual_adapter(self):
        # No semantic adapter at all, so routing itself is a downgrade.
        controller = self._fallback_controller(semantic_available=False)

        with self.assertRaises(CapabilityError) as raised:
            await controller.perform(
                "test", "element.activate",
                {"selector": {"name": "Save"}, "allow_fallback": False},
            )

        self.assertIn("allow_fallback", str(raised.exception))

    async def test_fallback_is_still_the_default(self):
        controller = self._fallback_controller()

        result = await controller.perform(
            "test", "element.activate", {"selector": {"name": "Save"}}
        )

        self.assertTrue(result["backend"]["fallback"])

    async def test_element_failure_retries_through_visual_fallback(self):
        semantic = StubAdapter(
            "atspi",
            "atspi",
            {Capability.ELEMENTS},
            failure="accessible element not found",
        )
        visual = StubAdapter("x11", "x11", {Capability.CAPTURE, Capability.POINTER})
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("atspi", "x11"), {})
        router.adapters = {"atspi": semantic, "x11": visual}
        router.status = {}
        router.probed_at = 0.0
        controller = object.__new__(Controller)
        controller.routers = {"test": router}
        controller.observations = {}

        async def locate(target, locator, click):
            return {"target": target, "text": locator["text"], "clicked": click}

        controller.locate = locate
        result = await controller.perform(
            "test",
            "element.activate",
            {"selector": {"name": "Save"}},
        )

        self.assertTrue(result["backend"]["fallback"])
        self.assertEqual(result["backend"]["adapter"], "x11")
        self.assertEqual(
            result["backend"]["fallback_reason"], "accessible element not found"
        )
        self.assertTrue(result["data"]["clicked"])

    async def test_element_activation_verifies_visual_postconditions(self):
        visual = StubAdapter("x11", "x11", {Capability.CAPTURE, Capability.POINTER})
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {})
        router.adapters = {"x11": visual}
        router.status = {}
        router.probed_at = 0.0
        controller = object.__new__(Controller)
        controller.routers = {"test": router}
        controller.observations = {}
        controller.locate = AsyncMock(return_value={"text": "Next", "clicked": True})
        controller.capture = AsyncMock(
            side_effect=[
                (Frame(b"before", 1600, 900, "x11"), "x11"),
                (Frame(b"after", 1600, 900, "x11"), "x11"),
            ]
        )
        controller._ocr = AsyncMock(return_value=[])

        result = await controller.perform(
            "test",
            "element.activate",
            {
                "selector": {"text": "Next"},
                "verify": {
                    "surface_changed": True,
                    "text_disappeared": "Next",
                    "timeout_ms": 100,
                },
            },
        )

        self.assertEqual(
            result["data"]["verification"],
            {
                "surface_changed": True,
                "text_disappeared": True,
                "attempts": 1,
            },
        )

    async def test_returns_versioned_envelope_and_prepares_selector(self):
        applications = StubAdapter(
            "x11",
            "x11",
            {Capability.APPLICATIONS},
        )
        applications.perform = AsyncMock(
            return_value={"application": {}, "matches": 1, "pid": 123}
        )
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {})
        router.adapters = {"x11": applications}
        router.status = {}
        router.probed_at = 0.0
        controller = object.__new__(Controller)
        controller.routers = {"test": router}
        controller.observations = {}

        result = await controller.perform(
            "test",
            "application.open",
            {"selector": {"name": "Terminal"}},
        )

        self.assertEqual(result["api_version"], API_VERSION)
        self.assertTrue(result["ok"])
        self.assertEqual(result["backend"]["adapter"], "x11")
        applications.perform.assert_awaited_once_with(
            "application.open", {"name": "Terminal"}
        )
        self.assertIn("elapsed_ms", result["timing"])

    async def test_rejects_flat_params_and_stale_surfaces(self):
        pointer = StubAdapter("x11", "x11", {Capability.POINTER})
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {})
        router.adapters = {"x11": pointer}
        router.status = {}
        router.probed_at = 0.0
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(SimpleNamespace(cache_dir=Path(directory)))
            controller.routers = {"test": router}

        with self.assertRaises(ContractError):
            await controller.perform(
                "test",
                "pointer.click",
                {"x": 10, "y": 20},
            )
        with self.assertRaises(SurfaceError):
            await controller.perform(
                "test",
                "pointer.click",
                {
                    "point": {"x": 10, "y": 20},
                    "space": "surface",
                    "surface_id": "stale",
                },
            )

    async def test_observe_returns_coordinate_provenance(self):
        capture = StubAdapter("capture", "x11", {Capability.CAPTURE})
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("capture",), {})
        router.adapters = {"capture": capture}
        router.status = {}
        router.probed_at = 0.0
        with tempfile.TemporaryDirectory() as directory:
            controller = Controller(SimpleNamespace(cache_dir=Path(directory)))
            controller.routers = {"test": router}
            controller.capture = AsyncMock(
                return_value=(Frame(b"png", 1600, 900, "x11"), "capture")
            )
            controller._ocr = AsyncMock(
                return_value=[
                    Word("Next", "next", 96.2, 700, 500, 50, 20, ("1", "1", "1", "1"))
                ]
            )

            result = await controller.observe("test", True)

        self.assertEqual(result["surface"]["space"], "screen")
        self.assertEqual(result["surface"]["width"], 1600)
        self.assertEqual(result["surface"]["adapter"], "capture")
        self.assertEqual(
            controller.observations["test"]["observation_id"],
            result["observation_id"],
        )
        self.assertEqual(result["ocr"], "Next")
        self.assertEqual(
            result["ocr_items"],
            [
                {
                    "text": "Next",
                    "confidence": 96.2,
                    "point": {"x": 725, "y": 510},
                    "box": {
                        "left": 700,
                        "top": 500,
                        "width": 50,
                        "height": 20,
                    },
                }
            ],
        )

    async def test_provenance_survives_processes_and_rejects_replaced_sessions(self):
        class PointerAdapter(StubAdapter):
            async def perform(self, operation, params):
                return {"x": params["x"], "y": params["y"], "action": "move"}

        capture = PointerAdapter(
            "desktop",
            "x11",
            {Capability.CAPTURE, Capability.POINTER},
        )
        capture.config["session_id"] = "session-one"
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("desktop",), {})
        router.adapters = {"desktop": capture}
        router.status = {}
        router.probed_at = 0.0
        with tempfile.TemporaryDirectory() as directory:
            config = SimpleNamespace(cache_dir=Path(directory))
            first = Controller(config)
            first.routers = {"test": router}
            first.capture = AsyncMock(
                return_value=(Frame(b"png", 1600, 900, "x11"), "desktop")
            )
            observation = await first.observe("test")

            second = Controller(config)
            second.routers = {"test": router}
            moved = await second.perform(
                "test",
                "pointer.move",
                {
                    "point": {"x": 10, "y": 20},
                    "space": "surface",
                    "surface_id": observation["surface"]["id"],
                    "observation_id": observation["observation_id"],
                },
            )
            capture.config["session_id"] = "session-two"
            with self.assertRaisesRegex(SurfaceError, "replaced desktop session"):
                await second.perform(
                    "test",
                    "pointer.move",
                    {
                        "point": {"x": 10, "y": 20},
                        "space": "surface",
                        "surface_id": observation["surface"]["id"],
                        "observation_id": observation["observation_id"],
                    },
                )

        self.assertEqual(moved["data"]["x"], 10)


class OperationTests(unittest.TestCase):
    def test_registry_separates_semantic_visual_and_fallback_operations(self):
        self.assertEqual(OPERATIONS["application.open"].tier, "semantic")
        self.assertEqual(OPERATIONS["pointer.click"].tier, "visual")
        self.assertEqual(
            OPERATIONS["element.activate"].fallback,
            frozenset({Capability.CAPTURE, Capability.POINTER}),
        )
        self.assertEqual(OPERATIONS["element.wait"].tier, "semantic")
        self.assertFalse(OPERATIONS["element.wait"].fallback)

    def test_canonical_compact_steps_are_normalized(self):
        self.assertEqual(
            normalize({"application.open": "Terminal"}),
            ("application.open", {"selector": {"name": "Terminal"}}),
        )
        self.assertEqual(
            normalize({"window.focus": "Terminal"}),
            ("window.focus", {"selector": {"title": "Terminal"}}),
        )
        self.assertEqual(
            normalize({"pointer.click": [12, 34]}),
            (
                "pointer.click",
                {"point": {"x": 12, "y": 34}, "space": "screen"},
            ),
        )
        self.assertEqual(
            normalize({"element.activate": "Save"}),
            ("element.activate", {"selector": {"name": "Save"}}),
        )
        self.assertEqual(
            normalize({"element.wait": "Save"}),
            ("element.wait", {"selector": {"name": "Save"}}),
        )
        self.assertEqual(
            normalize({"window.fullscreen": "Chrome"}),
            ("window.fullscreen", {"selector": {"title": "Chrome"}}),
        )

    def test_error_payload_uses_stable_machine_fields(self):
        payload = error_payload(
            ContractError("bad params", details={"operation": "pointer.click"})
        )

        self.assertEqual(payload["code"], "INVALID_PARAMS")
        self.assertFalse(payload["retryable"])
        self.assertEqual(payload["details"]["operation"], "pointer.click")

    def test_selectors_require_identity_and_reject_conflicting_text_modes(self):
        schema = OPERATIONS["element.find"].params_schema

        with self.assertRaises(ContractError):
            validate_params("element.find", schema, {"selector": {}})
        with self.assertRaises(ContractError):
            validate_params(
                "element.find",
                schema,
                {
                    "selector": {
                        "text": "Save",
                        "exact": True,
                        "contains": True,
                    }
                },
            )


class FakeToolSSH:
    def __init__(self, tools):
        self.tools = tools
        self.commands = []

    async def available(self):
        return True, None

    async def shell(self, command, stdin=None):
        self.commands.append((command, stdin))
        return 0, ("\n".join(self.tools) + "\n").encode(), b""


class ProbeResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_raising_adapter_does_not_break_the_others(self):
        class Exploding(StubAdapter):
            async def available(self):
                raise RuntimeError("transport blew up")

        healthy = StubAdapter("x11", "x11", {Capability.CAPTURE})
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11", "boom"), {})
        router.adapters = {
            "boom": Exploding("boom", "boom", {Capability.POINTER}),
            "x11": healthy,
        }
        router.status = {}
        router.probed_at = 0.0

        status = await router.probe()

        self.assertFalse(status["boom"].available)
        self.assertIn("RuntimeError", status["boom"].reason)
        # The healthy adapter must still be routable.
        self.assertTrue(status["x11"].available)
        self.assertIs(await router.select(Capability.CAPTURE), healthy)

    async def test_raw_routes_respect_the_probe_ttl(self):
        adapter = StubAdapter("x11", "x11", {Capability.CAPTURE}, raw={"poke": "Poke"})
        adapter.available = AsyncMock(return_value=(True, None))
        router = object.__new__(Router)
        router.target = TargetConfig("test", ("x11",), {}, probe_ttl=30)
        router.adapters = {"x11": adapter}
        router.status = {}
        router.probed_at = 0.0

        await router.operation_routes(include_raw=True)
        router.probed_at = time.monotonic()
        await router.operation_routes(include_raw=True)

        # Stale status used to be refreshed only when it was entirely absent.
        self.assertEqual(adapter.available.await_count, 1)


class ProvenanceKeyTests(unittest.TestCase):
    def test_the_signing_key_appears_atomically(self):
        from wayweaver.provenance import ProvenanceStore

        with tempfile.TemporaryDirectory() as directory:
            store = ProvenanceStore(Path(directory))
            key = store._key()
            path = Path(directory) / ".provenance-key"

            self.assertEqual(len(key), 32)
            self.assertEqual(len(path.read_bytes()), 32)
            # No partially written temporaries left behind for a second
            # process to read as an invalid key.
            leftovers = [p.name for p in Path(directory).iterdir() if p.name != ".provenance-key"]
            self.assertEqual(leftovers, [])

    def test_a_second_store_reuses_the_existing_key(self):
        from wayweaver.provenance import ProvenanceStore

        with tempfile.TemporaryDirectory() as directory:
            first = ProvenanceStore(Path(directory))._key()
            second = ProvenanceStore(Path(directory))._key()

        self.assertEqual(first, second)


class CliArgumentTests(unittest.TestCase):
    def test_params_are_not_read_from_the_filesystem(self):
        from wayweaver.cli import load_json

        with tempfile.TemporaryDirectory() as directory:
            decoy = Path(directory) / "decoy.json"
            decoy.write_text('{"from": "disk"}')
            with self.assertRaises(json.JSONDecodeError):
                load_json(str(decoy))
            self.assertEqual(load_json(str(decoy), allow_path=True), {"from": "disk"})

    def test_a_value_containing_a_null_byte_is_a_json_error(self):
        from wayweaver.cli import load_json

        # Path() raises ValueError on an embedded NUL, which used to escape.
        with self.assertRaises(json.JSONDecodeError):
            load_json("not json\x00", allow_path=True)


class ToolProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_missing_optional_tool_does_not_fail_the_probe(self):
        # A shell loop exits with its last iteration's status, so `command -v
        # <tool> && printf` made the whole adapter report unavailable whenever
        # the last tool in the list was absent -- which is exactly the case the
        # capability detection below is written to handle.
        from wayweaver.adapters.wayland import WaylandAdapter
        from wayweaver.adapters.x11 import X11Adapter

        adapters = {
            "x11": X11Adapter("x11", {"display": ":1"}, FakeToolSSH([])),
            "wayland": WaylandAdapter("wayland", {"display": "wayland-1"}, FakeToolSSH([])),
        }
        for name, adapter in adapters.items():
            await adapter.available()
            probe = next(
                command for command, _ in adapter.transport.commands
                if "for tool in" in command
            )
            with self.subTest(adapter=name):
                self.assertIn('if command -v "$tool"', probe)
                self.assertIn("fi; done", probe)
                self.assertNotIn('command -v "$tool" >/dev/null && printf', probe)


class WaylandTests(unittest.IsolatedAsyncioTestCase):
    async def test_canonical_buttons_map_onto_ydotool_numbering(self):
        # The contract numbers buttons 1=left, 2=middle, 3=right; ydotool uses
        # 0=left, 1=right, 2=middle. Adding the contract value to 0xC0 silently
        # turned a left click into a right click.
        for canonical, expected in ((1, "0xc0"), (2, "0xc2"), (3, "0xc1")):
            ssh = FakeToolSSH(["ydotool", "wtype"])
            adapter = WaylandAdapter("wayland", {"display": "wayland-1"}, ssh)
            adapter.pointer = (0, 0)

            await adapter.act("click", {"x": 10, "y": 10, "button": canonical})

            self.assertIn(
                f"ydotool click {expected}",
                ssh.commands[-1][0],
                f"canonical button {canonical} emitted the wrong ydotool code",
            )

    async def test_right_click_action_uses_the_ydotool_right_button(self):
        ssh = FakeToolSSH(["ydotool", "wtype"])
        adapter = WaylandAdapter("wayland", {"display": "wayland-1"}, ssh)
        adapter.pointer = (0, 0)

        await adapter.act("right_click", {"x": 10, "y": 10})

        self.assertIn("ydotool click 0xc1", ssh.commands[-1][0])

    async def test_capabilities_and_raw_tools_follow_live_session(self):
        ssh = FakeToolSSH(
            ["wayweaver-applications", "wl-copy", "wl-paste", "wtype", "ydotool"]
        )
        adapter = WaylandAdapter("wayland", {"display": "wayland-1"}, ssh)

        available, reason = await adapter.available()

        self.assertTrue(available)
        self.assertIsNone(reason)
        self.assertEqual(
            adapter.capabilities,
            frozenset(
                {
                    Capability.APPLICATIONS,
                    Capability.CLIPBOARD,
                    Capability.KEYBOARD,
                    Capability.POINTER,
                    # ydotool's wheel mode carries scrolling, so pointer and
                    # scroll arrive together.
                    Capability.SCROLL,
                }
            ),
        )
        self.assertEqual(
            adapter.raw_operations,
            {
                "wtype": "Call wtype with an argument list",
                "ydotool": "Call ydotool with an argument list",
            },
        )


class FakeRFB:
    width = 1600
    height = 900

    def __init__(self):
        self.pointers = []

    def pointer(self, x, y, buttons=0):
        self.pointers.append((x, y, buttons))

    def close(self):
        pass


class PointerTakeoverTests(unittest.TestCase):
    def test_vnc_reanchors_absolute_pointer_before_trajectory(self):
        adapter = VNCAdapter("vnc", {"host": "example", "port": 5900})
        adapter.pointer = (800, 450)
        connection = FakeRFB()
        adapter._connect = lambda: connection

        result = adapter._act_sync("click", {"x": 900, "y": 500, "duration_ms": 0})

        self.assertEqual(connection.pointers[0], (800, 450, 0))
        self.assertEqual(connection.pointers[-2:], [(900, 500, 1), (900, 500, 0)])
        self.assertEqual(result["x"], 900)
        self.assertEqual(result["y"], 500)


if __name__ == "__main__":
    unittest.main()
