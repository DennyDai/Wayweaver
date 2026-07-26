import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Any

from ..errors import ActionError, ConfigError
from . import RUNTIME_VERSION

if TYPE_CHECKING:
    from ..adapters.base import Adapter


@dataclass(frozen=True, slots=True)
class RuntimeAsset:
    name: str
    resource: tuple[str, ...]
    destination: str
    executable: bool = False


_LINUX_ASSETS = (
    RuntimeAsset(
        "applications",
        ("linux", "wayweaver-applications"),
        "bin/wayweaver-applications",
        True,
    ),
    RuntimeAsset("atspi", ("linux", "wayweaver-atspi"), "bin/wayweaver-atspi", True),
    RuntimeAsset(
        "clipboard",
        ("linux", "wayweaver-clipboard"),
        "bin/wayweaver-clipboard",
        True,
    ),
    RuntimeAsset(
        "x11-record",
        ("linux", "wayweaver-x11-record"),
        "bin/wayweaver-x11-record",
        True,
    ),
)
_WINDOWS_ASSETS = (RuntimeAsset("uia", ("windows", "uia.ps1"), "uia.ps1"),)
_GNOME_ASSETS = (
    RuntimeAsset(
        "extension",
        ("gnome", "wayweaver@wayweaver.local", "extension.js"),
        "extension.js",
    ),
    RuntimeAsset(
        "metadata",
        ("gnome", "wayweaver@wayweaver.local", "metadata.json"),
        "metadata.json",
    ),
)
_ASSETS = {
    "linux": _LINUX_ASSETS,
    "windows": _WINDOWS_ASSETS,
    "gnome": _GNOME_ASSETS,
}
_LINUX_REQUIREMENTS = (
    ("python3", "command", "python3"),
    ("sha256sum", "command", "coreutils"),
    ("maim", "command", "maim"),
    ("xclip", "command", "xclip"),
    ("xdotool", "command", "xdotool"),
    ("wmctrl", "command", "wmctrl"),
    ("xprop", "command", "x11-utils"),
    ("pyatspi", "python_module", "python3-pyatspi"),
    ("Xlib", "python_module", "python3-xlib"),
)
_GNOME_REQUIREMENTS = (
    ("gnome-extensions", "command", "gnome-shell"),
    ("gdbus", "command", "libglib2.0-bin"),
)


def runtime_assets(platform: str) -> tuple[RuntimeAsset, ...]:
    try:
        return _ASSETS[platform]
    except KeyError as error:
        raise ConfigError(f"unsupported runtime platform: {platform}") from error


def asset_bytes(asset: RuntimeAsset) -> bytes:
    resource = files("wayweaver.runtime").joinpath("assets", *asset.resource)
    return resource.read_bytes()


def asset_digest(asset: RuntimeAsset) -> str:
    return hashlib.sha256(asset_bytes(asset)).hexdigest()


def runtime_manifest(platform: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "runtime_version": RUNTIME_VERSION,
        "platform": platform,
        "assets": [
            {
                "name": asset.name,
                "path": asset.destination,
                "sha256": asset_digest(asset),
                "executable": asset.executable,
            }
            for asset in runtime_assets(platform)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["bundle_sha256"] = hashlib.sha256(encoded).hexdigest()
    return payload


def runtime_bundle_digest(platform: str) -> str:
    return str(runtime_manifest(platform)["bundle_sha256"])


def _posix_user_base(platform: str) -> str:
    if platform == "linux":
        return (
            f'"${{XDG_CACHE_HOME:-$HOME/.cache}}/wayweaver/runtime/{RUNTIME_VERSION}"'
        )
    if platform == "gnome":
        return (
            '"${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/'
            'wayweaver@wayweaver.local"'
        )
    raise ConfigError(f"unsupported POSIX runtime platform: {platform}")


def _linux_root() -> str:
    return '"${XDG_CACHE_HOME:-$HOME/.cache}/wayweaver/runtime"'


def _linux_release_name() -> str:
    return f"{RUNTIME_VERSION}-{runtime_bundle_digest('linux')[:16]}"


async def _shell(
    transport: "Adapter", command: str, stdin: bytes | None = None
) -> tuple[int, str, str]:
    code, stdout, stderr = await transport.shell(command, stdin)
    return (
        code,
        stdout.decode(errors="replace").strip(),
        stderr.decode(errors="replace").strip(),
    )


async def _require_transport(transport: "Adapter") -> None:
    available, reason = await transport.available()
    if not available:
        raise ActionError(reason or "runtime transport is unavailable")


async def _inspect_posix(transport: "Adapter", platform: str) -> list[dict[str, Any]]:
    user_base = _posix_user_base(platform)
    result = []
    for asset in runtime_assets(platform):
        expected = asset_digest(asset)
        command = f'user_base={user_base}; path="$user_base/{asset.destination}"; source=user; '
        if platform == "linux":
            command += (
                f'system_base="/opt/wayweaver/runtime/{RUNTIME_VERSION}"; '
                f'if [ ! -f "$path" ]; then path="$system_base/{asset.destination}"; source=system; fi; '
            )
        command += (
            'test -f "$path" || exit 4; '
            "actual=$(sha256sum \"$path\" | cut -d' ' -f1); "
            'printf \'%s\\t%s\\t%s\' "$actual" "$source" "$path"'
        )
        code, output, error = await _shell(transport, command)
        if code not in {0, 4}:
            raise ActionError(
                error or f"failed to inspect runtime asset {asset.name}",
                details={"platform": platform, "asset": asset.name},
            )
        if code == 4:
            actual = source = resolved_path = ""
            status = "missing"
        else:
            actual, source, resolved_path = output.split("\t", 2)
            status = "current" if actual == expected else "stale"
        result.append(
            {
                "name": asset.name,
                "path": asset.destination,
                "resolved_path": resolved_path or None,
                "source": source or None,
                "sha256": expected,
                "actual_sha256": actual or None,
                "status": status,
            }
        )
    return result


async def _upload_posix_asset(
    transport: "Adapter",
    base_assignment: str,
    asset: RuntimeAsset,
    data: bytes,
) -> str:
    expected = hashlib.sha256(data).hexdigest()
    mode = "0700" if asset.executable else "0600"
    command = (
        f'{base_assignment}; path="$base/{asset.destination}"; '
        'directory=${path%/*}; mkdir -p "$directory"; chmod 0700 "$directory"; '
        'tmp="$path.tmp.$$"; trap \'rm -f "$tmp"\' EXIT; cat > "$tmp"; '
        f'chmod {mode} "$tmp"; '
        "actual=$(sha256sum \"$tmp\" | cut -d' ' -f1); "
        f'[ "$actual" = "{expected}" ] || exit 3; '
        'mv -f "$tmp" "$path"; trap - EXIT; printf \'%s\' "$actual"'
    )
    code, actual, error = await _shell(transport, command, data)
    if code:
        raise ActionError(
            error or f"failed to install runtime asset {asset.name}",
            details={"asset": asset.name, "exit_code": code},
        )
    return actual


def _linux_activation_command(release_name: str) -> str:
    checks = " ".join(
        f'&& [ "$(sha256sum "$release/{asset.destination}" | cut -d\' \' -f1)" = "{asset_digest(asset)}" ]'
        for asset in _LINUX_ASSETS
    )
    return (
        f'root={_linux_root()}; release="$root/releases/{release_name}"; '
        # Without the exit, a failed verification is decorative: the shell
        # carries on past the `;` and activates the release anyway, with the
        # overall exit status taken from the commands that follow.
        f'active="$root/{RUNTIME_VERSION}"; test -f "$release/manifest.json" {checks} '
        "|| exit 5; "
        'link="$root/.activate.$$"; trap \'rm -f "$link"\' EXIT; '
        f'ln -s "releases/{release_name}" "$link"; '
        'if [ -e "$active" ] && [ ! -L "$active" ]; then rm -rf -- "$active"; fi; '
        'mv -Tf "$link" "$active"; trap - EXIT'
    )


async def _install_linux(transport: "Adapter") -> list[dict[str, Any]]:
    root = _linux_root()
    release_name = _linux_release_name()
    base_assignment = f'root={root}; base="$root/releases/{release_name}"'
    result = []
    for asset in _LINUX_ASSETS:
        actual = await _upload_posix_asset(
            transport, base_assignment, asset, asset_bytes(asset)
        )
        result.append(
            {
                "name": asset.name,
                "path": asset.destination,
                "sha256": actual,
                "source": "user",
                "status": "installed",
            }
        )

    manifest = runtime_manifest("linux")
    manifest_data = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    manifest_asset = RuntimeAsset("manifest", (), "manifest.json")
    await _upload_posix_asset(transport, base_assignment, manifest_asset, manifest_data)
    code, _, error = await _shell(transport, _linux_activation_command(release_name))
    if code:
        raise ActionError(
            error or "failed to activate Linux runtime",
            details={"release": release_name, "exit_code": code},
        )
    return result


async def _install_gnome(transport: "Adapter") -> list[dict[str, Any]]:
    base = _posix_user_base("gnome")
    result = []
    for asset in _GNOME_ASSETS:
        actual = await _upload_posix_asset(
            transport, f"base={base}", asset, asset_bytes(asset)
        )
        result.append(
            {
                "name": asset.name,
                "path": asset.destination,
                "sha256": actual,
                "source": "user",
                "status": "installed",
            }
        )
    return result


async def _remove_posix(transport: "Adapter", platform: str) -> None:
    if platform == "linux":
        root = _linux_root()
        command = (
            f'root={root}; active="$root/{RUNTIME_VERSION}"; '
            'target=$(readlink "$active" 2>/dev/null || true); rm -rf -- "$active"; '
            'case "$target" in releases/*) rm -rf -- "$root/$target" ;; esac; '
            f'rm -rf -- "$root/releases/{RUNTIME_VERSION}-"*'
        )
    else:
        base = _posix_user_base(platform)
        command = (
            "command -v gnome-extensions >/dev/null 2>&1 && "
            "gnome-extensions disable wayweaver@wayweaver.local >/dev/null 2>&1 || :; "
            f'base={base}; rm -rf -- "$base"'
        )
    code, _, error = await _shell(transport, command)
    if code:
        raise ActionError(error or f"failed to remove {platform} runtime")


def _windows_base() -> str:
    return (
        f"$base = Join-Path $env:LOCALAPPDATA 'Wayweaver\\runtime\\{RUNTIME_VERSION}'; "
    )


async def _inspect_windows(transport: "Adapter") -> list[dict[str, Any]]:
    result = []
    for asset in _WINDOWS_ASSETS:
        expected = asset_digest(asset)
        command = (
            _windows_base()
            + f"$path = Join-Path $base '{asset.destination}'; "
            + "if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { exit 4 }; "
            + "(Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant()"
        )
        code, actual, error = await _shell(transport, command)
        if code not in {0, 4}:
            raise ActionError(error or f"failed to inspect runtime asset {asset.name}")
        status = (
            "missing" if code == 4 else "current" if actual == expected else "stale"
        )
        result.append(
            {
                "name": asset.name,
                "path": asset.destination,
                "resolved_path": asset.destination if code == 0 else None,
                "source": "user" if code == 0 else None,
                "sha256": expected,
                "actual_sha256": actual or None,
                "status": status,
            }
        )
    return result


async def _install_windows(transport: "Adapter") -> list[dict[str, Any]]:
    result = []
    for asset in _WINDOWS_ASSETS:
        data = asset_bytes(asset)
        expected = hashlib.sha256(data).hexdigest()
        command = (
            _windows_base()
            + "New-Item -ItemType Directory -Force -Path $base | Out-Null; "
            + f"$path = Join-Path $base '{asset.destination}'; $tmp = \"$path.tmp.$PID\"; "
            + "$source = [Console]::OpenStandardInput(); $output = [IO.File]::Create($tmp); "
            + "try { $source.CopyTo($output) } finally { $output.Dispose() }; "
            + "$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $tmp).Hash.ToLowerInvariant(); "
            + f"if ($actual -ne '{expected}') {{ Remove-Item $tmp -Force; exit 3 }}; "
            + "Move-Item -LiteralPath $tmp -Destination $path -Force; [Console]::Out.Write($actual)"
        )
        code, actual, error = await _shell(transport, command, data)
        if code:
            raise ActionError(
                error or f"failed to install runtime asset {asset.name}",
                details={"platform": "windows", "asset": asset.name, "exit_code": code},
            )
        result.append(
            {
                "name": asset.name,
                "path": asset.destination,
                "sha256": actual,
                "source": "user",
                "status": "installed",
            }
        )
    return result


async def _remove_windows(transport: "Adapter") -> None:
    code, _, error = await _shell(
        transport,
        _windows_base()
        + "if (Test-Path -LiteralPath $base) { Remove-Item -Recurse -Force $base }",
    )
    if code:
        raise ActionError(error or "failed to remove windows runtime")


async def _probe_requirement(
    transport: "Adapter", name: str, kind: str, package: str, platform: str
) -> dict[str, Any]:
    if platform == "windows":
        # The runtime requirement is PowerShell's UI Automation assembly; any
        # other requirement is a command, and hard-coding the assembly check
        # would silently report on the wrong thing once one is added.
        command = (
            "$ErrorActionPreference = 'Stop'; "
            "Add-Type -AssemblyName UIAutomationClient; "
            "$PSVersionTable.PSVersion.ToString()"
            if kind == "runtime"
            else f"$ErrorActionPreference = 'Stop'; (Get-Command {name}).Version.ToString()"
        )
    elif kind == "python_module":
        command = f"python3 -c 'import {name}'"
    else:
        command = f"command -v {name} >/dev/null"
    code, output, _ = await _shell(transport, command)
    return {
        "name": name,
        "kind": kind,
        "available": code == 0,
        "version": output or None,
        "install_hint": None if code == 0 else package,
    }


async def _doctor_requirements(
    transport: "Adapter", platform: str
) -> list[dict[str, Any]]:
    if platform == "linux":
        requirements = _LINUX_REQUIREMENTS
    elif platform == "gnome":
        requirements = _GNOME_REQUIREMENTS
    else:
        requirements = (("PowerShell UI Automation", "runtime", "Windows PowerShell"),)
    return [
        await _probe_requirement(transport, name, kind, package, platform)
        for name, kind, package in requirements
    ]


async def manage_runtime(
    transport: "Adapter", action: str, platform: str
) -> dict[str, Any]:
    if action not in {"doctor", "inspect", "install", "remove"}:
        raise ConfigError(f"unsupported runtime action: {action}")
    runtime_assets(platform)
    await _require_transport(transport)
    requirements: list[dict[str, Any]] | None = None
    if action == "remove":
        if platform == "windows":
            await _remove_windows(transport)
        else:
            await _remove_posix(transport, platform)
        assets: list[dict[str, Any]] = []
    elif action in {"doctor", "inspect"}:
        assets = (
            await _inspect_windows(transport)
            if platform == "windows"
            else await _inspect_posix(transport, platform)
        )
        if action == "doctor":
            requirements = await _doctor_requirements(transport, platform)
    elif platform == "windows":
        assets = await _install_windows(transport)
    elif platform == "linux":
        assets = await _install_linux(transport)
    else:
        assets = await _install_gnome(transport)
    result: dict[str, Any] = {
        "ok": True,
        "action": action,
        "platform": platform,
        "runtime_version": RUNTIME_VERSION,
        "bundle_sha256": runtime_bundle_digest(platform),
        "assets": assets,
    }
    if requirements is not None:
        result["requirements"] = requirements
        result["ready"] = all(asset["status"] == "current" for asset in assets) and all(
            requirement["available"] for requirement in requirements
        )
    return result
