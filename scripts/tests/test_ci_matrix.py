import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "ci_matrix", Path(__file__).resolve().parents[1] / "ci_matrix.py")
ci_matrix = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci_matrix)


class MatrixTests(unittest.TestCase):
    def test_all_platforms_route_to_distinct_native_cloud_runners(self):
        rows = ci_matrix.build_matrix()["include"]
        self.assertEqual({row["target_os"] for row in rows},
                         {"windows", "linux", "macos"})
        for row in rows:
            self.assertIn("self-hosted", row["labels"])
            self.assertIn("brave-build", row["labels"])
            self.assertIn(row["arch"].upper(), row["labels"])

    def test_macos_intel_changes_architecture_and_routing_together(self):
        rows = ci_matrix.build_matrix("macos", "x64")["include"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["arch"], "x64")
        self.assertIn("X64", rows[0]["labels"])
        self.assertNotIn("ARM64", rows[0]["labels"])

    def test_custom_volume_is_preserved(self):
        row = ci_matrix.build_matrix(
            "windows", roots={"windows": r"D:\b"})["include"][0]
        self.assertEqual(row["build_root"], r"D:\b")

    def test_invalid_target_cannot_silently_skip_all_builds(self):
        with self.assertRaises(ValueError):
            ci_matrix.build_matrix("windwos")
        with self.assertRaises(ValueError):
            ci_matrix.build_matrix(macos_arch="amd64")


if __name__ == "__main__":
    unittest.main()
