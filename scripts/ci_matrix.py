#!/usr/bin/env python3
"""Select explicitly provisioned cloud runners for the manual build."""

import json
import os


def build_matrix(target_os="all", macos_arch="arm64", roots=None):
    if target_os not in {"all", "windows", "linux", "macos"}:
        raise ValueError("Unsupported target_os")
    if macos_arch not in {"arm64", "x64"}:
        raise ValueError("Unsupported macos_arch")
    roots = roots or {}
    targets = [
        ("windows", "Windows", "x64", "X64", r"C:\b"),
        ("linux", "Linux", "x64", "X64", "/opt/brave-build"),
        ("macos", "macOS", macos_arch,
         "ARM64" if macos_arch == "arm64" else "X64", "/Volumes/build/brave"),
    ]
    return {"include": [
        {"target_os": target, "arch": arch,
         "labels": ["self-hosted", label, cpu, "brave-build"],
         "build_root": roots.get(target) or root}
        for target, label, arch, cpu, root in targets
        if target_os in {"all", target}
    ]}


def main():
    matrix = build_matrix(
        os.environ.get("TARGET_OS", "all"),
        os.environ.get("MACOS_ARCH", "arm64"),
        {target: os.environ.get("BUILD_ROOT_" + target.upper())
         for target in ("windows", "linux", "macos")},
    )
    value = json.dumps(matrix, separators=(",", ":"))
    print(value)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("matrix=" + value + "\n")


if __name__ == "__main__":
    main()
