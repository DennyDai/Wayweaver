import json
import os
import random
import tempfile
import unittest
from pathlib import Path

from wayweaver.adapters.x11 import X11Adapter
from wayweaver.cli import load_json
from wayweaver.errors import ActionError, ContractError
from wayweaver.config import ConfigError, load_config
from wayweaver.motion import trajectory
from wayweaver.sequence import normalize, run_sequence
from wayweaver.vision import Word, find_text, ocr_items


class CliTests(unittest.TestCase):
    def test_long_inline_json_is_not_treated_as_a_path(self):
        payload = {"steps": [{"type": "x" * 300}]}
        self.assertEqual(load_json(json.dumps(payload)), payload)


class ConfigTests(unittest.TestCase):
    def test_loads_targets_and_expands_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.toml"
            path.write_text('[targets.vm.ssh]\nhost="${TEST_HOST}"\n')
            os.environ["TEST_HOST"] = "example.test"
            config = load_config(path)
            self.assertEqual(
                config.target("vm").adapters["ssh"]["host"], "example.test"
            )

    def test_configures_target_probe_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.toml"
            path.write_text(
                '[targets.vm]\nprobe_ttl=45\n[targets.vm.ssh]\nhost="example.test"\n'
            )
            config = load_config(path)

        self.assertEqual(config.target("vm").probe_ttl, 45)

    def test_missing_environment_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.toml"
            path.write_text('[targets.vm.ssh]\nhost="${WAYWEAVER_MISSING_TEST}"\n')
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_rejects_scalar_adapter_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "targets.toml"
            path.write_text(
                '[targets.vm]\ntypo="not-an-adapter-table"\n[targets.vm.ssh]\nhost="localhost"\n'
            )
            with self.assertRaises(ConfigError):
                load_config(path)


class MotionTests(unittest.TestCase):
    def test_trajectory_ends_exactly_at_target(self):
        points, duration = trajectory((10, 20), (500, 400), rng=random.Random(7))
        self.assertGreater(duration, 0)
        self.assertGreater(len(points), 8)
        self.assertEqual(points[-1], (500, 400))
        self.assertGreater(len(set(points)), 8)


class VisionTests(unittest.TestCase):
    def word(self, text, token, left, top, line="1"):
        return Word(text, token, 95, left, top, 50, 20, ("1", "1", "1", line))

    def test_region_disambiguates_visible_text(self):
        words = [
            self.word("Target", "target", 10, 10),
            self.word("Target", "target", 700, 500, "2"),
        ]
        match = find_text(words, {"text": "target", "region": [600, 400, 900, 700]})
        self.assertEqual(match["x"], 725)
        self.assertEqual(match["matches"], 1)
        self.assertEqual(match["total_matches"], 2)

    def test_fuzzy_region_recovers_minor_ocr_error(self):
        words = [
            self.word("ferminal", "ferminal", 20, 40),
            self.word("Emulator", "emulator", 80, 40),
            self.word("Terminal", "terminal", 700, 500, "2"),
            self.word("Emulator", "emulator", 760, 500, "2"),
        ]
        match = find_text(
            words,
            {
                "text": "Terminal Emulator",
                "region": [0, 0, 220, 180],
                "fuzzy": True,
            },
        )
        self.assertEqual(match["match"], "fuzzy")
        self.assertEqual(match["matches"], 1)
        self.assertEqual(match["total_matches"], 2)

    def test_matches_across_recognizer_word_splits(self):
        # The same button read as "Don't" in one crop and "Dont" in another;
        # token-for-token matching could never satisfy both spellings.
        apostrophe = [
            Word("Don't", "don", 95, 100, 50, 30, 20, ("1", "1", "1", "1")),
            Word("Don't", "t", 95, 100, 50, 30, 20, ("1", "1", "1", "1")),
            self.word("Save", "save", 140, 50),
        ]
        plain = [self.word("Dont", "dont", 100, 50), self.word("Save", "save", 140, 50)]

        for label, words in (("apostrophe", apostrophe), ("plain", plain)):
            for probe in ("Don't Save", "Dont Save"):
                with self.subTest(words=label, probe=probe):
                    match = find_text(words, {"text": probe})
                    self.assertEqual(match["box"]["left"], 100)

    def test_joined_matching_still_honours_line_boundaries(self):
        words = [
            self.word("Dont", "dont", 100, 50, line="1"),
            self.word("Save", "save", 100, 90, line="2"),
        ]

        with self.assertRaises(ActionError):
            find_text(words, {"text": "Dont Save"})

    def test_strict_locator_refuses_to_guess_between_matches(self):
        words = [self.word("Register", "register", 286, 115, "1"),
                 self.word("Register", "register", 752, 710, "2")]

        with self.assertRaises(ActionError) as raised:
            find_text(words, {"text": "Register", "strict": True})

        self.assertIn("ambiguous", str(raised.exception))
        self.assertEqual(raised.exception.details["matches"], 2)

    def test_strict_locator_accepts_an_explicit_nth(self):
        words = [self.word("Register", "register", 286, 115, "1"),
                 self.word("Register", "register", 752, 710, "2")]

        match = find_text(words, {"text": "Register", "strict": True, "nth": 1})

        self.assertEqual((match["x"], match["y"]), (777, 720))

    def test_ocr_items_expose_word_coordinates_without_token_duplicates(self):
        words = [
            Word("Open Project", "open", 94.4, 100, 200, 120, 30, ("1", "1", "1", "1")),
            Word(
                "Open Project",
                "project",
                94.4,
                100,
                200,
                120,
                30,
                ("1", "1", "1", "1"),
            ),
            self.word("Next", "next", 700, 500, "2"),
        ]

        items = ocr_items(words)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["text"], "Open Project")
        self.assertEqual(items[0]["point"], {"x": 160, "y": 215})
        self.assertEqual(
            items[0]["box"],
            {"left": 100, "top": 200, "width": 120, "height": 30},
        )


class FakeSSH:
    def __init__(self):
        self.commands = []

    async def shell(self, command, stdin=None):
        self.commands.append(command)
        if "wayweaver-applications open" in command:
            return (
                0,
                json.dumps(
                    {
                        "application": {
                            "id": "xfce4-terminal.desktop",
                            "name": "Xfce Terminal",
                        },
                        "matches": 1,
                        "pid": 123,
                    }
                ).encode(),
                b"",
            )
        if "wmctrl -lpGx" in command:
            return (
                0,
                b"0x01000003 0 123 10 20 800 600 xfce4-terminal.Xfce4-terminal desktop-vm Terminal - vm@desktop-vm\\n",
                b"",
            )
        if "wmctrl -d" in command:
            return (
                0,
                b"0 * DG: 1600x900 VP: 0,0 WA: 0,0 1600x900 Main\n"
                b"1 - DG: 1600x900 VP: 0,0 WA: 0,0 1600x900 Work\n",
                b"",
            )
        if "xdotool getmouselocation --shell" in command:
            return 0, b"X=700\nY=400\nSCREEN=0\nWINDOW=1\n", b""
        if "xdotool getactivewindow" in command:
            return 0, str(int("0x01000003", 16)).encode(), b""
        return 0, b"", b""


class X11Tests(unittest.IsolatedAsyncioTestCase):
    async def test_opens_catalog_application_without_visual_search(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)
        result = await adapter.perform(
            "application.open",
            {"id": "xfce4-terminal.desktop"},
        )
        self.assertEqual(result["application"]["name"], "Xfce Terminal")
        self.assertIn("wayweaver-applications open", ssh.commands[-1])

    async def test_reads_live_pointer_before_every_trajectory(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)
        await adapter.act("move", {"x": 710, "y": 410, "duration_ms": 0})
        self.assertIn("xdotool getmouselocation --shell", ssh.commands[-2])
        self.assertIn("xdotool mousemove 710 410", ssh.commands[-1])

    async def test_focuses_window_by_title(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)
        result = await adapter.act("focus_window", {"title": "terminal"})
        self.assertTrue(result["window"]["active"])
        self.assertIn("wmctrl -ia 0x01000003", ssh.commands[-1])

    async def test_asserts_window_by_class_and_active_state(self):
        adapter = X11Adapter("x11", {"display": ":1"}, FakeSSH())
        result = await adapter.act(
            "assert_window",
            {"class": "xfce4-terminal", "active": True},
        )
        self.assertEqual(result["id"], "0x01000003")
        self.assertEqual(result["matches"], 1)

    async def test_optional_close_skips_missing_window(self):
        adapter = X11Adapter("x11", {"display": ":1"}, FakeSSH())
        result = await adapter.act(
            "close_window", {"title": "Missing", "if_exists": True}
        )
        self.assertTrue(result["skipped"])

    async def test_switches_workspace_by_name(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)

        result = await adapter.perform("workspace.switch", {"name": "Work"})

        self.assertEqual(result, {"index": 1})
        self.assertIn("wmctrl -s 1", ssh.commands[-1])

    async def test_emits_one_x11_keyspec_for_keyboard_chords(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)

        result = await adapter.act("key", {"keys": ["ctrl+A"]})

        self.assertEqual(result, {"keys": ["ctrl+A"]})
        self.assertIn("xdotool key --clearmodifiers ctrl+a", ssh.commands[-1])

    async def test_window_wait_returns_once_a_window_is_absent(self):
        adapter = X11Adapter("x11", {"display": ":1"}, FakeSSH())

        result = await adapter.act(
            "wait_window", {"title": "Nonexistent", "absent": True, "timeout": 1}
        )

        self.assertEqual(result, {"absent": True})

    async def test_window_wait_absent_fails_while_the_window_remains(self):
        adapter = X11Adapter("x11", {"display": ":1"}, FakeSSH())

        with self.assertRaises(ActionError) as raised:
            await adapter.act(
                "wait_window",
                {"title": "terminal", "absent": True, "timeout": 0, "interval": 0.05},
            )

        self.assertIn("still present", str(raised.exception))

    async def test_unpositioned_scroll_targets_the_active_window(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)

        await adapter.act("scroll", {"direction": "down", "amount": 2})

        # wmctrl reports the active window at 10,20 sized 800x600.
        self.assertIn("xdotool mousemove 410 320", ssh.commands[-1])
        self.assertIn("xdotool click --repeat 2", ssh.commands[-1])

    async def test_positioned_scroll_uses_the_given_point(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)

        await adapter.act("scroll", {"direction": "down", "x": 760, "y": 600})

        self.assertIn("xdotool mousemove 760 600", ssh.commands[-1])

    async def test_types_text_that_begins_with_a_dash(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)

        result = await adapter.act("type", {"text": "-usage"})

        self.assertEqual(result, {"characters": 6})
        self.assertIn("-- -usage", ssh.commands[-1])

    async def test_preserves_keysym_case_for_named_keys_in_chords(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)

        for keys, expected in (
            (["ctrl+Return"], "ctrl+Return"),
            (["alt+F4"], "alt+F4"),
            (["super+Left"], "super+Left"),
            (["ctrl", "BackSpace"], "ctrl+BackSpace"),
            (["ctrl+shift+Tab"], "ctrl+shift+Tab"),
        ):
            with self.subTest(keys=keys):
                await adapter.act("key", {"keys": keys})
                self.assertIn(
                    f"xdotool key --clearmodifiers {expected}", ssh.commands[-1]
                )

    async def test_single_key_press_keeps_its_canonical_keysym(self):
        ssh = FakeSSH()
        adapter = X11Adapter("x11", {"display": ":1"}, ssh)

        await adapter.act("key", {"keys": ["Return"]})

        self.assertIn("xdotool key --clearmodifiers Return", ssh.commands[-1])

    async def test_unresolvable_keysym_fails_instead_of_reporting_success(self):
        class RejectingSSH(FakeSSH):
            async def shell(self, command, stdin=None):
                await super().shell(command, stdin)
                return 0, b"", b"(symbol) No such key name 'return'. Ignoring it.\n"

        adapter = X11Adapter("x11", {"display": ":1"}, RejectingSSH())

        with self.assertRaises(ActionError) as raised:
            await adapter.act("key", {"keys": ["ctrl+return"]})

        self.assertIn("ctrl+return", str(raised.exception))
        self.assertEqual(raised.exception.details["key_spec"], "ctrl+return")

    def test_recorder_prefers_accessible_elements_and_preserves_fallbacks(self):
        output = """BUTTON_DOWN 1 1145 716 1200
BUTTON_UP 1 1145 716 1234
BUTTON_DOWN 3 20 30 1250
BUTTON_UP 3 20 30 1300
BUTTON_DOWN 4 30 40 1320
BUTTON_UP 4 30 40 1330
BUTTON_DOWN 1 10 10 1350
BUTTON_UP 1 80 90 1450
KEY_DOWN 37 Control_L 1500
KEY_DOWN 38 a 1510
KEY_UP 38 a 1550
KEY_UP 37 Control_L 1560
"""
        events = X11Adapter._recorded_events(output)
        elements = [
            {
                "name": "",
                "role": "panel",
                "bounds": {
                    "left": 1050,
                    "top": 700,
                    "width": 200,
                    "height": 40,
                },
            },
            {
                "name": "Continue without Signing In",
                "role": "push button",
                "resource_id": "continue",
                "bounds": {
                    "left": 1050,
                    "top": 700,
                    "width": 200,
                    "height": 40,
                },
            },
        ]

        element = X11Adapter._element_at(elements, events[0]["x"], events[0]["y"])
        recorded, semantic = X11Adapter._recording_step(events[0], element)
        _, coordinate = X11Adapter._recording_step(events[1], None)
        _, scroll = X11Adapter._recording_step(events[2], None)
        _, drag = X11Adapter._recording_step(events[3], None)
        _, chord = X11Adapter._recording_step(events[4], None)

        self.assertTrue(recorded["semantic"])
        self.assertEqual(semantic["operation"], "element.activate")
        self.assertEqual(semantic["params"]["selector"]["resource_id"], "continue")
        self.assertEqual(coordinate["operation"], "pointer.click")
        self.assertEqual(coordinate["params"]["button"], "right")
        self.assertEqual(scroll["operation"], "pointer.scroll")
        self.assertEqual(scroll["params"]["direction"], "up")
        self.assertEqual(drag["operation"], "pointer.drag")
        self.assertEqual(drag["params"]["to"], {"x": 80, "y": 90})
        self.assertEqual(chord["operation"], "keyboard.chord")
        self.assertEqual(chord["params"]["keys"], ["CTRL", "a"])


class FakeController:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []

    async def perform(self, target, action, params):
        self.calls.append((target, action, params))
        if action == self.fail:
            raise RuntimeError("expected failure")
        return {"action": action}

    async def observe(self, target, ocr):
        return {"target": target, "ocr": "failure state"}


class WorkflowController:
    def __init__(self):
        self.calls = []
        self.pointer_failures = 1

    async def perform(self, target, operation, params):
        self.calls.append((target, operation, params))
        if operation == "pointer.move" and self.pointer_failures:
            self.pointer_failures -= 1
            raise ActionError("transient")
        data = (
            {
                "observation_id": "obs-1",
                "surface": {"id": "surface-1"},
            }
            if operation == "screen.observe"
            else params
        )
        return {"ok": True, "operation": operation, "data": data}

    async def observe(self, target, ocr):
        return {"target": target}


class SequenceTests(unittest.IsolatedAsyncioTestCase):
    def test_normalizes_explicit_and_compact_steps(self):
        self.assertEqual(
            normalize(
                {
                    "operation": "pointer.click",
                    "params": {
                        "point": {"x": 1, "y": 2},
                        "space": "screen",
                    },
                }
            ),
            ("pointer.click", {"point": {"x": 1, "y": 2}, "space": "screen"}),
        )
        self.assertEqual(
            normalize({"pointer.scroll": "down"}),
            ("pointer.scroll", {"direction": "down", "space": "screen"}),
        )
        self.assertEqual(
            normalize({"application.open": "Xfce Terminal"}),
            ("application.open", {"selector": {"name": "Xfce Terminal"}}),
        )
        self.assertEqual(
            normalize({"window.wait": "Terminal"}),
            ("window.wait", {"selector": {"title": "Terminal"}}),
        )
        with self.assertRaisesRegex(ValueError, "unknown operation"):
            normalize({"click": [1, 2]})

    async def test_runs_steps_without_agent_round_trips(self):
        controller = FakeController()
        result = await run_sequence(
            controller,
            "vm",
            [
                {"pointer.click": [10, 20]},
                {"keyboard.type": "hello"},
                {"keyboard.press": "ENTER"},
            ],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            [call[1] for call in controller.calls],
            ["pointer.click", "keyboard.type", "keyboard.press"],
        )

    async def test_failure_returns_completed_steps_and_observation(self):
        controller = FakeController("keyboard.type")
        result = await run_sequence(
            controller,
            "vm",
            [
                {"pointer.click": [10, 20]},
                {"keyboard.type": "hello"},
                {"keyboard.press": "ENTER"},
            ],
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["failed_step"], 1)
        self.assertEqual(len(result["completed"]), 1)
        self.assertEqual(result["observation"]["ocr"], "failure state")

    async def test_resolves_saved_values_retries_repeats_and_conditions(self):
        controller = WorkflowController()
        result = await run_sequence(
            controller,
            "vm",
            [
                {
                    "id": "observe",
                    "operation": "screen.observe",
                    "params": {},
                    "save_as": "observation",
                },
                {
                    "id": "move",
                    "operation": "pointer.move",
                    "params": {
                        "point": {"x": 10, "y": 20},
                        "space": "surface",
                        "surface_id": {"$ref": "observation.data.surface.id"},
                        "observation_id": {"$ref": "observation.data.observation_id"},
                    },
                    "retry": {"max_attempts": 2, "backoff_ms": 0},
                    "repeat": 2,
                    "save_as": "moves",
                },
                {
                    "operation": "keyboard.press",
                    "params": {"key": "ENTER"},
                    "when": {"ref": "moves.0.ok", "equals": True},
                },
                {
                    "operation": "time.sleep",
                    "params": {"duration_ms": 1},
                    "when": {"ref": "moves.0.ok", "equals": False},
                },
            ],
        )

        self.assertTrue(result["ok"])
        pointer_calls = [
            params
            for _, operation, params in controller.calls
            if operation == "pointer.move"
        ]
        self.assertEqual(len(pointer_calls), 3)
        self.assertEqual(pointer_calls[0]["surface_id"], "surface-1")
        self.assertEqual(result["completed"][1]["attempts"], 2)
        self.assertEqual(result["completed"][-1]["skipped"], True)
        self.assertEqual(len(result["saved"]["moves"]), 2)

    async def test_retry_skips_non_retryable_and_filtered_errors(self):
        class FailingController:
            def __init__(self, error):
                self.error = error
                self.calls = 0

            async def perform(self, target, operation, params):
                self.calls += 1
                raise self.error

            async def observe(self, target, ocr):
                return {"target": target}

        invalid = FailingController(ContractError("bad params"))
        invalid_result = await run_sequence(
            invalid,
            "vm",
            [
                {
                    "operation": "keyboard.press",
                    "params": {"key": "ENTER"},
                    "retry": {"max_attempts": 3},
                }
            ],
            on_error="stop",
        )
        filtered = FailingController(ActionError("transient"))
        filtered_result = await run_sequence(
            filtered,
            "vm",
            [
                {
                    "operation": "keyboard.press",
                    "params": {"key": "ENTER"},
                    "retry": {
                        "max_attempts": 3,
                        "on_codes": ["PROTOCOL_ERROR"],
                    },
                }
            ],
            on_error="stop",
        )

        self.assertFalse(invalid_result["ok"])
        self.assertEqual(invalid.calls, 1)
        self.assertEqual(invalid_result["failure"]["attempts"], 1)
        self.assertFalse(filtered_result["ok"])
        self.assertEqual(filtered.calls, 1)

    async def test_bounds_saved_results_without_breaking_internal_references(self):
        controller = WorkflowController()
        result = await run_sequence(
            controller,
            "vm",
            [
                {
                    "operation": "keyboard.type",
                    "params": {"text": "x" * 10_000},
                    "save_as": "large",
                },
                {
                    "operation": "keyboard.press",
                    "params": {"key": "ENTER"},
                    "when": {"ref": "large.ok", "equals": True},
                },
            ],
            saved_output_limit=256,
        )

        self.assertTrue(result["ok"])
        self.assertTrue(result["saved"]["large"]["truncated"])
        self.assertGreater(result["saved"]["large"]["bytes"], 10_000)
        self.assertEqual(len(controller.calls), 2)


if __name__ == "__main__":
    unittest.main()


class TextOutputTests(unittest.TestCase):
    """Recognized text must survive being assembled into lines.

    Comparing each word only against the previous one collapsed genuine
    repetition, silently rewriting what the caller reads off the screen.
    """

    @staticmethod
    def word(text, left, line=("1", "1", "1", "1")):
        from wayweaver.vision import Word

        return Word(
            text=text,
            token=text.casefold(),
            confidence=90.0,
            left=left,
            top=0,
            width=20,
            height=10,
            line=line,
        )

    def test_repeated_words_are_preserved(self):
        from wayweaver.vision import text_output

        words = [
            self.word(text, index * 30)
            for index, text in enumerate(["Total", "1", "1", "1", "done"])
        ]
        self.assertEqual(text_output(words), "Total 1 1 1 done")

    def test_a_word_emitted_twice_at_one_place_is_collapsed(self):
        from wayweaver.vision import text_output

        duplicate = self.word("OK", 5)
        self.assertEqual(text_output([duplicate, duplicate, duplicate]), "OK")

    def test_lines_stay_separate(self):
        from wayweaver.vision import text_output

        words = [
            self.word("one", 0, ("1", "1", "1", "1")),
            self.word("two", 0, ("1", "1", "2", "1")),
        ]
        self.assertEqual(text_output(words), "one\ntwo")


class PngSizeTests(unittest.TestCase):
    def test_a_truncated_capture_is_reported_as_a_bad_capture(self):
        from wayweaver.errors import ProtocolError
        from wayweaver.image import png_size

        # A partial read can carry a valid signature and stop before the
        # dimensions, which unpacking reported as a struct error.
        truncated = b"\x89PNG\r\n\x1a\n" + b"\x00" * 4 + b"IHDR" + b"\x00\x00"
        with self.assertRaises(ProtocolError):
            png_size(truncated)

    def test_a_real_png_header_still_reads(self):
        from wayweaver.image import encode_png, png_size

        self.assertEqual(png_size(encode_png(3, 2, b"\x00" * 18)), (3, 2))


class WindowGeometryTests(unittest.IsolatedAsyncioTestCase):
    """Window coordinates must describe the client area.

    wmctrl reports a window's position with its frame extents added on top of
    the client area's own coordinates, so a decorated window reads as starting
    below where it actually does. Measured against xwininfo on the reference
    desktop: wmctrl said y=75 for a window whose client area begins at y=51,
    with a 24px frame -- exactly the height of an application's menu bar, so
    anything scoped to the window rectangle excluded the menu bar.
    """

    LISTING = (
        b"0x01c00339 0 238859 0 75 1600 849 ghidra.Ghidra host CodeBrowser\n"
        b"0x02200007 0 166487 10 85 640 480 thunar.Thunar host vm - Thunar\n"
    )

    class GeometrySSH:
        def __init__(self, frames=b""):
            self.frames = frames
            self.commands = []

        async def shell(self, command, stdin=None):
            self.commands.append(command)
            if "wmctrl -lpGx" in command:
                return 0, WindowGeometryTests.LISTING + b"---\n" + self.frames, b""
            if "xdotool getactivewindow" in command:
                return 0, b"", b""
            return 0, b"", b""

    def adapter(self, frames=b""):
        from wayweaver.adapters.x11 import X11Adapter

        return X11Adapter("x11", {"display": ":1"}, self.GeometrySSH(frames))

    async def test_frame_extents_are_subtracted(self):
        adapter = self.adapter(
            b"0x01c00339 0,0,24,0\n0x02200007 5,5,29,5\n"
        )
        windows = {item["title"]: item for item in await adapter.windows()}
        self.assertEqual((windows["CodeBrowser"]["x"], windows["CodeBrowser"]["y"]),
                         (0, 51))
        self.assertEqual((windows["vm - Thunar"]["x"], windows["vm - Thunar"]["y"]),
                         (5, 56))

    async def test_a_window_without_extents_keeps_its_coordinates(self):
        adapter = self.adapter(b"0x01c00339 0,0,24,0\n")
        windows = {item["title"]: item for item in await adapter.windows()}
        self.assertEqual(windows["vm - Thunar"]["y"], 85)

    async def test_missing_xprop_degrades_instead_of_failing(self):
        # xprop is optional, so a desktop without it still lists windows.
        adapter = self.adapter(b"")
        windows = {item["title"]: item for item in await adapter.windows()}
        self.assertEqual(windows["CodeBrowser"]["y"], 75)
        self.assertEqual(len(windows), 2)

    def test_extent_parsing_ignores_malformed_lines(self):
        from wayweaver.adapters.x11 import X11Adapter

        parsed = X11Adapter._frame_extents(
            "0xaa 1,2,3,4\n0xbb not-numbers\n0xcc 1,2\nnoid\n0xdd 9,8,7,6\n"
        )
        self.assertEqual(parsed, {"0xaa": (1, 3), "0xdd": (9, 7)})


class OptionalStepTests(unittest.IsolatedAsyncioTestCase):
    """A step whose failure is an answer must not end the sequence.

    Dismissing a dialog that may not be there is the ordinary way a task
    begins. Without this the whole sequence ends on the first absent dialog,
    which is why such openings had to stay outside the runner -- and every
    step left outside it is another round trip the agent pays for.
    """

    class Recorder:
        def __init__(self, failing):
            self.failing = failing
            self.performed = []

        async def perform(self, target, operation, params=None):
            self.performed.append(operation)
            if operation in self.failing:
                from wayweaver.errors import ActionError

                raise ActionError(f"{operation} found nothing")
            return {"ok": True, "operation": operation, "data": {}}

        async def observe(self, target, ocr=False):
            return {}

    async def execute(self, steps, failing=()):
        from wayweaver.sequence import run_sequence

        controller = self.Recorder(set(failing))
        result = await run_sequence(controller, "desktop", steps, on_error="stop")
        return result, controller

    async def test_an_optional_failure_lets_the_rest_run(self):
        result, controller = await self.execute(
            [
                {"operation": "element.activate",
                 "params": {"selector": {"name": "Close"}}, "optional": True},
                {"operation": "window.list", "params": {}},
            ],
            failing={"element.activate"},
        )
        self.assertTrue(result["ok"])
        self.assertEqual(controller.performed, ["element.activate", "window.list"])
        self.assertFalse(result["completed"][0]["ok"])
        self.assertTrue(result["completed"][0]["optional"])

    async def test_a_required_failure_still_stops(self):
        result, controller = await self.execute(
            [
                {"operation": "element.activate",
                 "params": {"selector": {"name": "Close"}}},
                {"operation": "window.list", "params": {}},
            ],
            failing={"element.activate"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(controller.performed, ["element.activate"])

    async def test_a_later_step_can_branch_on_the_outcome(self):
        result, controller = await self.execute(
            [
                {"operation": "element.activate",
                 "params": {"selector": {"name": "Close"}},
                 "optional": True, "save_as": "dismissed"},
                {"operation": "window.list", "params": {},
                 "when": {"ref": "dismissed.ok", "equals": True}},
                {"operation": "screen.observe", "params": {}},
            ],
            failing={"element.activate"},
        )
        self.assertTrue(result["ok"])
        # window.list was skipped because the dialog was never there.
        self.assertEqual(controller.performed, ["element.activate", "screen.observe"])
        self.assertTrue(result["completed"][1]["skipped"])

    def test_optional_must_be_a_boolean(self):
        from wayweaver.sequence import _options

        with self.assertRaises(ValueError):
            _options({"operation": "window.list", "optional": "yes"})


class StringSelectorTests(unittest.TestCase):
    """A CSS selector is a string, and preparation must not flatten it.

    The browser operations declare `selector` as a string in their schema,
    while preparation spread every selector as if it were a mapping. That
    raised a bare ValueError from inside the contract layer, so browser.click
    -- whose selector is required -- could never run at all.
    """

    def test_a_string_selector_survives_preparation(self):
        from wayweaver.contracts import prepare_params

        prepared = prepare_params("browser.click", {"selector": "button.radius"})
        self.assertEqual(prepared["selector"], "button.radius")

    def test_a_mapping_selector_is_still_flattened(self):
        from wayweaver.contracts import prepare_params

        prepared = prepare_params(
            "element.find", {"selector": {"name": "Save", "role": "button"}}
        )
        self.assertEqual(prepared["name"], "Save")
        self.assertEqual(prepared["role"], "button")
        self.assertNotIn("selector", prepared)

    def test_both_browser_operations_accept_their_schema(self):
        from wayweaver.contracts import prepare_params, validate_params
        from wayweaver.operations import OPERATIONS

        for name in ("browser.read", "browser.click"):
            spec = OPERATIONS[name]
            params = {"selector": "#main .item"}
            validated = validate_params(name, spec.params_schema, params)
            self.assertEqual(
                prepare_params(name, validated)["selector"], "#main .item"
            )
