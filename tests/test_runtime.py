import json
import os
import tempfile
import unittest
from pathlib import Path

from wayweaver.adapters.local import LocalAdapter
from wayweaver.adapters.base import Adapter
from wayweaver.cli import parser
from wayweaver.runtime import LINUX_COMMANDS, RUNTIME_VERSION, windows_uia_command
from wayweaver.runtime.deploy import (
    asset_bytes,
    asset_digest,
    manage_runtime,
    runtime_assets,
    runtime_manifest,
)
from wayweaver.types import Capability


class SystemRuntimeAdapter(Adapter):
    kind = "shell"
    capabilities = frozenset({Capability.SHELL})

    def __init__(self):
        super().__init__("shell", {})
        self.assets = iter(runtime_assets("linux"))

    async def shell(self, command, stdin=None):
        asset = next(self.assets)
        path = f"/opt/wayweaver/runtime/{RUNTIME_VERSION}/{asset.destination}"
        output = f"{asset_digest(asset)}\tsystem\t{path}".encode()
        return 0, output, b""


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_system_runtime_is_detected_without_user_install(self):
        inspected = await manage_runtime(SystemRuntimeAdapter(), "inspect", "linux")
        self.assertTrue(
            all(asset["status"] == "current" for asset in inspected["assets"])
        )
        self.assertTrue(
            all(asset["source"] == "system" for asset in inspected["assets"])
        )

    async def test_linux_runtime_lifecycle_is_versioned_and_verified(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = LocalAdapter(
                "local",
                {"environment": {"XDG_CACHE_HOME": directory}},
            )
            missing = await manage_runtime(transport, "inspect", "linux")
            self.assertEqual(
                [asset["status"] for asset in missing["assets"]],
                ["missing"] * len(LINUX_COMMANDS),
            )

            installed = await manage_runtime(transport, "install", "linux")
            self.assertTrue(installed["ok"])
            self.assertEqual(installed["runtime_version"], RUNTIME_VERSION)
            base = Path(directory) / "wayweaver" / "runtime" / RUNTIME_VERSION / "bin"
            for command in LINUX_COMMANDS:
                path = base / command
                self.assertTrue(path.is_file())
                self.assertTrue(os.access(path, os.X_OK))
            active = base.parent
            self.assertTrue(active.is_symlink())
            release = active.resolve()
            manifest = json.loads((release / "manifest.json").read_text())
            self.assertEqual(manifest, runtime_manifest("linux"))

            current = await manage_runtime(transport, "inspect", "linux")
            self.assertEqual(
                [asset["status"] for asset in current["assets"]],
                ["current"] * len(LINUX_COMMANDS),
            )
            doctor = await manage_runtime(transport, "doctor", "linux")
            self.assertTrue(
                all(asset["status"] == "current" for asset in doctor["assets"])
            )
            self.assertIn("requirements", doctor)
            self.assertIsInstance(doctor["ready"], bool)
            await manage_runtime(transport, "remove", "linux")
            self.assertFalse(base.parent.exists())

    async def test_gnome_extension_lifecycle_uses_user_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            transport = LocalAdapter(
                "local",
                {"environment": {"XDG_DATA_HOME": directory}},
            )
            await manage_runtime(transport, "install", "gnome")
            extension = (
                Path(directory)
                / "gnome-shell"
                / "extensions"
                / "wayweaver@wayweaver.local"
            )
            self.assertTrue((extension / "extension.js").is_file())
            current = await manage_runtime(transport, "inspect", "gnome")
            self.assertTrue(
                all(asset["status"] == "current" for asset in current["assets"])
            )
            await manage_runtime(transport, "remove", "gnome")
            self.assertFalse(extension.exists())

    def test_windows_runtime_asset_and_default_command_are_packaged(self):
        assets = runtime_assets("windows")
        self.assertEqual([asset.name for asset in assets], ["uia"])
        self.assertIn(b"UIAutomationClient", asset_bytes(assets[0]))
        command = windows_uia_command()
        self.assertIn(RUNTIME_VERSION, command)
        self.assertIn("$env:LOCALAPPDATA", command)

    def test_runtime_cli_requires_an_explicit_action(self):
        with self.assertRaises(SystemExit):
            parser().parse_args(["runtime"])
        args = parser().parse_args(
            ["runtime", "doctor", "desktop", "--platform", "linux"]
        )
        self.assertEqual(args.runtime_command, "doctor")
        self.assertEqual(args.target, "desktop")
        self.assertEqual(args.platform, "linux")


if __name__ == "__main__":
    unittest.main()


class ActivationVerificationTests(unittest.IsolatedAsyncioTestCase):
    """A release that fails verification must not be activated.

    The check chain used to end at a `;`, so the shell carried on and
    activated the release anyway, with the overall exit status taken from the
    commands after it -- verification was decorative. Reproduced against a
    real shell before the fix: a release with a wrong hash activated with
    exit 0.
    """

    def run_activation(self, prepare):
        import asyncio
        import os
        import subprocess
        import tempfile

        from wayweaver.runtime import RUNTIME_VERSION
        from wayweaver.runtime.deploy import (
            _LINUX_ASSETS,
            _linux_activation_command,
            _linux_release_name,
            asset_bytes,
        )

        release_name = _linux_release_name()
        with tempfile.TemporaryDirectory() as home:
            root = os.path.join(home, "wayweaver", "runtime")
            release = os.path.join(root, "releases", release_name)
            os.makedirs(os.path.join(release, "bin"))
            with open(os.path.join(release, "manifest.json"), "w") as stream:
                stream.write("{}")
            for asset in _LINUX_ASSETS:
                with open(os.path.join(release, asset.destination), "wb") as stream:
                    stream.write(asset_bytes(asset))
            prepare(release)
            completed = subprocess.run(
                ["bash", "-c", _linux_activation_command(release_name)],
                env={**os.environ, "XDG_CACHE_HOME": home},
                capture_output=True,
            )
            active = os.path.join(root, RUNTIME_VERSION)
            return completed.returncode, os.path.islink(active)

    def test_a_verified_release_activates(self):
        code, activated = self.run_activation(lambda release: None)
        self.assertEqual(code, 0)
        self.assertTrue(activated)

    def test_a_corrupt_release_is_not_activated(self):
        import os

        def corrupt(release):
            from wayweaver.runtime.deploy import _LINUX_ASSETS

            with open(
                os.path.join(release, _LINUX_ASSETS[0].destination), "ab"
            ) as stream:
                stream.write(b"tampered")

        code, activated = self.run_activation(corrupt)
        self.assertEqual(code, 5)
        self.assertFalse(activated)

    def test_a_release_without_a_manifest_is_not_activated(self):
        import os

        def strip(release):
            os.unlink(os.path.join(release, "manifest.json"))

        code, activated = self.run_activation(strip)
        self.assertEqual(code, 5)
        self.assertFalse(activated)
