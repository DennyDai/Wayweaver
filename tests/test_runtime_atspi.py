"""Unit tests for the packaged AT-SPI helper.

The helper ships as an extensionless script and imports pyatspi lazily, so its
tree-walking and matching logic can be loaded and exercised without a live
accessibility bus.
"""

import unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

HELPER = (
    Path(__file__).resolve().parent.parent
    / "src/wayweaver/runtime/assets/linux/wayweaver-atspi"
)


def load_helper():
    loader = SourceFileLoader("wayweaver_atspi", str(HELPER))
    spec = spec_from_loader(loader.name, loader)
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeNode:
    """Stands in for an AT-SPI accessible."""

    def __init__(self, name="", role="panel", children=()):
        self.name = name
        self._role = role
        self._children = list(children)

    @property
    def childCount(self):  # noqa: N802 - mirrors the AT-SPI API
        return len(self._children)

    def getChildAtIndex(self, index):  # noqa: N802 - mirrors the AT-SPI API
        return self._children[index]

    def getRoleName(self):  # noqa: N802 - mirrors the AT-SPI API
        return self._role


def chain(depth, leaf_name="target"):
    """A single spine `depth` levels deep, with a named leaf at the bottom."""
    node = FakeNode(name=leaf_name, role="entry")
    for _ in range(depth):
        node = FakeNode(children=[node])
    return node


class AtspiHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()

    def test_default_depth_reaches_browser_page_content(self):
        # Chrome nests page content roughly fifty levels below the desktop root;
        # a shallower default silently hides every web element.
        self.assertGreaterEqual(self.helper.DEFAULT_MAX_DEPTH, 50)

    def test_walk_descends_to_the_default_depth(self):
        found = [
            node.name
            for node, _ in self.helper.walk(None, chain(45))
            if getattr(node, "name", "")
        ]

        self.assertIn("target", found)

    def test_walk_stops_at_an_explicit_max_depth(self):
        found = [
            node.name
            for node, _ in self.helper.walk(None, chain(45), max_depth=10)
            if getattr(node, "name", "")
        ]

        self.assertNotIn("target", found)

    def test_walk_reports_the_path_to_each_node(self):
        tree = FakeNode(children=[FakeNode(children=[FakeNode(name="leaf")])])

        paths = {node.name: path for node, path in self.helper.walk(None, tree)}

        self.assertEqual(paths["leaf"], (0, 0))

    def test_walk_survives_a_node_that_raises(self):
        class Hostile(FakeNode):
            @property
            def childCount(self):  # noqa: N802
                raise RuntimeError("disconnected")

        tree = FakeNode(children=[Hostile(name="broken"), FakeNode(name="sibling")])

        found = [node.name for node, _ in self.helper.walk(None, tree) if node.name]

        self.assertEqual(found, ["broken", "sibling"])

    def test_role_names_match_across_toolkit_vocabularies(self):
        # The same widget is "push button" to GTK and Chrome but "button" to
        # the Java ATK bridge; an exact comparison silently found nothing.
        for wanted, actual in (
            ("push button", "button"),
            ("button", "push button"),
            ("pushbutton", "button"),
            ("push_button", "push button"),
            ("entry", "text"),
            ("check box", "checkbutton"),
        ):
            with self.subTest(wanted=wanted, actual=actual):
                self.assertTrue(self.helper.role_matches(wanted, actual))

    def test_unrelated_roles_do_not_match(self):
        for wanted, actual in (
            ("push button", "menu item"),
            ("menu", "menu item"),
            ("entry", "push button"),
        ):
            with self.subTest(wanted=wanted, actual=actual):
                self.assertFalse(self.helper.role_matches(wanted, actual))

    def test_an_absent_role_matches_anything(self):
        self.assertTrue(self.helper.role_matches("", "push button"))

    def test_role_selector_uses_the_normalized_comparison(self):
        node = FakeNode(name="Yes", role="button")

        self.assertTrue(self.helper.matches(node, {"name": "Yes", "role": "push button"}))
        self.assertFalse(self.helper.matches(node, {"name": "Yes", "role": "menu item"}))

    def test_matches_on_a_name_substring_by_default(self):
        node = FakeNode(name="Username", role="entry")

        self.assertTrue(self.helper.matches(node, {"name": "user"}))
        self.assertFalse(self.helper.matches(node, {"name": "password"}))

    def test_exact_name_matching_rejects_a_substring(self):
        node = FakeNode(name="Username field", role="entry")

        self.assertFalse(self.helper.matches(node, {"name": "Username", "exact": True}))
        self.assertTrue(
            self.helper.matches(node, {"name": "Username field", "exact": True})
        )

    def test_role_narrows_a_name_match(self):
        node = FakeNode(name="Username", role="entry")

        self.assertTrue(self.helper.matches(node, {"name": "Username", "role": "entry"}))
        self.assertFalse(
            self.helper.matches(node, {"name": "Username", "role": "push button"})
        )

    def test_an_empty_selector_matches_nothing(self):
        node = FakeNode(name="Username", role="entry")

        self.assertFalse(self.helper.matches(node, {}))


if __name__ == "__main__":
    unittest.main()


class Extents:
    def __init__(self, x, y, width, height):
        self.x, self.y = x, y
        self.width, self.height = width, height


class PlacedNode(FakeNode):
    """A node with a position, so on-screen filtering can be exercised."""

    def __init__(self, name="", role="entry", children=(), extents=None):
        super().__init__(name=name, role=role, children=children)
        self.description = ""
        self._extents = extents or Extents(0, 0, 10, 10)

    def queryComponent(self):  # noqa: N802 - mirrors the AT-SPI API
        return self

    def getExtents(self, _coords):  # noqa: N802 - mirrors the AT-SPI API
        return self._extents

    def getState(self):  # noqa: N802 - mirrors the AT-SPI API
        raise NotImplementedError

    def queryAction(self):  # noqa: N802 - mirrors the AT-SPI API
        raise NotImplementedError


class FakePyatspi:
    """Enough of pyatspi for find() to run without a bus."""

    DESKTOP_COORDS = 0

    def __init__(self, desktop):
        self.Registry = self
        self._desktop = desktop

    def getDesktop(self, _index):  # noqa: N802 - mirrors the AT-SPI API
        return self._desktop


class OffScreenRankingTests(unittest.TestCase):
    """An element below the fold must be findable, but must never outrank a
    visible one -- reporting it missing sends callers to OCR, which matches the
    same word in surrounding prose."""

    SCREEN = (1600, 900)

    @classmethod
    def setUpClass(cls):
        cls.helper = load_helper()

    def setUp(self):
        self._screen_size = self.helper.screen_size
        self.helper.screen_size = lambda: self.SCREEN
        self.addCleanup(setattr, self.helper, "screen_size", self._screen_size)

    def find(self, desktop, options):
        return self.helper.find(FakePyatspi(desktop), options)

    def test_offscreen_match_is_hidden_by_default(self):
        below = PlacedNode("Username", extents=Extents(569, 1400, 368, 39))
        with self.assertRaises(ValueError):
            self.find(FakeNode(children=[below]), {"name": "Username"})

    def test_offscreen_match_is_returned_when_requested(self):
        below = PlacedNode("Username", extents=Extents(569, 1400, 368, 39))
        _node, described, count = self.find(
            FakeNode(children=[below]),
            {"name": "Username", "include_offscreen": True},
        )
        self.assertEqual(described["bounds"]["top"], 1400)
        self.assertEqual(count, 1)

    def test_a_visible_match_outranks_an_offscreen_one_found_first(self):
        # Tree order puts the off-screen copy first; the visible one must win.
        below = PlacedNode("Username", extents=Extents(569, 1400, 368, 39))
        visible = PlacedNode("Username", extents=Extents(569, 300, 368, 39))
        _node, described, _count = self.find(
            FakeNode(children=[below, visible]),
            {"name": "Username", "include_offscreen": True},
        )
        self.assertEqual(described["bounds"]["top"], 300)
