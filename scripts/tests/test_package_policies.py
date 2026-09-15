"""Portable checks for the generated policy bundle; never install policies."""

import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "package_policies.py"
SPEC = importlib.util.spec_from_file_location("package_policies", SCRIPT)
package_policies = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(package_policies)


class PackagePoliciesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def generate(self, path, bundle_id="com.brave.Browser.development"):
        with contextlib.redirect_stdout(io.StringIO()):
            package_policies.package(path, bundle_id)

    def test_formats_and_checksums(self):
        output = self.root / "bundle"
        domain = "com.brave.Browser"
        self.generate(output, domain)
        policies = json.loads((output / "linux/brave-debloatinator.json").read_bytes())
        self.assertIs(policies["BraveVPNDisabled"], True)
        self.assertIs(policies["PasswordManagerEnabled"], False)
        self.assertEqual(policies["NewTabPageLocation"], "https://search.brave.com")
        registry_bytes = (output / "windows/brave-debloatinator.reg").read_bytes()
        self.assertTrue(registry_bytes.startswith(b"\xff\xfe"))
        registry = registry_bytes.decode("utf-16")
        self.assertIn("[HKEY_LOCAL_MACHINE\\Software\\Policies\\BraveSoftware\\Brave]", registry)
        self.assertIn('"BraveVPNDisabled"=dword:00000001\r\n', registry)
        self.assertIn('"PasswordManagerEnabled"=dword:00000000\r\n', registry)
        self.assertIn('"NewTabPageLocation"="https://search.brave.com"', registry)
        plist = plistlib.loads((output / f"macos/{domain}.plist").read_bytes())
        self.assertEqual(plist, policies)
        profile = plistlib.loads((output / "macos/brave-debloatinator.mobileconfig").read_bytes())
        self.assertEqual(profile["PayloadType"], "Configuration")
        self.assertFalse(profile["PayloadRemovalDisallowed"])
        payload = profile["PayloadContent"][0]
        self.assertEqual(payload["PayloadType"], "com.apple.ManagedClient.preferences")
        forced = payload["PayloadContent"][domain]["Forced"][0]["mcx_preference_settings"]
        self.assertEqual(forced, policies)
        sums = (output / "SHA256SUMS").read_text("ascii").splitlines()
        self.assertEqual(len(sums), 6)
        for line in sums:
            expected, name = line.split("  ", 1)
            self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), expected)

    def test_deterministic_and_repeatable(self):
        first, second = self.root / "first", self.root / "second"
        self.generate(first)
        self.generate(second)
        snapshot = {path.relative_to(first): path.read_bytes()
                    for path in first.rglob("*") if path.is_file()}
        self.generate(first)
        for name, data in snapshot.items():
            self.assertEqual((second / name).read_bytes(), data)
            self.assertEqual((first / name).read_bytes(), data)

    def test_rejects_tampered_source_before_writing(self):
        source = self.root / "source"
        source.mkdir()
        for name in ("policies.json", "SOURCE.json"):
            (source / name).write_bytes((package_policies.UPSTREAM / name).read_bytes())
        with (source / "policies.json").open("ab") as stream:
            stream.write(b" ")
        output = self.root / "bundle"
        with patch.object(package_policies, "UPSTREAM", source):
            with self.assertRaisesRegex(ValueError, "SHA256"):
                self.generate(output)
        self.assertFalse(output.exists())

    def test_rejects_invalid_bundle_id_and_unowned_output(self):
        for bundle_id in ("../escape", "com/brave/Browser", "", "com.brave.\nBrowser"):
            with self.subTest(bundle_id=bundle_id):
                with self.assertRaises(ValueError):
                    self.generate(self.root / "bundle", bundle_id)
        output = self.root / "existing"
        output.mkdir()
        protected = output / "README.md"
        protected.write_text("Keep me", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "empty output"):
            self.generate(output)
        self.assertEqual(protected.read_text("utf-8"), "Keep me")

    def test_rejects_modified_bundle(self):
        output = self.root / "bundle"
        self.generate(output)
        readme = output / "README.md"
        readme.write_text("User edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "modified"):
            self.generate(output)
        self.assertEqual(readme.read_text("utf-8"), "User edit")


if __name__ == "__main__":
    unittest.main()
