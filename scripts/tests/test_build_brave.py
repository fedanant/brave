"""Failure boundaries for source build orchestration; no browser downloads."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import build_brave


class BuildSafetyTests(unittest.TestCase):
    def test_cross_platform_dry_run_does_not_execute_or_create(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.object(build_brave, "run") as command:
            output = Path(temp) / "not-created"
            printed = io.StringIO()
            with redirect_stdout(printed):
                result = build_brave.main(["--target-os", "macos", "--arch", "arm64",
                                           "--output", str(output), "--dry-run"])
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(printed.getvalue())["arch"], "arm64")
            command.assert_not_called()
            self.assertFalse(output.exists())

    def test_unowned_persistent_directory_is_preserved(self):
        lock = build_brave.read_lock()
        with tempfile.TemporaryDirectory() as temp:
            plan = build_brave.make_plan(lock, build_brave.source_metadata(lock, None),
                                         "linux", "x64", temp, "dist")
            workspace = Path(plan["workspace"])
            workspace.mkdir()
            sentinel = workspace / "user-data"
            sentinel.write_text("keep me", encoding="utf-8")
            with self.assertRaisesRegex(build_brave.BuildError, "unowned"):
                with build_brave.workspace_guard(workspace, plan):
                    self.fail("Unowned workspace was acquired")
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep me")
            self.assertFalse((workspace / build_brave.MANAGED_MARKER).exists())

    def test_active_workspace_cannot_be_acquired_twice(self):
        lock = build_brave.read_lock()
        with tempfile.TemporaryDirectory() as temp:
            plan = build_brave.make_plan(lock, build_brave.source_metadata(lock, None),
                                         "linux", "x64", temp, "dist")
            workspace = Path(plan["workspace"])
            with build_brave.workspace_guard(workspace, plan):
                with self.assertRaisesRegex(build_brave.BuildError, "Another build"):
                    with build_brave.workspace_guard(workspace, plan):
                        self.fail("Concurrent workspace acquisition succeeded")
                self.assertTrue((workspace / ".build-active.lock").exists())
            self.assertFalse((workspace / ".build-active.lock").exists())

    def test_stale_linux_packages_do_not_count_as_current_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            build = Path(temp)
            for name in ("brave-browser_0.0.0_amd64.deb", "brave-browser-0.0.0-1.x86_64.rpm",
                         "brave-browser-0.0.0-linux-amd64.zip"):
                (build / name).write_bytes(b"old package")
            plan = {"build_dir": temp, "target_os": "linux", "arch": "x64",
                    "source": {"version": "1.95.101", "chromium_version": "153.0.8010.37"}}
            with self.assertRaisesRegex(build_brave.BuildError, "current package"):
                build_brave.collect_artifacts(plan)

    def test_build_workspace_cannot_contain_output_or_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workspace = root / "managed"
            with self.assertRaisesRegex(build_brave.BuildError, "contain one another"):
                build_brave.check_paths(root, workspace, workspace / "dist", "linux")
            with mock.patch.object(build_brave, "REPO_ROOT", root):
                with self.assertRaisesRegex(build_brave.BuildError, "contain one another"):
                    build_brave.check_paths(root, workspace, Path(temp).parent / "artifact-output", "linux")

    def test_old_macos_packages_do_not_break_new_version_collection(self):
        with tempfile.TemporaryDirectory() as temp:
            build = Path(temp)
            (build / "packaged").mkdir()
            for version in ("153.1.95.100", "153.1.95.101"):
                for suffix in ("dmg", "pkg"):
                    (build / "packaged" / f"BraveBrowser-{version}.{suffix}").write_bytes(b"package")
            (build / "dist").mkdir()
            (build / "dist" / "brave-v1.95.101-darwin-arm64.zip").write_bytes(b"archive")
            app = build / "Brave Browser.app" / "Contents"
            app.mkdir(parents=True)
            (app / "Info.plist").write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.brave.Browser"}))
            plan = {"build_dir": temp, "target_os": "macos", "arch": "arm64",
                    "source": {"version": "1.95.101", "chromium_version": "153.0.8010.37"}}
            files, bundle_id = build_brave.collect_artifacts(plan)
            self.assertEqual(bundle_id, "com.brave.Browser")
            self.assertEqual(len(files), 3)
            self.assertTrue(all("1.95.101" in path.name for path in files))


if __name__ == "__main__":
    unittest.main()
