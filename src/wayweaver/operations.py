from dataclasses import dataclass
from typing import Any

from .contracts import (
    BOOLEAN,
    COORDINATE_PROPERTIES,
    ELEMENT_EXPECT_SCHEMA,
    ELEMENT_SELECTOR_SCHEMA,
    GENERIC_RESULT_SCHEMA,
    NON_EMPTY_STRING,
    NON_NEGATIVE_INTEGER,
    NUMBER,
    POINT_SCHEMA,
    POSITIVE_INTEGER,
    STRING,
    TIME_PROPERTIES,
    WINDOW_EXPECT_SCHEMA,
    WINDOW_SELECTOR_SCHEMA,
    JSONSchema,
    object_schema,
)
from .types import Capability


API_VERSION = "0.2"


@dataclass(frozen=True, slots=True)
class OperationSpec:
    name: str
    description: str
    required: frozenset[Capability]
    tier: str
    params_schema: JSONSchema
    result_schema: JSONSchema
    examples: tuple[dict[str, Any], ...] = ()
    fallback: frozenset[Capability] = frozenset()
    action: str | None = None


def _spec(
    name: str,
    description: str,
    required: Capability | tuple[Capability, ...] | None,
    *,
    tier: str = "semantic",
    params: JSONSchema | None = None,
    result: JSONSchema = GENERIC_RESULT_SCHEMA,
    examples: tuple[dict[str, Any], ...] = (),
    fallback: tuple[Capability, ...] = (),
    action: str | None = None,
) -> OperationSpec:
    capabilities = (
        ()
        if required is None
        else required
        if isinstance(required, tuple)
        else (required,)
    )
    return OperationSpec(
        name=name,
        description=description,
        required=frozenset(capabilities),
        tier=tier,
        params_schema=params or object_schema(),
        result_schema=result,
        examples=examples,
        fallback=frozenset(fallback),
        action=action,
    )


APPLICATION_SELECTOR = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "name": NON_EMPTY_STRING,
        "exact": BOOLEAN,
        "nth": NON_NEGATIVE_INTEGER,
    },
    any_of=({"required": ["id"]}, {"required": ["name"]}),
)
WINDOW_TARGET_PROPERTIES = {
    "selector": WINDOW_SELECTOR_SCHEMA,
    "expect": WINDOW_EXPECT_SCHEMA,
    **TIME_PROPERTIES,
    "if_exists": BOOLEAN,
}
ELEMENT_TARGET_PROPERTIES = {
    "selector": ELEMENT_SELECTOR_SCHEMA,
    "expect": ELEMENT_EXPECT_SCHEMA,
    **TIME_PROPERTIES,
    "allow_fallback": BOOLEAN,
}
ACTIVATION_VERIFY_SCHEMA = object_schema(
    {
        "surface_changed": BOOLEAN,
        "text_disappeared": NON_EMPTY_STRING,
        **TIME_PROPERTIES,
    },
    any_of=(
        {"required": ["surface_changed"]},
        {"required": ["text_disappeared"]},
    ),
)
ELEMENT_ACTIVATE_PROPERTIES = {
    **ELEMENT_TARGET_PROPERTIES,
    "verify": ACTIVATION_VERIFY_SCHEMA,
}
POINTER_CONTEXT_PROPERTIES = {
    **COORDINATE_PROPERTIES,
    "duration_ms": NON_NEGATIVE_INTEGER,
}
CLICK_RESULT = object_schema(
    {
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "action": NON_EMPTY_STRING,
    },
    required=("x", "y", "action"),
)
TYPE_RESULT = object_schema(
    {"characters": NON_NEGATIVE_INTEGER, "verification": {"type": "object"}},
    required=("characters",),
)
DRAG_RESULT = object_schema(
    {
        "from": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
        },
        "to": {
            "type": "array",
            "items": {"type": "integer"},
            "minItems": 2,
            "maxItems": 2,
        },
    },
    required=("from", "to"),
)
SCROLL_RESULT = object_schema(
    {
        "direction": {"enum": ["up", "down", "left", "right"]},
        "amount": POSITIVE_INTEGER,
    },
    required=("direction", "amount"),
)
KEY_RESULT = object_schema(
    {"keys": {"type": "array", "items": NON_EMPTY_STRING}},
    required=("keys",),
)
TEXT_RESULT = object_schema({"text": STRING}, required=("text",))
COPY_RESULT = object_schema(
    {"text": STRING, "characters": NON_NEGATIVE_INTEGER},
    required=("text", "characters"),
)
CLICKED_RESULT = object_schema({"clicked": BOOLEAN}, required=("clicked",))
APPLICATION_LIST_RESULT = object_schema(
    {"applications": {"type": "array", "items": {"type": "object"}}},
    required=("applications",),
)
APPLICATION_OPEN_RESULT = object_schema(
    {
        "application": {"type": "object"},
        "matches": POSITIVE_INTEGER,
        "pid": POSITIVE_INTEGER,
    },
    required=("application", "matches", "pid"),
)
WINDOW_LIST_RESULT = object_schema(
    {"windows": {"type": "array", "items": {"type": "object"}}},
    required=("windows",),
)
WORKSPACE_LIST_RESULT = object_schema(
    {"workspaces": {"type": "array", "items": {"type": "object"}}},
    required=("workspaces",),
)
WORKSPACE_SWITCH_RESULT = {
    "type": "object",
    "properties": {
        "index": NON_NEGATIVE_INTEGER,
        "workspace": {"type": ["integer", "string"]},
    },
    "anyOf": [{"required": ["index"]}, {"required": ["workspace"]}],
    "additionalProperties": False,
}
ELEMENT_LIST_RESULT = object_schema(
    {
        "elements": {"type": "array", "items": {"type": "object"}},
        "truncated": BOOLEAN,
    },
    required=("elements", "truncated"),
)
TAB_LIST_RESULT = object_schema(
    {"tabs": {"type": "array", "items": {"type": "object"}}},
    required=("tabs",),
)
ELEMENT_POINT_RESULT = {
    "type": "object",
    "properties": {
        "point": POINT_SCHEMA,
        "box": object_schema(
            {
                "left": {"type": "integer"},
                "top": {"type": "integer"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
            },
            required=("left", "top", "width", "height"),
        ),
        "source": NON_EMPTY_STRING,
        "tier": {"enum": ["semantic", "visual"]},
        "surface": object_schema(
            {
                "adapter": NON_EMPTY_STRING,
                "width": POSITIVE_INTEGER,
                "height": POSITIVE_INTEGER,
            },
            required=("adapter", "width", "height"),
        ),
    },
    "required": ["point", "box", "source", "tier", "surface"],
}
OCR_ITEM_RESULT = object_schema(
    {
        "text": NON_EMPTY_STRING,
        "confidence": NUMBER,
        "point": POINT_SCHEMA,
        "box": object_schema(
            {
                "left": {"type": "integer"},
                "top": {"type": "integer"},
                "width": POSITIVE_INTEGER,
                "height": POSITIVE_INTEGER,
            },
            required=("left", "top", "width", "height"),
        ),
    },
    required=("text", "confidence", "point", "box"),
)


OBSERVE_RESULT = object_schema(
    {
        "observation_id": NON_EMPTY_STRING,
        "target": NON_EMPTY_STRING,
        "surface": object_schema(
            {
                "id": NON_EMPTY_STRING,
                "session_id": NON_EMPTY_STRING,
                "adapter": NON_EMPTY_STRING,
                "source": NON_EMPTY_STRING,
                "space": {"enum": ["screen", "viewport"]},
                "width": POSITIVE_INTEGER,
                "height": POSITIVE_INTEGER,
                "scale": NUMBER,
            },
            required=(
                "id",
                "adapter",
                "session_id",
                "source",
                "space",
                "width",
                "height",
                "scale",
            ),
        ),
        "screenshot": NON_EMPTY_STRING,
        "windows": {"type": "array", "items": {"type": "object"}},
        "ocr": STRING,
        "ocr_items": {"type": "array", "items": OCR_ITEM_RESULT},
    },
    required=("observation_id", "target", "surface", "screenshot"),
)
RECORDING_ID = {"type": "string", "pattern": "^[0-9a-f]{32}$"}
RECORDING_STATE_RESULT = object_schema(
    {
        "recording_id": RECORDING_ID,
        "status": {"enum": ["recording", "stopped", "cancelled"]},
        "event_lines": NON_NEGATIVE_INTEGER,
    },
    required=("recording_id", "status"),
)
RECORDING_RESULT = object_schema(
    {
        "recording_id": RECORDING_ID,
        "status": {"enum": ["stopped"]},
        "duration_ms": NON_NEGATIVE_INTEGER,
        "events": {"type": "array", "items": {"type": "object"}},
        "steps": {"type": "array", "items": {"type": "object"}},
        "semantic_steps": NON_NEGATIVE_INTEGER,
        "coordinate_steps": NON_NEGATIVE_INTEGER,
    },
    required=(
        "recording_id",
        "status",
        "duration_ms",
        "events",
        "steps",
        "semantic_steps",
        "coordinate_steps",
    ),
)


_SPECS = (
    _spec(
        "application.list",
        "List launchable desktop applications",
        Capability.APPLICATIONS,
        result=APPLICATION_LIST_RESULT,
        action="list_applications",
    ),
    _spec(
        "application.open",
        "Open a desktop application by ID or name",
        Capability.APPLICATIONS,
        params=object_schema(
            {"selector": APPLICATION_SELECTOR}, required=("selector",)
        ),
        result=APPLICATION_OPEN_RESULT,
        examples=({"selector": {"name": "Xfce Terminal"}},),
        action="open_application",
    ),
    _spec(
        "window.list",
        "List windows with identity, state, and geometry",
        Capability.WINDOWS,
        result=WINDOW_LIST_RESULT,
    ),
    _spec(
        "window.wait",
        "Wait for a window matching identity fields",
        Capability.WINDOWS,
        params=object_schema(WINDOW_TARGET_PROPERTIES, required=("selector",)),
        examples=(
            {
                "selector": {"title": "Visual Studio Code"},
                "expect": {"active": True},
                "timeout_ms": 5000,
            },
            {
                "selector": {"title": "Create New Folder"},
                "expect": {"absent": True},
                "timeout_ms": 5000,
            },
        ),
        action="wait_window",
    ),
    _spec(
        "window.assert",
        "Assert that a matching window exists",
        Capability.WINDOWS,
        params=object_schema(WINDOW_TARGET_PROPERTIES, required=("selector",)),
        action="assert_window",
    ),
    _spec(
        "window.focus",
        "Focus and raise a matching window",
        Capability.WINDOWS,
        params=object_schema(WINDOW_TARGET_PROPERTIES, required=("selector",)),
        examples=({"selector": {"title": "Visual Studio Code"}},),
        action="focus_window",
    ),
    _spec(
        "window.close",
        "Close a matching window",
        Capability.WINDOWS,
        params=object_schema(WINDOW_TARGET_PROPERTIES, required=("selector",)),
        action="close_window",
    ),
    _spec(
        "window.move",
        "Move a matching window",
        Capability.WINDOWS,
        params=object_schema(
            {
                **WINDOW_TARGET_PROPERTIES,
                "x": {"type": "integer"},
                "y": {"type": "integer"},
            },
            required=("selector", "x", "y"),
        ),
        action="move_window",
    ),
    _spec(
        "window.resize",
        "Resize a matching window",
        Capability.WINDOWS,
        params=object_schema(
            {
                **WINDOW_TARGET_PROPERTIES,
                "width": POSITIVE_INTEGER,
                "height": POSITIVE_INTEGER,
            },
            required=("selector", "width", "height"),
        ),
        action="resize_window",
    ),
    *(
        _spec(
            f"window.{verb}",
            f"{verb.title()} a matching window",
            Capability.WINDOWS,
            params=object_schema(WINDOW_TARGET_PROPERTIES, required=("selector",)),
            action=f"{verb}_window",
        )
        for verb in ("minimize", "maximize", "restore")
    ),
    _spec(
        "window.fullscreen",
        "Set or clear fullscreen state",
        Capability.WINDOWS,
        params=object_schema(
            {**WINDOW_TARGET_PROPERTIES, "enabled": BOOLEAN}, required=("selector",)
        ),
        action="fullscreen_window",
    ),
    _spec(
        "workspace.list",
        "List desktops or compositor workspaces",
        Capability.WORKSPACES,
        result=WORKSPACE_LIST_RESULT,
        action="list_workspaces",
    ),
    _spec(
        "workspace.switch",
        "Switch the active workspace",
        Capability.WORKSPACES,
        params=object_schema(
            {
                "index": NON_NEGATIVE_INTEGER,
                "name": NON_EMPTY_STRING,
                "nth": NON_NEGATIVE_INTEGER,
            },
            any_of=({"required": ["index"]}, {"required": ["name"]}),
        ),
        result=WORKSPACE_SWITCH_RESULT,
        action="switch_workspace",
    ),
    _spec(
        "clipboard.read",
        "Read the desktop clipboard",
        Capability.CLIPBOARD,
        result=TEXT_RESULT,
    ),
    _spec(
        "clipboard.write",
        "Replace the desktop clipboard",
        Capability.CLIPBOARD,
        params=object_schema({"text": STRING}, required=("text",)),
        result=TYPE_RESULT,
    ),
    _spec(
        "clipboard.copy",
        "Copy the focused selection and return its exact text",
        (Capability.CLIPBOARD, Capability.KEYBOARD),
        params=object_schema(
            {
                "select_all": BOOLEAN,
                "settle_ms": NON_NEGATIVE_INTEGER,
                "restore": BOOLEAN,
            }
        ),
        result=COPY_RESULT,
        examples=({"select_all": True},),
    ),
    _spec(
        "element.list",
        "List accessible interface elements",
        Capability.ELEMENTS,
        params=object_schema(
            {
                "limit": POSITIVE_INTEGER,
                "max_depth": POSITIVE_INTEGER,
                "include_offscreen": BOOLEAN,
            }
        ),
        result=ELEMENT_LIST_RESULT,
    ),
    _spec(
        "element.find",
        "Find an accessible element or visible-text fallback",
        Capability.ELEMENTS,
        params=object_schema(ELEMENT_TARGET_PROPERTIES, required=("selector",)),
        examples=({"selector": {"name": "Save", "role": "button"}},),
        fallback=(Capability.CAPTURE,),
    ),
    _spec(
        "element.focused",
        "Describe the element that currently holds keyboard focus",
        Capability.ELEMENTS,
        # The helper scopes to an application when asked; hiding that here
        # forced every focus query to walk the whole desktop, which costs
        # seconds when a large toolkit is registered.
        params=object_schema(
            {"max_depth": POSITIVE_INTEGER, "application": NON_EMPTY_STRING}
        ),
        examples=({"application": "Chrome"},),
    ),
    _spec(
        "element.point",
        "Resolve an element to an actionable point through the richest layer available",
        Capability.ELEMENTS,
        params=object_schema(ELEMENT_TARGET_PROPERTIES, required=("selector",)),
        result=ELEMENT_POINT_RESULT,
        fallback=(Capability.CAPTURE,),
        examples=(
            {
                "selector": {"name": "Save", "role": "push button"},
                "allow_fallback": False,
            },
        ),
    ),
    _spec(
        "element.assert",
        "Assert an accessible element and optional state",
        Capability.ELEMENTS,
        params=object_schema(ELEMENT_TARGET_PROPERTIES, required=("selector",)),
    ),
    _spec(
        "element.wait",
        "Wait for an accessible element and optional state",
        Capability.ELEMENTS,
        params=object_schema(ELEMENT_TARGET_PROPERTIES, required=("selector",)),
        examples=(
            {
                "selector": {"name": "Save", "role": "button"},
                "expect": {"state": "enabled", "value": True},
                "timeout_ms": 5000,
            },
        ),
    ),
    *(
        _spec(
            f"element.{verb}",
            description,
            Capability.ELEMENTS,
            params=object_schema(
                ELEMENT_ACTIVATE_PROPERTIES
                if verb == "activate"
                else ELEMENT_TARGET_PROPERTIES,
                required=("selector",),
            ),
            fallback=(Capability.CAPTURE, Capability.POINTER)
            if verb == "activate"
            else (),
        )
        for verb, description in (
            ("activate", "Activate an accessible element or visible-text fallback"),
            ("focus", "Focus an accessible interface element"),
            ("read", "Read accessible element text and state"),
        )
    ),
    _spec(
        "element.set_value",
        "Set an accessible element value or text",
        Capability.ELEMENTS,
        params=object_schema(
            {**ELEMENT_TARGET_PROPERTIES, "value": {}}, required=("selector", "value")
        ),
    ),
    _spec(
        "pointer.move",
        "Move the pointer",
        Capability.POINTER,
        tier="visual",
        params=object_schema(
            {"point": POINT_SCHEMA, **POINTER_CONTEXT_PROPERTIES},
            required=("point", "space"),
        ),
        examples=({"point": {"x": 640, "y": 450}, "space": "screen"},),
        result=CLICK_RESULT,
        action="move",
    ),
    _spec(
        "pointer.click",
        "Click a point",
        Capability.POINTER,
        tier="visual",
        params=object_schema(
            {
                "point": POINT_SCHEMA,
                **POINTER_CONTEXT_PROPERTIES,
                "button": {"enum": ["left", "middle", "right"]},
                "count": {"type": "integer", "minimum": 1, "maximum": 2},
            },
            required=("point", "space"),
        ),
        result=CLICK_RESULT,
        examples=(
            {
                "point": {"x": 1225, "y": 204},
                "space": "screen",
                "button": "left",
                "count": 1,
                "duration_ms": 100,
            },
        ),
    ),
    _spec(
        "pointer.drag",
        "Drag between two points",
        Capability.POINTER,
        tier="visual",
        params=object_schema(
            {
                "from": POINT_SCHEMA,
                "to": POINT_SCHEMA,
                **POINTER_CONTEXT_PROPERTIES,
                "button": {"enum": ["left", "middle", "right"]},
            },
            required=("from", "to", "space"),
        ),
        result=DRAG_RESULT,
        action="drag",
    ),
    _spec(
        "pointer.scroll",
        "Scroll at an optional point",
        Capability.SCROLL,
        tier="visual",
        params=object_schema(
            {
                "point": POINT_SCHEMA,
                **COORDINATE_PROPERTIES,
                "direction": {"enum": ["up", "down", "left", "right"]},
                "amount": POSITIVE_INTEGER,
            },
            required=("space", "direction"),
        ),
        result=SCROLL_RESULT,
        action="scroll",
    ),
    _spec(
        "keyboard.type",
        "Type Unicode text into the focused interface",
        Capability.KEYBOARD,
        tier="visual",
        params=object_schema(
            {
                "text": STRING,
                "interval_ms": NON_NEGATIVE_INTEGER,
                "verify": BOOLEAN,
            },
            required=("text",),
        ),
        result=TYPE_RESULT,
        examples=({"text": "hello", "interval_ms": 15},),
        action="type",
    ),
    _spec(
        "keyboard.press",
        "Press and release one canonical key",
        Capability.KEYBOARD,
        tier="visual",
        params=object_schema({"key": NON_EMPTY_STRING}, required=("key",)),
        examples=({"key": "ENTER"},),
        result=KEY_RESULT,
        action="key",
    ),
    _spec(
        "keyboard.chord",
        "Press a canonical key chord",
        Capability.KEYBOARD,
        tier="visual",
        params=object_schema(
            {
                "keys": {
                    "type": "array",
                    "items": NON_EMPTY_STRING,
                    "minItems": 2,
                    "uniqueItems": True,
                }
            },
            required=("keys",),
        ),
        examples=({"keys": ["CTRL", "SHIFT", "P"]},),
        result=KEY_RESULT,
        action="key",
    ),
    _spec(
        "screen.observe",
        "Capture screen, windows, and optional OCR",
        Capability.CAPTURE,
        tier="visual",
        params=object_schema({"ocr": BOOLEAN}),
        result=OBSERVE_RESULT,
        examples=({"ocr": True},),
    ),
    _spec(
        "shell.execute",
        "Execute a shell command",
        Capability.SHELL,
        params=object_schema(
            {
                "command": NON_EMPTY_STRING,
                "stdin": STRING,
                "check": BOOLEAN,
                "allowed_exit_codes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
            required=("command",),
        ),
        result=object_schema(
            {
                "exit_code": {"type": "integer"},
                "success": BOOLEAN,
                "stdout": STRING,
                "stderr": STRING,
            },
            required=("exit_code", "success", "stdout", "stderr"),
        ),
        examples=(
            {"command": "uname -a", "check": True},
            {"command": "test -f /tmp/ready", "allowed_exit_codes": [0, 1]},
        ),
    ),
    _spec(
        "browser.navigate",
        "Navigate the visible browser page",
        Capability.BROWSER,
        params=object_schema({"url": NON_EMPTY_STRING}, required=("url",)),
        examples=({"url": "https://example.com"},),
    ),
    _spec(
        "tab.list",
        "List browser tabs and targets",
        Capability.BROWSER,
        result=TAB_LIST_RESULT,
    ),
    _spec(
        "browser.read",
        "Read visible browser page text",
        Capability.BROWSER,
        params=object_schema({"selector": NON_EMPTY_STRING}),
        result=TEXT_RESULT,
    ),
    _spec(
        "browser.click",
        "Click a browser element by CSS selector",
        Capability.BROWSER,
        params=object_schema({"selector": NON_EMPTY_STRING}, required=("selector",)),
        result=CLICKED_RESULT,
    ),
    _spec(
        "browser.type",
        "Focus a browser element and insert text",
        Capability.BROWSER,
        params=object_schema(
            {"selector": NON_EMPTY_STRING, "text": STRING, "clear": BOOLEAN},
            required=("selector", "text"),
        ),
        result=TYPE_RESULT,
    ),
    _spec(
        "recording.start",
        "Start an independently controlled X11 interaction recording",
        Capability.RECORDING,
        result=RECORDING_STATE_RESULT,
        examples=({},),
    ),
    _spec(
        "recording.status",
        "Inspect a live X11 interaction recording",
        Capability.RECORDING,
        params=object_schema(
            {"recording_id": RECORDING_ID}, required=("recording_id",)
        ),
        result=RECORDING_STATE_RESULT,
    ),
    _spec(
        "recording.stop",
        "Stop a recording and emit canonical semantic and coordinate steps",
        Capability.RECORDING,
        params=object_schema(
            {"recording_id": RECORDING_ID, "infer_elements": BOOLEAN},
            required=("recording_id",),
        ),
        result=RECORDING_RESULT,
    ),
    _spec(
        "recording.cancel",
        "Cancel a recording without emitting steps",
        Capability.RECORDING,
        params=object_schema(
            {"recording_id": RECORDING_ID}, required=("recording_id",)
        ),
        result=RECORDING_STATE_RESULT,
    ),
    _spec(
        "recording.capture",
        "Record bounded X11 pointer and keyboard interactions",
        Capability.RECORDING,
        params=object_schema(
            {
                "duration_ms": {
                    "type": "integer",
                    "minimum": 100,
                    "maximum": 60_000,
                },
                "infer_elements": BOOLEAN,
            },
            required=("duration_ms",),
        ),
        result=RECORDING_RESULT,
        examples=({"duration_ms": 5000, "infer_elements": True},),
    ),
    _spec("viewer.open", "Open an interactive viewer", Capability.VIEWER),
    _spec(
        "time.sleep",
        "Pause a sequence for a bounded duration",
        None,
        tier="control",
        params=object_schema(
            {"duration_ms": NON_NEGATIVE_INTEGER}, required=("duration_ms",)
        ),
        result=object_schema(
            {"duration_ms": NON_NEGATIVE_INTEGER}, required=("duration_ms",)
        ),
        examples=({"duration_ms": 250},),
    ),
)

OPERATIONS = {spec.name: spec for spec in _SPECS}
