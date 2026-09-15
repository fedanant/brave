"""Release assembly and failure boundaries, without uploads or browser builds."""

import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import package_policies
import publish_release


VERSION = "1.95.101"
COMMIT = "a" * 40
RUN = "123456"


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.output = self.root / "release"

    def checksums(self, root):
        (root / "SHA256SUMS").write_text("".join(
            f"{publish_release.sha256(path)}  {path.relative_to(root).as_posix()}\n"
            for path in sorted(root.rglob("*"))
            if path.is_file() and path != root / "SHA256SUMS"), encoding="ascii")

    def build(self, target_os="linux", arch="x64", attempt=1, source_mode="repository"):
        root = self.artifacts / f"brave-debloat-{target_os}-{arch}-{RUN}-{attempt}"
        (root / "browser").mkdir(parents=True)
        if target_os == "windows":
            names = ["brave_installer.exe", f"brave-v{VERSION}-win32-{arch}.zip"]
        elif target_os == "linux":
            names = [f"brave-browser_{VERSION}_amd64.deb", f"brave-browser-{VERSION}-1.x86_64.rpm",
                     f"brave-browser-{VERSION}-linux-amd64.zip"]
        else:
            names = [f"BraveBrowser-153.{VERSION}.dmg", f"BraveBrowser-153.{VERSION}.pkg",
                     f"brave-v{VERSION}-darwin-{arch}.zip"]
        bundle_id = "com.brave.Browser.development" if target_os == "macos" else None
        with redirect_stdout(io.StringIO()):
            package_policies.package(root / "policies", bundle_id or "com.brave.Browser")
        for name in names:
            (root / "browser" / name).write_bytes(f"native-package-{attempt}".encode())
        manifest = {"target_os": target_os, "arch": arch,
                    "source": {"version": VERSION, "chromium_version": "153.0.8010.37",
                               "commit": COMMIT, "mode": source_mode,
                               "repository": "C:\\private\\runner\\checkout"},
                    "brave_commit": COMMIT, "chromium_commit": "b" * 40,
                    "depot_tools_commit": "c" * 40, "workspace": "/private/runner/workspace",
                    "commands": {"build": ["private-command"]}, "macos_bundle_id": bundle_id,
                    "artifacts": [{"path": f"browser/{name}",
                                   "sha256": publish_release.sha256(root / "browser" / name)}
                                  for name in names]}
        publish_release.write_json(root / "build-manifest.json", manifest)
        self.checksums(root)
        return root

    def args(self, targets=None, **overrides):
        targets = targets or [("linux", "x64")]
        values = {"artifacts": self.artifacts, "output": self.output,
                  "matrix_json": json.dumps({"include": [{"target_os": os, "arch": arch}
                                                          for os, arch in targets]}),
                  "repository": "owner/browser", "commit": COMMIT, "run_id": RUN,
                  "run_attempt": "1", "source_mode": "repository", "dry_run": False}
        return argparse.Namespace(**(values | overrides))

    def change_manifest(self, root, mutate):
        path = root / "build-manifest.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        mutate(value)
        publish_release.write_json(path, value)
        self.checksums(root)

    def test_all_native_targets_have_unique_assets_and_public_metadata(self):
        targets = [("windows", "x64"), ("linux", "x64"), ("macos", "x64"), ("macos", "arm64")]
        for target in targets:
            self.build(*target)
        release = publish_release.prepare_release(self.args(targets))
        self.assertEqual(len(release["targets"]), 4)
        self.assertEqual(release["tag"], f"brave-v{VERSION}-repository-{RUN}-1")
        self.assertTrue((self.output / f"macos-x64-BraveBrowser-153.{VERSION}.pkg").is_file())
        self.assertTrue((self.output / f"macos-arm64-BraveBrowser-153.{VERSION}.pkg").is_file())
        for target in release["targets"]:
            text = (self.output / target["build_info"]).read_text(encoding="utf-8")
            self.assertNotIn("private", text)
            self.assertNotIn("commands", text)
            self.assertNotIn("workspace", text)
        publish_release.verify_checksums(self.output)
        with zipfile.ZipFile(self.output / "macos-arm64-debloat-policies.zip") as archive:
            self.assertIn("README.md", archive.namelist())
            self.assertIn("SOURCE.json", archive.namelist())
            self.assertIn("macos/com.brave.Browser.development.plist", archive.namelist())
            archive.extractall(self.root / "extracted-policies")
        publish_release.verify_checksums(self.root / "extracted-policies")

    def test_rerun_uses_newest_available_attempt_for_each_target(self):
        old = self.build("linux", attempt=1)
        newest = self.build("linux", attempt=3)
        self.build("windows", attempt=1)
        args = self.args([("linux", "x64"), ("windows", "x64")], run_attempt="3")
        release = publish_release.prepare_release(args)
        attempts = {target["target_os"]: target["artifact_attempt"] for target in release["targets"]}
        self.assertEqual(attempts, {"linux": 3, "windows": 1})
        self.assertTrue(old.is_dir())
        self.assertTrue(newest.is_dir())
        self.assertEqual((self.output / f"linux-x64-brave-browser_{VERSION}_amd64.deb").read_bytes(),
                         b"native-package-3")

    def test_missing_or_unexpected_platform_fails_before_creating_output(self):
        self.build()
        with self.assertRaisesRegex(publish_release.ReleaseError, "Missing build artifact"):
            publish_release.prepare_release(self.args([("linux", "x64"), ("windows", "x64")]))
        self.build("windows")
        with self.assertRaisesRegex(publish_release.ReleaseError, "Unexpected build target"):
            publish_release.prepare_release(self.args())
        self.assertFalse(self.output.exists())

    def test_duplicate_matrix_target_and_attempt_are_rejected(self):
        self.build()
        with self.assertRaisesRegex(publish_release.ReleaseError, "Duplicate build matrix"):
            publish_release.prepare_release(self.args([("linux", "x64"), ("linux", "x64")]))
        self.build(attempt="01")
        with self.assertRaisesRegex(publish_release.ReleaseError, "Duplicate build artifact"):
            publish_release.prepare_release(self.args())

    def test_future_attempt_and_wrong_run_are_rejected(self):
        root = self.build(attempt=2)
        with self.assertRaisesRegex(publish_release.ReleaseError, "Invalid artifact attempt"):
            publish_release.prepare_release(self.args())
        root.rename(root.with_name(root.name.replace(RUN, "999999")))
        with self.assertRaisesRegex(publish_release.ReleaseError, "Unexpected build target or run"):
            publish_release.prepare_release(self.args(run_attempt="2"))

    def test_checksum_mismatch_prevents_any_github_call(self):
        root = self.build()
        (root / "browser" / f"brave-browser_{VERSION}_amd64.deb").write_bytes(b"corrupted")
        with mock.patch.object(publish_release, "gh") as gh, redirect_stderr(io.StringIO()):
            self.assertEqual(publish_release.main(self.cli_args()), 1)
        gh.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_missing_native_package_fails_even_with_consistent_checksums(self):
        root = self.build()
        (root / "browser" / f"brave-browser_{VERSION}_amd64.deb").unlink()
        self.change_manifest(root, lambda value: value["artifacts"].pop(0))
        with self.assertRaisesRegex(publish_release.ReleaseError, "Linux packages"):
            publish_release.prepare_release(self.args())

    def test_inner_policy_checksum_is_verified(self):
        root = self.build()
        (root / "policies" / "README.md").write_text("modified policy docs", encoding="utf-8")
        self.checksums(root)
        with self.assertRaisesRegex(publish_release.ReleaseError, "Checksum mismatch: policies/README"):
            publish_release.prepare_release(self.args())

    def test_mixed_source_revisions_are_rejected(self):
        self.build("linux", source_mode="upstream-stable")
        windows = self.build("windows", source_mode="upstream-stable")
        self.change_manifest(windows, lambda value: value.update(chromium_commit="d" * 40))
        with self.assertRaisesRegex(publish_release.ReleaseError, "different versions, revisions"):
            publish_release.prepare_release(self.args([("linux", "x64"), ("windows", "x64")],
                                                      source_mode="upstream-stable"))

    def test_mixed_policy_configuration_is_rejected(self):
        self.build("linux")
        windows = self.build("windows")
        policy = windows / "policies" / "linux" / "brave-debloatinator.json"
        values = json.loads(policy.read_text(encoding="utf-8"))
        values["BraveAIChatEnabled"] = not values["BraveAIChatEnabled"]
        publish_release.write_json(policy, values)
        self.checksums(windows / "policies")
        self.checksums(windows)
        with self.assertRaisesRegex(publish_release.ReleaseError, "different versions, revisions.*policies"):
            publish_release.prepare_release(self.args([("linux", "x64"), ("windows", "x64")]))
        self.assertFalse(self.output.exists())

    def test_source_mode_and_repository_commit_must_match_workflow(self):
        root = self.build()
        with self.assertRaisesRegex(publish_release.ReleaseError, "source mode"):
            publish_release.prepare_release(self.args(source_mode="upstream-stable"))
        self.change_manifest(root, lambda value: value["source"].update(commit="d" * 40))
        self.change_manifest(root, lambda value: value.update(brave_commit="d" * 40))
        with self.assertRaisesRegex(publish_release.ReleaseError, "workflow commit"):
            publish_release.prepare_release(self.args())

    def test_asset_limit_and_nonempty_output_fail_without_github_writes(self):
        self.build()
        with mock.patch.object(publish_release, "MAX_ASSET_BYTES", 8):
            with self.assertRaisesRegex(publish_release.ReleaseError, "smaller than 2 GiB"):
                publish_release.prepare_release(self.args())
        self.output.mkdir()
        sentinel = self.output / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(publish_release.ReleaseError, "new or empty"):
            publish_release.prepare_release(self.args())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")

    def test_checksum_traversal_duplicate_and_unlisted_file_are_rejected(self):
        root = self.build()
        path = root / "SHA256SUMS"
        original = path.read_text(encoding="ascii")
        for invalid, message in (("f" * 64 + "  ../outside\n", "inside its bundle"),
                                 (original + original.splitlines()[0] + "\n", "Duplicate checksum")):
            path.write_text(invalid, encoding="ascii")
            with self.subTest(message=message), self.assertRaisesRegex(publish_release.ReleaseError, message):
                publish_release.prepare_release(self.args())
        path.write_text(original, encoding="ascii")
        (root / "unexpected.txt").write_text("extra", encoding="utf-8")
        with self.assertRaisesRegex(publish_release.ReleaseError, "every bundle file"):
            publish_release.prepare_release(self.args())

    def cli_args(self):
        result = []
        for key, value in vars(self.args()).items():
            if key != "dry_run":
                result += ["--" + key.replace("_", "-"), str(value)]
        return result

    def test_dry_run_prepares_complete_assets_without_running_gh(self):
        self.build()
        with mock.patch.object(publish_release, "gh") as gh, redirect_stdout(io.StringIO()):
            self.assertEqual(publish_release.main([*self.cli_args(), "--dry-run"]), 0)
        gh.assert_not_called()
        self.assertTrue((self.output / "release-manifest.json").is_file())
        publish_release.verify_checksums(self.output)

    def test_publish_creates_draft_then_uploads_before_publication(self):
        self.build(source_mode="upstream-stable")
        workflow_commit = "d" * 40
        release = publish_release.prepare_release(self.args(source_mode="upstream-stable", commit=workflow_commit))
        with mock.patch.object(publish_release, "gh", side_effect=["[]", "", "", ""]) as gh:
            publish_release.publish_release(release, self.output)
        commands = [call.args for call in gh.call_args_list]
        self.assertEqual([command[:2] for command in commands[1:]],
                         [("release", "create"), ("release", "upload"), ("release", "edit")])
        self.assertEqual(commands[1][commands[1].index("--target") + 1], workflow_commit)
        self.assertIn("--draft", commands[1])
        self.assertNotIn("--clobber", commands[2])
        self.assertEqual({Path(value).name for value in commands[2][5:]},
                         {path.name for path in self.output.iterdir()})
        self.assertIn("--draft=false", commands[3])
        self.assertIn("--latest=false", commands[3])

    def test_failed_upload_leaves_draft_and_never_publishes(self):
        self.build()
        release = publish_release.prepare_release(self.args())
        with mock.patch.object(publish_release, "gh", side_effect=["[]", "",
                publish_release.ReleaseError("upload failed")]) as gh:
            with self.assertRaisesRegex(publish_release.ReleaseError, "left as a draft"):
                publish_release.publish_release(release, self.output)
        self.assertEqual(gh.call_count, 3)
        self.assertFalse(any(call.args[:2] == ("release", "edit") for call in gh.call_args_list))

    def test_failed_final_edit_reports_unconfirmed_publication(self):
        self.build()
        release = publish_release.prepare_release(self.args())
        with mock.patch.object(publish_release, "gh", side_effect=["[]", "", "",
                publish_release.ReleaseError("response lost")]):
            with self.assertRaisesRegex(publish_release.ReleaseError, "Publication was not confirmed"):
                publish_release.publish_release(release, self.output)

    def test_existing_tag_or_failed_create_cannot_overwrite_a_release(self):
        self.build()
        release = publish_release.prepare_release(self.args())
        existing = json.dumps([{"ref": f"refs/tags/{release['tag']}"}])
        with mock.patch.object(publish_release, "gh", return_value=existing) as gh:
            with self.assertRaisesRegex(publish_release.ReleaseError, "already exists"):
                publish_release.publish_release(release, self.output)
        self.assertEqual(gh.call_count, 1)
        with mock.patch.object(publish_release, "gh", side_effect=["[]",
                publish_release.ReleaseError("release already exists")]) as gh:
            with self.assertRaisesRegex(publish_release.ReleaseError, "already exists"):
                publish_release.publish_release(release, self.output)
        self.assertEqual(gh.call_count, 2)


if __name__ == "__main__":
    unittest.main()
