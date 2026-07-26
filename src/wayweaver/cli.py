import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from .controller import Controller
from .sequence import run_sequence
from .errors import error_payload


def load_json(value: str, allow_path: bool = False) -> Any:
    """Parse inline JSON, or a file/stdin when the argument documents that.

    Only `run` advertises a file path, so treating every params argument as a
    possible filename made behaviour depend on what happened to exist on disk.
    """
    if value == "-":
        return json.load(sys.stdin)
    if allow_path:
        try:
            path = Path(value)
            if path.is_file():
                return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    return json.loads(value)


async def execute(args: argparse.Namespace) -> Any:
    controller = Controller.from_path(args.config)
    try:
        if args.command == "targets":
            return await controller.targets()
        if args.command == "observe":
            return await controller.observe(args.target, args.ocr)
        if args.command == "capture":
            frame, adapter = await controller.capture(
                args.target, load_json(args.params)
            )
            output = frame.save(Path(args.output).expanduser())
            return {
                "output": str(output),
                "adapter": adapter,
                "width": frame.width,
                "height": frame.height,
            }
        if args.command == "operations":
            return await controller.operations(args.target, args.raw)
        if args.command == "do":
            return await controller.perform(
                args.target, args.operation, load_json(args.params)
            )
        if args.command == "run":
            payload = load_json(args.sequence, allow_path=True)
            if isinstance(payload, list):
                steps, options = payload, {}
            else:
                steps, options = payload["steps"], payload
            return await run_sequence(
                controller,
                args.target,
                steps,
                on_error=options.get("on_error", "observe"),
                observe_after=bool(options.get("observe_after", False)),
                saved_output_limit=options.get("saved_output_limit", 32_768),
            )
        if args.command == "raw":
            return await controller.raw(
                args.target, args.adapter, args.operation, load_json(args.params)
            )
        if args.command == "runtime":
            return await controller.runtime(
                args.target,
                args.runtime_command,
                args.platform,
                args.transport,
            )
        raise ValueError(f"unknown command: {args.command}")
    finally:
        await controller.close()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="wayweaver")
    root.add_argument("--config")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("targets")

    observe = commands.add_parser("observe")
    observe.add_argument("target")
    observe.add_argument("--ocr", action="store_true")

    capture = commands.add_parser("capture")
    capture.add_argument("target")
    capture.add_argument("output")
    capture.add_argument("--params", default="{}")

    operations = commands.add_parser("operations")
    operations.add_argument("target")
    operations.add_argument("--raw", action="store_true")

    perform = commands.add_parser("do")
    perform.add_argument("target")
    perform.add_argument("operation")
    perform.add_argument("params", nargs="?", default="{}")

    run = commands.add_parser("run")
    run.add_argument("target")
    run.add_argument("sequence", help="inline JSON, a JSON file, or - for stdin")

    raw = commands.add_parser("raw")
    raw.add_argument("target")
    raw.add_argument("adapter")
    raw.add_argument("operation")
    raw.add_argument("params", nargs="?", default="{}")

    runtime = commands.add_parser("runtime")
    runtime_actions = runtime.add_subparsers(dest="runtime_command", required=True)
    for action in ("doctor", "inspect", "install", "remove"):
        command = runtime_actions.add_parser(action)
        command.add_argument("target")
        command.add_argument(
            "--platform",
            choices=("linux", "windows", "gnome"),
            default="linux",
        )
        command.add_argument("--transport")

    return root


def main() -> None:
    args = parser().parse_args()
    try:
        result = asyncio.run(execute(args))
    except Exception as error:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": error_payload(error),
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
