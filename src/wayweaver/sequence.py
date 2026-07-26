import asyncio
import re
import json
from typing import Any

from .controller import Controller
from .errors import WayweaverError, error_payload
from .operations import OPERATIONS


_STEP_FIELDS = {
    "id",
    "operation",
    "params",
    "retry",
    "repeat",
    "save_as",
    "when",
    "optional",
}
_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def normalize(step: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if not isinstance(step, dict):
        raise ValueError("each step must be an object")
    if "operation" in step:
        unexpected = set(step) - _STEP_FIELDS
        if unexpected:
            raise ValueError(
                "explicit steps contain unknown fields: "
                + ", ".join(sorted(unexpected))
            )
        operation = str(step["operation"])
        params = step.get("params", {})
        if operation not in OPERATIONS:
            raise ValueError(f"unknown operation: {operation}")
        if not isinstance(params, dict):
            raise ValueError("step params must be an object")
        return operation, dict(params)
    if len(step) != 1:
        raise ValueError("each compact step must contain exactly one operation")
    operation, value = next(iter(step.items()))
    if operation not in OPERATIONS:
        raise ValueError(f"unknown operation: {operation}")
    if isinstance(value, dict):
        return operation, dict(value)
    if value is True or value is None:
        return operation, {}
    if operation == "time.sleep" and isinstance(value, (int, float)):
        return operation, {"duration_ms": round(value)}
    if operation == "workspace.switch":
        return operation, {"index": value} if isinstance(value, int) else {
            "name": value
        }
    if operation == "application.open" and isinstance(value, str):
        return operation, {"selector": {"name": value}}
    if operation.startswith("window.") and isinstance(value, str):
        return operation, {"selector": {"title": value}}
    if operation.startswith("element.") and isinstance(value, str):
        return operation, {"selector": {"name": value}}
    if operation in {"pointer.move", "pointer.click"}:
        if not isinstance(value, list) or len(value) != 2:
            raise ValueError(f"{operation} expects [x, y]")
        return operation, {
            "point": {"x": value[0], "y": value[1]},
            "space": "screen",
        }
    if operation == "pointer.drag":
        if not isinstance(value, list) or len(value) != 4:
            raise ValueError("pointer.drag expects [x1, y1, x2, y2]")
        return operation, {
            "from": {"x": value[0], "y": value[1]},
            "to": {"x": value[2], "y": value[3]},
            "space": "screen",
        }
    if operation == "pointer.scroll" and isinstance(value, str):
        return operation, {"direction": value, "space": "screen"}
    if operation == "keyboard.type" and isinstance(value, str):
        return operation, {"text": value}
    if operation == "keyboard.press" and isinstance(value, str):
        return operation, {"key": value}
    if operation == "keyboard.chord" and isinstance(value, list):
        return operation, {"keys": value}
    if operation == "clipboard.write" and isinstance(value, str):
        return operation, {"text": value}
    if operation == "browser.navigate" and isinstance(value, str):
        return operation, {"url": value}
    if operation in {"browser.read", "browser.click"} and isinstance(value, str):
        return operation, {"selector": value}
    raise ValueError(f"{operation} compact value has no supported form")


def _lookup(reference: str, saved: dict[str, Any]) -> Any:
    parts = reference.split(".")
    if not parts or parts[0] not in saved:
        raise ValueError(f"unknown saved result: {reference!r}")
    value: Any = saved[parts[0]]
    for part in parts[1:]:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            raise ValueError(f"saved result path not found: {reference!r}")
    return value


def _resolve(value: Any, saved: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            reference = value["$ref"]
            if not isinstance(reference, str):
                raise ValueError("$ref must be a string")
            return _lookup(reference, saved)
        return {key: _resolve(item, saved) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item, saved) for item in value]
    return value


def _options(step: dict[str, Any]) -> dict[str, Any]:
    if "operation" not in step:
        return {
            "id": None,
            "max_attempts": 1,
            "backoff_ms": 0,
            "on_codes": None,
            "repeat": 1,
            "save_as": None,
            "when": None,
            "optional": False,
        }
    step_id = step.get("id")
    if step_id is not None and (
        not isinstance(step_id, str) or not _VARIABLE_NAME.fullmatch(step_id)
    ):
        raise ValueError("step id must be an identifier")
    save_as = step.get("save_as")
    if save_as is not None and (
        not isinstance(save_as, str) or not _VARIABLE_NAME.fullmatch(save_as)
    ):
        raise ValueError("save_as must be an identifier")
    retry = step.get("retry", {})
    if not isinstance(retry, dict) or set(retry) - {
        "max_attempts",
        "backoff_ms",
        "on_codes",
    }:
        raise ValueError("retry accepts only max_attempts, backoff_ms, and on_codes")
    max_attempts = retry.get("max_attempts", 1)
    backoff_ms = retry.get("backoff_ms", 0)
    repeat = step.get("repeat", 1)
    on_codes = retry.get("on_codes")
    if on_codes is not None and (
        not isinstance(on_codes, list)
        or not on_codes
        or not all(isinstance(code, str) and code for code in on_codes)
        or len(set(on_codes)) != len(on_codes)
    ):
        raise ValueError("retry.on_codes must be a non-empty array of unique codes")
    if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
        raise ValueError("retry.max_attempts must be between 1 and 10")
    if not isinstance(backoff_ms, int) or not 0 <= backoff_ms <= 60_000:
        raise ValueError("retry.backoff_ms must be between 0 and 60000")
    if not isinstance(repeat, int) or not 1 <= repeat <= 100:
        raise ValueError("repeat must be between 1 and 100")
    optional = step.get("optional", False)
    if not isinstance(optional, bool):
        raise ValueError("optional must be a boolean")
    when = step.get("when")
    if when is not None and (
        not isinstance(when, dict)
        or set(when) != {"ref", "equals"}
        or not isinstance(when["ref"], str)
    ):
        raise ValueError("when must contain ref and equals")
    return {
        "id": step_id,
        "max_attempts": max_attempts,
        "backoff_ms": backoff_ms,
        "on_codes": frozenset(on_codes) if on_codes is not None else None,
        "repeat": repeat,
        "save_as": save_as,
        "when": when,
        "optional": optional,
    }


def _bounded_saved(saved: dict[str, Any], limit: int) -> tuple[dict[str, Any], int]:
    public: dict[str, Any] = {}
    used = 2
    omitted = 0
    for name, value in saved.items():
        encoded = json.dumps(value, separators=(",", ":"), default=str).encode()
        candidate: Any = value
        if len(encoded) > max(0, limit - used):
            candidate = {"truncated": True, "bytes": len(encoded)}
        candidate_size = len(
            json.dumps({name: candidate}, separators=(",", ":"), default=str).encode()
        )
        if used + candidate_size > limit:
            omitted += 1
            continue
        public[name] = candidate
        used += candidate_size
    return public, omitted


async def run_sequence(
    controller: Controller,
    target: str,
    steps: list[dict[str, Any]],
    *,
    on_error: str = "observe",
    observe_after: bool = False,
    saved_output_limit: int = 32_768,
) -> dict[str, Any]:
    if on_error not in {"observe", "stop"}:
        raise ValueError("on_error must be 'observe' or 'stop'")
    if (
        not isinstance(saved_output_limit, int)
        or not 0 <= saved_output_limit <= 1_000_000
    ):
        raise ValueError("saved_output_limit must be between 0 and 1000000")
    completed = []
    saved: dict[str, Any] = {}
    for index, step in enumerate(steps):
        repeat_index = 0
        attempts = 0
        try:
            operation, raw_params = normalize(step)
            options = _options(step)
            when = options["when"]
            if when is not None and _lookup(when["ref"], saved) != when["equals"]:
                skipped = {
                    "step": index,
                    "operation": operation,
                    "ok": True,
                    "skipped": True,
                }
                if options["id"]:
                    skipped["id"] = options["id"]
                completed.append(skipped)
                continue
            executions = []
            for repeat_index in range(options["repeat"]):
                params = _resolve(raw_params, saved)
                failure = None
                for attempts in range(1, options["max_attempts"] + 1):
                    try:
                        response = await controller.perform(target, operation, params)
                        break
                    except WayweaverError as error:
                        allowed_codes = options["on_codes"]
                        should_retry = error.retryable and (
                            allowed_codes is None or error.code in allowed_codes
                        )
                        if not should_retry or attempts >= options["max_attempts"]:
                            # An optional step is one whose failure is an
                            # answer: dismissing a dialog that may not be
                            # there is the ordinary way a task begins, and
                            # without this the whole sequence ends on it.
                            if not options["optional"]:
                                raise
                            failure = error
                            break
                        if options["backoff_ms"]:
                            await asyncio.sleep(options["backoff_ms"] / 1000)
                if failure is not None:
                    response = {
                        "ok": False,
                        "operation": operation,
                        "optional": True,
                        "error": error_payload(failure),
                    }
                entry = {"step": index, **response, "attempts": attempts}
                if options["id"]:
                    entry["id"] = options["id"]
                if options["repeat"] > 1:
                    entry["repeat_index"] = repeat_index
                completed.append(entry)
                executions.append(response)
                if failure is not None:
                    break
            if options["save_as"]:
                saved[options["save_as"]] = (
                    executions[0] if len(executions) == 1 else executions
                )
        except Exception as error:
            public_saved, omitted = _bounded_saved(saved, saved_output_limit)
            output: dict[str, Any] = {
                "ok": False,
                "failed_step": index,
                "step": step,
                "error": error_payload(error),
                "completed": completed,
                "failure": {
                    "repeat_index": repeat_index,
                    "attempts": attempts,
                },
                "saved": public_saved,
            }
            if omitted:
                output["saved_omitted"] = omitted
            if on_error == "observe":
                try:
                    output["observation"] = await controller.observe(target, True)
                except Exception as observation_error:
                    output["observation_error"] = error_payload(observation_error)
            return output
    public_saved, omitted = _bounded_saved(saved, saved_output_limit)
    output = {"ok": True, "completed": completed, "saved": public_saved}
    if omitted:
        output["saved_omitted"] = omitted
    if observe_after:
        output["observation"] = await controller.observe(target, True)
    return output
