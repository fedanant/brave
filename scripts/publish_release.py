#!/usr/bin/env python3
"""Validate completed native builds and publish their assets to GitHub Releases.

--dry-run prepares the release directory without contacting GitHub.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import zipfile


SUPPORTED = {("windows", "x64"), ("linux", "x64"), ("macos", "x64"), ("macos", "arm64")}
MAX_ASSET_BYTES = 2 * 1024**3


class ReleaseError(RuntimeError):
    """An incomplete build or unsafe publication request."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_path(value: str) -> str:
    require(isinstance(value, str) and bool(re.fullmatch(r"[A-Za-z0-9._ /-]+", value)),
            f"Invalid artifact path: {value!r}")
    path = PurePosixPath(value)
    require(not path.is_absolute() and path.as_posix() == value
            and all(part not in (".", "..") for part in path.parts),
            f"Artifact path must stay inside its bundle: {value!r}")
    return value


def inventory(root: Path) -> dict[str, Path]:
    require(root.is_dir() and not root.is_symlink(), f"Missing or linked artifact directory: {root}")
    files = {}
    for path in root.rglob("*"):
        require(not path.is_symlink(), f"Symbolic links are not supported: {path}")
        if path.is_file():
            files[safe_path(path.relative_to(root).as_posix())] = path
        else:
            require(path.is_dir(), f"Unsupported artifact entry: {path}")
    return files


def verify_checksums(root: Path) -> dict[str, str]:
    files = inventory(root)
    require("SHA256SUMS" in files, f"Missing SHA256SUMS in {root.name}")
    expected = {}
    for line in files["SHA256SUMS"].read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (.+)", line)
        require(match is not None, f"Invalid checksum line in {root.name}")
        digest, name = match.groups()
        name = safe_path(name)
        require(name not in expected, f"Duplicate checksum path: {name}")
        expected[name] = digest
    require(set(expected) == set(files) - {"SHA256SUMS"},
            f"Checksums must cover every bundle file exactly once: {root.name}")
    for name, digest in expected.items():
        require(sha256(files[name]) == digest, f"Checksum mismatch: {root.name}/{name}")
    return expected


def select_builds(artifacts: Path, matrix: dict, run_id: str, run_attempt: str) -> dict:
    rows = matrix.get("include")
    require(isinstance(rows, list) and bool(rows), "Build matrix must contain nonempty include rows")
    targets = [(row["target_os"], row["arch"]) for row in rows]
    require(all(target in SUPPORTED for target in targets), "Unsupported build matrix target")
    require(len(set(targets)) == len(targets), "Duplicate build matrix target")
    require(artifacts.is_dir() and not artifacts.is_symlink(), "Artifact root is missing or linked")
    candidates = {target: {} for target in targets}
    pattern = re.compile(r"brave-debloat-(windows|linux|macos)-(x64|arm64)-(\d+)-(\d+)")
    for path in artifacts.iterdir():
        match = pattern.fullmatch(path.name)
        require(match is not None and path.is_dir() and not path.is_symlink(),
                f"Unexpected artifact entry: {path.name}")
        target_os, arch, artifact_run, attempt = match.groups()
        target = (target_os, arch)
        require(artifact_run == run_id and target in candidates,
                f"Unexpected build target or run: {path.name}")
        attempt = int(attempt)
        require(0 < attempt <= int(run_attempt), f"Invalid artifact attempt: {path.name}")
        require(attempt not in candidates[target], f"Duplicate build artifact: {path.name}")
        candidates[target][attempt] = path
    require(all(candidates.values()), "Missing build artifact for an expected target")
    return {target: (max(attempts), attempts[max(attempts)])
            for target, attempts in candidates.items()}


def validate_build(root: Path, target: tuple[str, str], source_mode: str,
                   workflow_commit: str) -> dict:
    checksums = verify_checksums(root)
    manifest = json.loads((root / "build-manifest.json").read_text(encoding="utf-8"))
    target_os, arch = target
    require((manifest["target_os"], manifest["arch"]) == target,
            f"Build metadata target disagrees with artifact name: {root.name}")
    source = manifest["source"]
    require(source["mode"] == source_mode, "Build source mode disagrees with workflow input")
    version, chromium = source["version"], source["chromium_version"]
    require(bool(re.fullmatch(r"\d+\.\d+\.\d+", version)), "Invalid Brave version")
    require(bool(re.fullmatch(r"\d+\.\d+\.\d+\.\d+", chromium)), "Invalid Chromium version")
    for commit in (source["commit"], manifest["brave_commit"], manifest["chromium_commit"],
                   manifest["depot_tools_commit"]):
        require(bool(re.fullmatch(r"[0-9a-f]{40}", commit)), "Build revisions must be full commit hashes")
    require(source["commit"] == manifest["brave_commit"], "Build Brave revision disagrees with source")
    if source_mode == "repository":
        require(source["commit"] == workflow_commit, "Repository build does not match workflow commit")

    packages = {}
    for item in manifest["artifacts"]:
        name = safe_path(item["path"])
        require(PurePosixPath(name).parent.as_posix() == "browser" and name not in packages,
                f"Duplicate or invalid browser package path: {name}")
        require(checksums.get(name) == item["sha256"], f"Package manifest checksum disagrees: {name}")
        size = (root / name).stat().st_size
        require(0 < size < MAX_ASSET_BYTES, f"Release asset must be nonempty and smaller than 2 GiB: {name}")
        packages[name] = item["sha256"]
    require(set(packages) == {name for name in checksums if name.startswith("browser/")},
            "Build manifest must list every browser package exactly once")
    if target_os == "windows":
        expected = {"browser/brave_installer.exe", f"browser/brave-v{version}-win32-{arch}.zip"}
        require(set(packages) == expected, "Missing or unexpected Windows packages")
    elif target_os == "linux":
        expected = {f"browser/brave-browser_{version}_amd64.deb",
                    f"browser/brave-browser-{version}-1.x86_64.rpm",
                    f"browser/brave-browser-{version}-linux-amd64.zip"}
        require(set(packages) == expected, "Missing or unexpected Linux packages")
    else:
        full_version = chromium.split(".")[0] + "." + version
        expected_zip = f"browser/brave-v{version}-darwin-{arch}.zip"
        require(len(packages) == 3 and expected_zip in packages
                and all(sum(name.endswith(f"-{full_version}.{suffix}") for name in packages) == 1
                        for suffix in ("dmg", "pkg")), "Missing or unexpected macOS packages")
        require(bool(re.fullmatch(r"com\.brave\.[A-Za-z0-9.-]+", manifest["macos_bundle_id"] or "")),
                "Invalid macOS bundle identifier")

    policies = verify_checksums(root / "policies")
    bundle_id = manifest.get("macos_bundle_id") or "com.brave.Browser"
    required_policies = {"README.md", "SOURCE.json", "windows/brave-debloatinator.reg",
                         "linux/brave-debloatinator.json", "macos/brave-debloatinator.mobileconfig",
                         f"macos/{bundle_id}.plist"}
    require(set(policies) == required_policies, "Incomplete or unexpected policy bundle")
    require(set(checksums) == set(packages) | {"build-manifest.json", "policies/SHA256SUMS"}
            | {f"policies/{name}" for name in policies}, "Unexpected files in build bundle")
    return {"source_mode": source_mode, "version": version, "chromium_version": chromium,
            "brave_commit": source["commit"], "chromium_commit": manifest["chromium_commit"],
            "depot_tools_commit": manifest["depot_tools_commit"],
            "policy_sha256": policies["linux/brave-debloatinator.json"],
            "policy_source_sha256": policies["SOURCE.json"],
            "macos_bundle_id": manifest.get("macos_bundle_id"), "packages": packages}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prepare_release(args: argparse.Namespace) -> dict:
    require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository)),
            "Repository must be OWNER/REPO")
    require(bool(re.fullmatch(r"[0-9a-f]{40}", args.commit)), "Workflow commit must be a full SHA")
    for value in (args.run_id, args.run_attempt):
        require(bool(re.fullmatch(r"[1-9]\d*", value)), "Run ID and attempt must be positive integers")
    artifacts, output = args.artifacts.resolve(), args.output.resolve()
    require(artifacts != output and artifacts not in output.parents and output not in artifacts.parents,
            "Release output and downloaded artifacts must not contain one another")
    require(not args.output.is_symlink() and (not output.exists()
            or output.is_dir() and not any(output.iterdir())), "Release output must be new or empty")
    selected = select_builds(args.artifacts, json.loads(args.matrix_json), args.run_id, args.run_attempt)
    builds = {target: validate_build(root, target, args.source_mode, args.commit)
              for target, (_, root) in selected.items()}
    common_keys = ("source_mode", "version", "chromium_version", "brave_commit",
                   "chromium_commit", "depot_tools_commit", "policy_sha256", "policy_source_sha256")
    common = {key: next(iter(builds.values()))[key] for key in common_keys}
    require(all(all(build[key] == common[key] for key in common_keys) for build in builds.values()),
            "Cannot combine builds from different versions, revisions, source modes or policies")
    tag = f"brave-v{common['version']}-{args.source_mode}-{args.run_id}-{args.run_attempt}"
    release = {"schema_version": 1, "repository": args.repository, "tag": tag,
               "workflow_commit": args.commit, "run_id": args.run_id, "run_attempt": args.run_attempt,
               **common, "signing": "unsigned; macOS is not notarized",
               "automatic_browser_updates": False, "targets": []}
    output.mkdir(parents=True, exist_ok=True)
    for target, build in sorted(builds.items()):
        attempt, root = selected[target]
        prefix = "-".join(target)
        assets = []
        for name, digest in sorted(build["packages"].items()):
            filename = prefix + "-" + PurePosixPath(name).name.replace(" ", "-")
            destination = output / filename
            require(not destination.exists(), f"Duplicate release asset filename: {filename}")
            shutil.copyfile(root / name, destination)
            require(sha256(destination) == digest, f"Package changed while preparing release: {name}")
            assets.append(filename)
        policy_name = f"{prefix}-debloat-policies.zip"
        with zipfile.ZipFile(output / policy_name, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in sorted(inventory(root / "policies").items()):
                archive.write(path, name)
        assets.append(policy_name)
        info_name = f"{prefix}-build-info.json"
        info = {**common, "target_os": target[0], "arch": target[1], "artifact_attempt": attempt,
                "workflow_commit": args.commit, "repository": args.repository,
                "macos_bundle_id": build["macos_bundle_id"], "signing": release["signing"],
                "automatic_browser_updates": False,
                "assets": [{"name": name, "sha256": sha256(output / name)} for name in assets]}
        write_json(output / info_name, info)
        release["targets"].append({"target_os": target[0], "arch": target[1],
                                   "artifact_attempt": attempt, "build_info": info_name,
                                   "assets": [*assets, info_name]})
    write_json(output / "release-manifest.json", release)
    targets_text = ", ".join("-".join(target) for target in sorted(builds))
    notes = (f"# Brave {common['version']} with debloat policies\n\n"
             f"Platforms: {targets_text}. Source mode: `{args.source_mode}`.\n\n"
             f"Brave commit: `{common['brave_commit']}`. Chromium: `{common['chromium_version']}`.\n\n"
             "Download the browser package and the matching `debloat-policies.zip`. "
             "Follow the README inside the policy ZIP to apply or update policies.\n\n"
             "Builds are unsigned; macOS packages are not notarized. "
             "Browser updates are installed manually from these releases. "
             "SHA256SUMS checks file integrity; it is not a publisher signature.\n\n"
             f"[Build run](https://github.com/{args.repository}/actions/runs/{args.run_id}) · "
             f"[Workflow commit](https://github.com/{args.repository}/commit/{args.commit})\n")
    (output / "RELEASE-NOTES.md").write_text(notes, encoding="utf-8")
    assets = sorted(path for path in output.iterdir() if path.is_file())
    for path in assets:
        require(0 < path.stat().st_size < MAX_ASSET_BYTES,
                f"Release asset must be nonempty and smaller than 2 GiB: {path.name}")
    (output / "SHA256SUMS").write_text(
        "".join(f"{sha256(path)}  {path.name}\n" for path in assets), encoding="ascii")
    return release


def gh(*arguments: str) -> str:
    try:
        result = subprocess.run(["gh", *arguments], check=True, text=True, encoding="utf-8",
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as error:
        raise ReleaseError("Install GitHub CLI (gh) before publishing") from error
    except subprocess.CalledProcessError as error:
        raise ReleaseError(f"GitHub command failed: {(error.stderr or error.stdout or '').strip()}") from error
    return result.stdout.strip()


def publish_release(release: dict, output: Path) -> None:
    repository, tag = release["repository"], release["tag"]
    refs = json.loads(gh("api", f"repos/{repository}/git/matching-refs/tags/{tag}"))
    require(not any(ref["ref"] == f"refs/tags/{tag}" for ref in refs),
            f"Release tag already exists: {tag}. Re-run the workflow for a new attempt.")
    gh("release", "create", tag, "--repo", repository, "--target", release["workflow_commit"],
       "--draft", "--latest=false", "--title", f"Brave {release['version']} ({release['source_mode']})",
       "--notes-file", str(output / "RELEASE-NOTES.md"))
    try:
        gh("release", "upload", tag, "--repo", repository,
           *(str(path) for path in sorted(output.iterdir()) if path.is_file()))
    except ReleaseError as error:
        raise ReleaseError(f"{error}\nRelease {tag} was left as a draft; publication did not complete.") from error
    try:
        gh("release", "edit", tag, "--repo", repository, "--draft=false", "--latest=false")
    except ReleaseError as error:
        raise ReleaseError(f"{error}\nPublication was not confirmed. Inspect release {tag} before retrying.") from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--matrix-json", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    parser.add_argument("--source-mode", required=True, choices=("repository", "upstream-stable"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        release = prepare_release(args)
        url = f"https://github.com/{args.repository}/releases/tag/{release['tag']}"
        if not args.dry_run:
            publish_release(release, args.output.resolve())
            summary = os.environ.get("GITHUB_STEP_SUMMARY")
            if summary:
                with Path(summary).open("a", encoding="utf-8") as handle:
                    handle.write(f"## GitHub Release\n\n[{release['tag']}]({url})\n")
        print(json.dumps({"tag": release["tag"], "published": not args.dry_run,
                          "url": url if not args.dry_run else None,
                          "output": str(args.output.resolve())}))
    except (ReleaseError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"Release error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
