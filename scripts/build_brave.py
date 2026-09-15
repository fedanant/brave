#!/usr/bin/env python3
"""Build a committed Brave checkout, or the pinned upstream stable release.

Requires a provisioned native runner. --dry-run only prints the plan; it never
downloads Chromium, installs packages, or creates build/output directories.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import platform
import plistlib
import re
import shutil
import subprocess
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "config" / "build-lock.json"
TARGET_OS = {"windows": "win", "linux": "linux", "macos": "mac"}
HOST_OS = {"Windows": "windows", "Linux": "linux", "Darwin": "macos"}
SUPPORTED = {("windows", "x64"), ("linux", "x64"), ("macos", "x64"), ("macos", "arm64")}
MANAGED_MARKER = ".brave-actions-workspace.json"
SYNC_MARKER = ".brave-actions-sync-complete"
ENV_HEADER = "# Managed by scripts/build_brave.py.\n"


class BuildError(RuntimeError):
    """An actionable configuration or build failure."""


def run(args: list[str], cwd: Path | None = None, *, capture: bool = False,
        env: dict[str, str] | None = None) -> str:
    """Use argument arrays, including resolved .cmd shims on Windows."""
    executable = shutil.which(args[0])
    if executable:
        args = [executable, *args[1:]]
    if not capture:
        print(f"[{cwd or Path.cwd()}] {subprocess.list2cmdline(args)}", flush=True)
    try:
        result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                                encoding="utf-8", errors="replace",
                                stdout=subprocess.PIPE if capture else None,
                                stderr=subprocess.PIPE if capture else None)
    except FileNotFoundError as error:
        raise BuildError(f"Required executable not found: {args[0]}") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "").strip() if capture else "See command output above."
        raise BuildError(f"Command failed ({error.returncode}): {args[0]} {' '.join(args[1:])}\n{detail}") from error
    return (result.stdout or "").strip()


def git(cwd: Path, *args: str) -> str:
    return run(["git", "-C", str(cwd), *args], capture=True)


def read_lock(path: Path = LOCK_PATH) -> dict:
    lock = json.loads(path.read_text(encoding="utf-8"))
    if lock.get("schema_version") != 1:
        raise BuildError("Unsupported config/build-lock.json schema_version.")
    for value in (lock["brave"]["commit"], lock["toolchain"]["depot_tools_commit"]):
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise BuildError("Source and depot_tools must use full 40-character commit hashes.")
    for value in (lock["brave"]["version"], lock["toolchain"]["node"], lock["toolchain"]["pnpm"]):
        if not re.fullmatch(r"\d+\.\d+\.\d+", value):
            raise BuildError(f"Invalid pinned version: {value!r}")
    if lock["brave"]["tag"] != "v" + lock["brave"]["version"]:
        raise BuildError("Pinned Brave tag and version disagree.")
    if lock["build"] != {"configuration": "Release", "channel": "release"}:
        raise BuildError("This packager supports only unsigned Release builds on the release channel.")
    return lock


def package_metadata(package: dict) -> dict:
    if package.get("name") != "brave-core":
        raise BuildError("The source directory must be a brave-core repository.")
    projects = package["config"]["projects"]
    chromium = projects["chrome"].get("tag", "")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", chromium):
        raise BuildError("The source package.json must pin Chromium using config.projects.chrome.tag.")
    version = package["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise BuildError(f"Unsupported Brave version: {version!r}")
    scripts = package.get("scripts", {})
    for name in ("init", "sync", "build", "create_dist"):
        if name not in scripts:
            raise BuildError(f"Source package.json is missing the {name!r} script.")
    return {"version": version, "chromium_version": chromium}


def source_metadata(lock: dict, source_dir: Path | None) -> dict:
    if source_dir is None:
        return {"mode": "upstream-stable", **lock["brave"]}
    source_dir = source_dir.resolve()
    commit = git(source_dir, "rev-parse", "HEAD")
    package = json.loads(git(source_dir, "show", f"{commit}:package.json"))
    return {"mode": "repository", "repository": str(source_dir),
            "commit": commit, **package_metadata(package)}


def make_plan(lock: dict, source: dict, target_os: str, arch: str,
              build_root: str, output: str, jobs: int | None = None) -> dict:
    if (target_os, arch) not in SUPPORTED:
        raise BuildError(f"Unsupported native target: {target_os}-{arch}")
    # A toolchain/Chromium change gets a new slot; old caches are never erased.
    slot_key = source["chromium_version"] + lock["toolchain"]["depot_tools_commit"]
    slot_hash = hashlib.sha256(slot_key.encode()).hexdigest()[:8]
    slot = f"{TARGET_OS[target_os]}-{arch}-{slot_hash}"
    path_type = PureWindowsPath if target_os == "windows" else PurePosixPath
    workspace = path_type(build_root) / slot
    core = workspace / "src" / "brave"
    build_dir = workspace / "src" / "out" / ("Release_arm64" if arch == "arm64" else "Release")
    target_flags = [f"--target_os={TARGET_OS[target_os]}", f"--target_arch={arch}"]
    build_flags = [*target_flags, "--channel=release", "--skip_signing", "--use_remoteexec=false",
                   "--gn=enable_updater:false", "--gn=enable_update_notifications:false",
                   "--gn=should_generate_symbols:false"]
    if jobs:
        build_flags.append(f"--ninja=j:{jobs}")
    return {"source": source, "toolchain": lock["toolchain"],
            "target_os": target_os, "arch": arch, "workspace": str(workspace),
            "brave_core_dir": str(core), "build_dir": str(build_dir), "output": output,
            "commands": {
                "initialize": ["pnpm", "run", "init", *target_flags, "--no-history"],
                "synchronize": ["pnpm", "run", "sync", *target_flags, "--no-history"],
                "build": ["pnpm", "run", "build", "Release", *build_flags],
                "package": ["pnpm", "run", "create_dist", "Release", *build_flags],
            },
            "signing": "unsigned; macOS is not notarized", "automatic_browser_updates": False,
            "policies": "generated as separate installable policy files; not applied to the runner"}


def version_tuple(value: str) -> tuple[int, int, int]:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", value)
    if not match:
        raise BuildError(f"Cannot parse tool version: {value!r}")
    return tuple(map(int, match.groups()))


def check_paths(build_root: Path, workspace: Path, output: Path, target_os: str,
                source_dir: Path | None = None) -> None:
    if not build_root.is_absolute():
        raise BuildError("BRAVE_BUILD_ROOT must be an absolute persistent path.")
    if workspace.resolve().parent != build_root.resolve():
        raise BuildError("The managed workspace must stay directly inside BRAVE_BUILD_ROOT.")
    if any(char.isspace() for char in str(workspace)):
        raise BuildError("BRAVE_BUILD_ROOT must not contain spaces or other whitespace.")
    if target_os == "windows" and (len(str(workspace)) > 60 or str(workspace).startswith("\\\\")):
        raise BuildError("Use a short local Windows path, for example BRAVE_BUILD_ROOT=C:\\b (no UNC paths).")
    for checkout in (REPO_ROOT, output, *([source_dir] if source_dir is not None else [])):
        first, second = checkout.resolve(), workspace.resolve()
        if first == second or first in second.parents or second in first.parents:
            raise BuildError("The CI checkout/output and persistent build workspace must not contain one another.")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise BuildError(f"Output must be a new or empty directory, to avoid uploading stale artifacts: {output}")


def preflight(plan: dict, lock: dict, *, prepare_only: bool, min_free_gb: int | None) -> None:
    host_os = HOST_OS.get(platform.system())
    if host_os != plan["target_os"]:
        raise BuildError(f"{plan['target_os']} builds require a native {plan['target_os']} runner; host is {host_os}.")
    machine = platform.machine().lower()
    host_arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(machine)
    if host_arch != plan["arch"]:
        raise BuildError(f"Use a native {plan['arch']} runner/Python process (detected {machine}).")
    if sys.version_info < (3, 10):
        raise BuildError("Python 3.10 or newer is required.")
    for tool in ("git", "node", "pnpm"):
        actual = version_tuple(run([tool, "--version"], capture=True))
        expected = version_tuple(lock["toolchain"]["git_min" if tool == "git" else tool])
        if (tool == "git" and actual < expected) or (tool != "git" and actual != expected):
            relation = "at least" if tool == "git" else "exactly"
            raise BuildError(f"Install {tool} {relation} {'.'.join(map(str, expected))}; found {actual}.")
    if plan["target_os"] == "macos":
        run(["xcodebuild", "-version"], capture=True)
        run(["xcrun", "--sdk", "macosx", "--show-sdk-path"], capture=True)
    elif plan["target_os"] == "windows":
        vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
        if not vswhere.is_file():
            raise BuildError("Install Visual Studio 2022+ with Desktop development with C++, ATL/MFC and the Windows SDK; vswhere.exe is missing.")
        installed = run([str(vswhere), "-latest", "-products", "*", "-requires",
                         "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], capture=True)
        if not installed:
            raise BuildError("Visual Studio C++ x64/x86 build tools were not found by vswhere.")
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock") as key:
                developer_mode = winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")[0]
        except OSError:
            developer_mode = 0
        if developer_mode != 1:
            raise BuildError("Enable Windows Developer Mode on the runner so Chromium can create symlinks.")
    elif not prepare_only:
        for tool in ("gcc", "g++", "pkg-config", "dpkg-deb", "rpmbuild", "zip"):
            if not shutil.which(tool):
                raise BuildError(f"Linux build prerequisite missing: {tool}. Provision Chromium dependencies before compiling (see docs/brave-debloat-ci.md).")
    ancestor = Path(plan["workspace"])
    while not ancestor.exists():
        ancestor = ancestor.parent
    available_gb = shutil.disk_usage(ancestor).free / 1024**3
    if min_free_gb is None:
        min_free_gb = 100 if (Path(plan["workspace"]) / SYNC_MARKER).exists() else 600
    if available_gb < min_free_gb:
        raise BuildError(f"Build volume has {available_gb:.1f} GiB free; need at least {min_free_gb} GiB. Provision more disk or explicitly adjust --min-free-gb for an existing cache.")


@contextmanager
def workspace_guard(workspace: Path, plan: dict):
    marker = workspace / MANAGED_MARKER
    expected = {"schema_version": 1, "target_os": plan["target_os"], "arch": plan["arch"],
                "chromium_version": plan["source"]["chromium_version"],
                "depot_tools_commit": plan["toolchain"]["depot_tools_commit"]}
    if workspace.exists() and not marker.exists() and any(workspace.iterdir()):
        raise BuildError(f"Refusing to reuse an unowned directory: {workspace}. Choose another BRAVE_BUILD_ROOT.")
    workspace.mkdir(parents=True, exist_ok=True)
    if marker.exists():
        if json.loads(marker.read_text(encoding="utf-8")) != expected:
            raise BuildError(f"Workspace metadata disagrees with this build: {marker}")
    else:
        marker.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
    active = workspace / ".build-active.lock"
    try:
        descriptor = os.open(active, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise BuildError(f"Another build owns {active}. If a prior runner was killed, verify it has stopped before removing that lock file manually.") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"pid={os.getpid()}\n")
        yield
    finally:
        active.unlink()


def checkout_source(plan: dict) -> None:
    core = Path(plan["brave_core_dir"])
    source = plan["source"]
    if source["mode"] == "repository":
        dirty = git(Path(source["repository"]), "status", "--porcelain", "--untracked-files=normal")
        if dirty:
            raise BuildError("--source-dir must be a clean committed checkout. Commit your changes first; this builder uses HEAD, not uncommitted files.")
    if core.exists() and not (core / ".git").exists() and any(core.iterdir()):
        raise BuildError(f"Refusing to replace a non-Git source directory: {core}")
    if not (core / ".git").exists():
        core.mkdir(parents=True, exist_ok=True)
        run(["git", "init", str(core)])
    if git(core, "status", "--porcelain", "--untracked-files=no"):
        raise BuildError(f"Persistent brave-core checkout has tracked modifications: {core}. Preserve or resolve them before rebuilding.")
    for key, value in (("core.autocrlf", "false"), ("core.filemode", "false"),
                       ("core.longpaths", "true"), ("core.symlinks", "true")):
        git(core, "config", key, value)
    git(core, "config", "remote.origin.url", source["repository"])
    git(core, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    # No reset/clean: fetch immutable source content and detach only a clean tree.
    run(["git", "fetch", "--no-tags", "--depth=1", source["repository"], source["commit"]], cwd=core)
    git(core, "checkout", "--detach", source["commit"])
    if git(core, "rev-parse", "HEAD") != source["commit"]:
        raise BuildError("Brave checkout does not match the requested commit.")
    package = json.loads((core / "package.json").read_text(encoding="utf-8"))
    if package_metadata(package) != {key: source[key] for key in ("version", "chromium_version")}:
        raise BuildError("Pinned/source metadata disagrees with the checked out package.json.")


def configure_checkout(plan: dict) -> None:
    core = Path(plan["brave_core_dir"])
    env_path = core / ".env"
    if env_path.exists() and not env_path.read_text(encoding="utf-8").startswith(ENV_HEADER):
        raise BuildError(f"Refusing to overwrite an unmanaged build config: {env_path}")
    env_path.write_text(ENV_HEADER + "is_brave_release_build=0\nignore_patch_version_number=false\n"
                        "use_brave_hermetic_toolchain=false\nuse_remoteexec=false\n"
                        f"projects_depot_tools_revision={plan['toolchain']['depot_tools_commit']}\n",
                        encoding="utf-8")


def collect_artifacts(plan: dict) -> tuple[list[Path], str | None]:
    build = Path(plan["build_dir"])
    version = plan["source"]["version"]
    arch = plan["arch"]
    if plan["target_os"] == "windows":
        groups = [[build / "brave_installer.exe"],
                  [build / "dist" / f"brave-v{version}-win32-{arch}.zip"]]
    elif plan["target_os"] == "linux":
        groups = [[build / f"brave-browser_{version}_amd64.deb"],
                  [build / f"brave-browser-{version}-1.x86_64.rpm"],
                  [build / f"brave-browser-{version}-linux-amd64.zip"]]
    else:
        full_version = plan["source"]["chromium_version"].split(".")[0] + "." + version
        groups = [list((build / "packaged").glob(f"*-{full_version}.dmg")),
                  list((build / "packaged").glob(f"*-{full_version}.pkg")),
                  [build / "dist" / f"brave-v{version}-darwin-{arch}.zip"]]
    files = []
    for candidates in groups:
        candidates = [path for path in candidates if path.is_file() and path.stat().st_size > 0]
        if len(candidates) != 1:
            raise BuildError(f"Expected one current package in {build}, got {len(candidates)} candidates: {candidates}. Check create_dist output; stale packages are not uploaded.")
        files.extend(candidates)
    bundle_id = None
    if plan["target_os"] == "macos":
        plists = list(build.glob("*.app/Contents/Info.plist"))
        if len(plists) != 1:
            raise BuildError(f"Expected one top-level browser .app in {build}; found {len(plists)}.")
        with plists[0].open("rb") as handle:
            bundle_id = plistlib.load(handle).get("CFBundleIdentifier")
        if not isinstance(bundle_id, str) or not bundle_id.startswith("com.brave."):
            raise BuildError(f"Unexpected browser bundle ID: {bundle_id!r}")
    return files, bundle_id


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_revisions(plan: dict) -> dict:
    core = Path(plan["brave_core_dir"])
    chromium_dir = core.parent
    revisions = {"brave_commit": git(core, "rev-parse", "HEAD"),
                 "chromium_commit": git(chromium_dir, "rev-parse", "HEAD"),
                 "depot_tools_commit": git(core.parents[1] / "vendor" / "depot_tools", "rev-parse", "HEAD")}
    if revisions["brave_commit"] != plan["source"]["commit"]:
        raise BuildError("Brave HEAD changed during synchronization/build.")
    if revisions["depot_tools_commit"] != plan["toolchain"]["depot_tools_commit"]:
        raise BuildError("depot_tools HEAD does not match the build lock.")
    expected_chromium_commit = git(chromium_dir, "rev-parse", f"refs/tags/{plan['source']['chromium_version']}^{{commit}}")
    if revisions["chromium_commit"] != expected_chromium_commit:
        raise BuildError("Synced Chromium HEAD disagrees with the version tag in package.json.")
    return revisions


def export_result(plan: dict, env: dict[str, str]) -> None:
    revisions = verify_revisions(plan)
    files, bundle_id = collect_artifacts(plan)
    output = Path(plan["output"])
    # Recheck at the end too, before copying gigabytes into an artifact folder.
    if output.exists() and any(output.iterdir()):
        raise BuildError(f"Output became nonempty during the build: {output}")
    output.mkdir(parents=True, exist_ok=True)
    packages = output / "browser"
    packages.mkdir()
    for path in files:
        shutil.copy2(path, packages / path.name)
    policy_command = [sys.executable, str(REPO_ROOT / "scripts" / "package_policies.py"),
                      "--output", str(output / "policies"),
                      "--macos-bundle-id", bundle_id or "com.brave.Browser"]
    run(policy_command, env=env)
    manifest = {**plan, "built_at": datetime.now(timezone.utc).isoformat(),
                **revisions,
                "macos_bundle_id": bundle_id,
                "artifacts": [{"path": f"browser/{path.name}", "sha256": sha256(packages / path.name)} for path in files]}
    (output / "build-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    checksum_lines = [f"{sha256(path)}  {path.relative_to(output).as_posix()}" for path in sorted(output.rglob("*")) if path.is_file()]
    (output / "SHA256SUMS").write_text("\n".join(checksum_lines) + "\n", encoding="utf-8")
    print(f"Build complete: {output}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-os", required=True, choices=TARGET_OS)
    parser.add_argument("--arch", required=True, choices=("x64", "arm64"))
    parser.add_argument("--source-dir", type=Path, help="Build this repository's committed HEAD; omit for pinned upstream stable")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--prepare-only", action="store_true", help="Fetch/sync sources and toolchains, then stop before compilation")
    parser.add_argument("--jobs", type=int, help="Maximum local compilation jobs")
    parser.add_argument("--min-free-gb", type=int, help="Minimum free GiB on build volume (default: 600 for a new checkout, 100 for an existing sync)")
    args = parser.parse_args(argv)
    try:
        if args.jobs is not None and args.jobs < 1:
            raise BuildError("--jobs must be positive.")
        if args.min_free_gb is not None and args.min_free_gb < 1:
            raise BuildError("--min-free-gb must be positive.")
        lock = read_lock()
        source = source_metadata(lock, args.source_dir)
        root_value = os.environ.get("BRAVE_BUILD_ROOT", "")
        if not root_value:
            if not args.dry_run:
                raise BuildError("Set BRAVE_BUILD_ROOT to a persistent build disk, e.g. C:\\b on Windows or /opt/brave-build on Linux/macOS.")
            root_value = r"C:\b" if args.target_os == "windows" else "/opt/brave-build"
        plan = make_plan(lock, source, args.target_os, args.arch, root_value,
                         str(args.output.resolve()), args.jobs)
        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return 0
        workspace = Path(plan["workspace"])
        check_paths(Path(root_value), workspace, args.output.resolve(), args.target_os, args.source_dir)
        preflight(plan, lock, prepare_only=args.prepare_only, min_free_gb=args.min_free_gb)
        env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1", DEPOT_TOOLS_WIN_TOOLCHAIN="0")
        # Apply these to nested gclient clones without changing machine Git config.
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
        for index, (key, value) in enumerate((("core.autocrlf", "false"), ("core.filemode", "false"),
                                              ("core.longpaths", "true"), ("core.symlinks", "true")), start=count):
            env[f"GIT_CONFIG_KEY_{index}"] = key
            env[f"GIT_CONFIG_VALUE_{index}"] = value
        env["GIT_CONFIG_COUNT"] = str(count + 4)
        with workspace_guard(workspace, plan):
            checkout_source(plan)
            configure_checkout(plan)
            core = Path(plan["brave_core_dir"])
            sync = "synchronize" if (workspace / ".gclient").exists() else "initialize"
            run(plan["commands"][sync], core, env=env)
            verify_revisions(plan)
            (workspace / SYNC_MARKER).write_text(source["commit"] + "\n", encoding="utf-8")
            if args.prepare_only:
                print(f"Sources prepared in {workspace}. On Linux provision dependencies with: sudo {workspace / 'src/build/install-build-deps.sh'}")
                return 0
            run(plan["commands"]["build"], core, env=env)
            run(plan["commands"]["package"], core, env=env)
            export_result(plan, env)
        return 0
    except (BuildError, OSError, ValueError, KeyError) as error:
        print(f"Build error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
