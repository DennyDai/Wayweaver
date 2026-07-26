from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .errors import ContractError, ProtocolError


JSONSchema = dict[str, Any]


def object_schema(
    properties: dict[str, JSONSchema] | None = None,
    *,
    required: tuple[str, ...] = (),
    any_of: tuple[JSONSchema, ...] = (),
) -> JSONSchema:
    schema: JSONSchema = {
        "type": "object",
        "properties": properties or {},
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    if any_of:
        schema["anyOf"] = list(any_of)
    return schema


STRING: JSONSchema = {"type": "string"}
NON_EMPTY_STRING: JSONSchema = {"type": "string", "minLength": 1}
BOOLEAN: JSONSchema = {"type": "boolean"}
NON_NEGATIVE_INTEGER: JSONSchema = {"type": "integer", "minimum": 0}
POSITIVE_INTEGER: JSONSchema = {"type": "integer", "minimum": 1}
NUMBER: JSONSchema = {"type": "number"}

POINT_SCHEMA = object_schema(
    {"x": {"type": "integer"}, "y": {"type": "integer"}},
    required=("x", "y"),
)
REGION_SCHEMA = object_schema(
    {
        "x": {"type": "integer"},
        "y": {"type": "integer"},
        "width": POSITIVE_INTEGER,
        "height": POSITIVE_INTEGER,
    },
    required=("x", "y", "width", "height"),
)
COORDINATE_PROPERTIES: dict[str, JSONSchema] = {
    "space": {
        "type": "string",
        "enum": ["screen", "surface", "viewport"],
    },
    "surface_id": NON_EMPTY_STRING,
    "observation_id": NON_EMPTY_STRING,
}
TIME_PROPERTIES: dict[str, JSONSchema] = {
    "timeout_ms": NON_NEGATIVE_INTEGER,
    "interval_ms": POSITIVE_INTEGER,
}
WINDOW_SELECTOR_SCHEMA = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "title": NON_EMPTY_STRING,
        "class_name": NON_EMPTY_STRING,
        "pid": NON_NEGATIVE_INTEGER,
        "exact": BOOLEAN,
        "nth": NON_NEGATIVE_INTEGER,
    },
    any_of=(
        {"required": ["id"]},
        {"required": ["title"]},
        {"required": ["class_name"]},
        {"required": ["pid"]},
    ),
)
WINDOW_EXPECT_SCHEMA = object_schema({"active": BOOLEAN, "absent": BOOLEAN})
ELEMENT_SELECTOR_SCHEMA = object_schema(
    {
        "id": NON_EMPTY_STRING,
        "resource_id": NON_EMPTY_STRING,
        "automation_id": NON_EMPTY_STRING,
        "name": NON_EMPTY_STRING,
        "text": NON_EMPTY_STRING,
        "role": NON_EMPTY_STRING,
        "class_name": NON_EMPTY_STRING,
        "exact": BOOLEAN,
        "contains": BOOLEAN,
        "nth": NON_NEGATIVE_INTEGER,
        "region": REGION_SCHEMA,
        "window": NON_EMPTY_STRING,
        "application": NON_EMPTY_STRING,
        "strict": BOOLEAN,
        "include_offscreen": BOOLEAN,
        "scroll": {"type": "integer", "minimum": 0, "maximum": 30},
        "fuzzy": BOOLEAN,
        "similarity": {"type": "number", "minimum": 0, "maximum": 1},
        "full_screen_fallback": BOOLEAN,
    }
)
ELEMENT_SELECTOR_SCHEMA["anyOf"] = [
    {"required": [field]}
    for field in (
        "id",
        "resource_id",
        "automation_id",
        "name",
        "text",
        "role",
        "class_name",
        "region",
    )
]
ELEMENT_SELECTOR_SCHEMA["not"] = {
    "properties": {"exact": {"const": True}, "contains": {"const": True}},
    "required": ["exact", "contains"],
}
ELEMENT_EXPECT_SCHEMA = object_schema(
    {"state": NON_EMPTY_STRING, "value": {}, "active": BOOLEAN}
)
GENERIC_RESULT_SCHEMA: JSONSchema = {"type": "object"}


def validate_params(
    operation: str, schema: JSONSchema, params: object
) -> dict[str, Any]:
    if not isinstance(params, dict):
        raise ContractError(
            f"{operation} params must be an object",
            details={"operation": operation},
        )
    try:
        Draft202012Validator(schema).validate(params)
    except ValidationError as error:
        location = ".".join(str(item) for item in error.absolute_path)
        prefix = f"{location}: " if location else ""
        raise ContractError(
            f"invalid {operation} params: {prefix}{error.message}",
            details={
                "operation": operation,
                "path": list(error.absolute_path),
                "validator": error.validator,
            },
        ) from error
    return deepcopy(params)


def validate_result(operation: str, schema: JSONSchema, result: object) -> None:
    try:
        Draft202012Validator(schema).validate(result)
    except ValidationError as error:
        raise ProtocolError(
            f"{operation} returned data outside its result contract: {error.message}",
            details={
                "operation": operation,
                "path": list(error.absolute_path),
                "validator": error.validator,
            },
            retryable=False,
        ) from error


def _region_bounds(region: dict[str, Any]) -> list[int]:
    x = int(region["x"])
    y = int(region["y"])
    return [x, y, x + int(region["width"]), y + int(region["height"])]


# Canonical key names use X11 keysym spelling, which every backend adapter
# translates from. The set previously stopped at a dozen entries, so navigation
# and editing keys passed through untranslated: `UP`, `HOME` and `PAGEUP` are
# not valid keysyms and X11 silently dropped them, while CDP received DOM key
# values it does not recognize. Anything absent here still passes through, so a
# backend-specific keysym remains usable.
_KEY_ALIASES = {
    "ENTER": "Return",
    "RETURN": "Return",
    "ESC": "Escape",
    "ESCAPE": "Escape",
    "CTRL": "ctrl",
    "CONTROL": "ctrl",
    "ALT": "alt",
    "SHIFT": "shift",
    "META": "meta",
    "SUPER": "super",
    "WIN": "super",
    "SPACE": "space",
    "BACKSPACE": "BackSpace",
    "BKSP": "BackSpace",
    "DELETE": "Delete",
    "DEL": "Delete",
    "TAB": "Tab",
    "INSERT": "Insert",
    "INS": "Insert",
    "HOME": "Home",
    "END": "End",
    "PAGEUP": "Page_Up",
    "PAGE_UP": "Page_Up",
    "PGUP": "Page_Up",
    "PAGEDOWN": "Page_Down",
    "PAGE_DOWN": "Page_Down",
    "PGDN": "Page_Down",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "MENU": "Menu",
    "PRINTSCREEN": "Print",
    "CAPSLOCK": "Caps_Lock",
}
_KEY_ALIASES.update({f"F{index}": f"F{index}" for index in range(1, 25)})


def _canonical_key(value: str) -> str:
    return _KEY_ALIASES.get(value.upper(), value)


def prepare_params(operation: str, params: dict[str, Any]) -> dict[str, Any]:
    prepared = deepcopy(params)
    selector = prepared.pop("selector", None)
    if selector:
        prepared.update(selector)
    expected = prepared.pop("expect", None)
    if expected:
        prepared.update(expected)
    if "class_name" in prepared:
        prepared["class"] = prepared.pop("class_name")
    if isinstance(prepared.get("region"), dict):
        prepared["region"] = _region_bounds(prepared["region"])
    if "timeout_ms" in prepared:
        prepared["timeout"] = prepared.pop("timeout_ms") / 1000
    if "interval_ms" in prepared:
        interval_ms = int(prepared.pop("interval_ms"))
        if operation == "keyboard.type":
            prepared["delay_ms"] = interval_ms
        else:
            prepared["interval"] = interval_ms / 1000
    if operation in {"pointer.move", "pointer.click"}:
        prepared.update(prepared.pop("point"))
    elif operation == "pointer.drag":
        start = prepared.pop("from")
        end = prepared.pop("to")
        prepared.update(
            {"x1": start["x"], "y1": start["y"], "x2": end["x"], "y2": end["y"]}
        )
    elif operation == "pointer.scroll" and "point" in prepared:
        prepared.update(prepared.pop("point"))
    if operation in {"pointer.click", "pointer.drag"} and "button" in prepared:
        prepared["button"] = {"left": 1, "middle": 2, "right": 3}[prepared["button"]]
    if operation == "keyboard.press":
        prepared["keys"] = [_canonical_key(prepared.pop("key"))]
    elif operation == "keyboard.chord":
        prepared["keys"] = [
            "+".join(_canonical_key(key) for key in prepared.pop("keys"))
        ]
    for field in ("space", "surface_id", "observation_id"):
        prepared.pop(field, None)
    return prepared
