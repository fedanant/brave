#!/usr/bin/env python3
"""Package pinned Brave debloatinator policies without installing them."""

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import re
import sys
import uuid


UPSTREAM = Path(__file__).resolve().parents[1] / "policies" / "upstream"
BOOLEAN_POLICIES = {
    "BraveRewardsDisabled",
    "BraveWalletDisabled",
    "BraveVPNDisabled",
    "BraveAIChatEnabled",
    "TorDisabled",
    "PasswordManagerEnabled",
}
STRING_POLICIES = {"NewTabPageLocation", "DnsOverHttpsMode"}
REGISTRY_KEY = r"HKEY_LOCAL_MACHINE\Software\Policies\BraveSoftware\Brave"
DEFAULT_BUNDLE_ID = "com.brave.Browser.development"


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def load_policies():
    # Git may expand LF to CRLF on Windows; verify the canonical Git bytes.
    raw = (UPSTREAM / "policies.json").read_bytes().replace(b"\r\n", b"\n")
    provenance = json.loads((UPSTREAM / "SOURCE.json").read_text("utf-8"))
    if hashlib.sha256(raw).hexdigest() != provenance["sha256"]:
        raise ValueError("Vendored policies.json does not match SOURCE.json SHA256")
    blob = b"blob " + str(len(raw)).encode("ascii") + b"\0" + raw
    if hashlib.sha1(blob).hexdigest() != provenance["git_blob"]:
        raise ValueError("Vendored policies.json does not match upstream Git blob")
    policies = json.loads(raw)
    if not isinstance(policies, dict) or set(policies) != BOOLEAN_POLICIES | STRING_POLICIES:
        raise ValueError("Unexpected or missing policy names in pinned input")
    # Upstream uses 1, but BraveVPNDisabled's policy schema requires a boolean.
    if type(policies["BraveVPNDisabled"]) is int and policies["BraveVPNDisabled"] == 1:
        policies["BraveVPNDisabled"] = True
    for name in BOOLEAN_POLICIES:
        if type(policies[name]) is not bool:
            raise ValueError(f"{name} must be a boolean")
    for name in STRING_POLICIES:
        value = policies[name]
        if not isinstance(value, str) or any(ord(char) < 32 for char in value):
            raise ValueError(f"{name} must be a string without control characters")
    return policies, provenance


def windows_registry(policies):
    lines = ["Windows Registry Editor Version 5.00", "", f"[{REGISTRY_KEY}]"]
    for name, value in sorted(policies.items()):
        if isinstance(value, bool):
            lines.append(f'"{name}"=dword:{int(value):08x}')
        else:
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'"{name}"="{escaped}"')
    # Registry Editor accepts UTF-16 LE with a BOM and Windows line endings.
    return b"\xff\xfe" + ("\r\n".join(lines) + "\r\n").encode("utf-16-le")


def macos_profile(policies, bundle_id):
    identifier = f"org.brave-debloatinator.policies.{bundle_id}"

    def stable_uuid(suffix):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, identifier + suffix)).upper()

    return {
        "PayloadType": "Configuration",
        "PayloadVersion": 1,
        "PayloadIdentifier": identifier,
        "PayloadUUID": stable_uuid("/profile"),
        "PayloadDisplayName": "Brave debloatinator policies",
        "PayloadDescription": f"Pinned debloatinator policies for {bundle_id}.",
        "PayloadScope": "System",
        "PayloadRemovalDisallowed": False,
        "PayloadContent": [{
            "PayloadType": "com.apple.ManagedClient.preferences",
            "PayloadVersion": 1,
            "PayloadIdentifier": identifier + ".preferences",
            "PayloadUUID": stable_uuid("/preferences"),
            "PayloadDisplayName": "Brave managed preferences",
            "PayloadContent": {
                bundle_id: {"Forced": [{"mcx_preference_settings": policies}]}
            },
        }],
    }


def readme(policies, provenance, bundle_id):
    names = ", ".join(f"'{name}'" for name in sorted(policies))
    rows = "\n".join(f"| `{name}` | `{json.dumps(value)}` |"
                     for name, value in sorted(policies.items()))
    return f"""# Brave debloatinator policy bundle

This bundle contains managed settings for Brave. It does not remove browser
code or install policies automatically. Applying policies also disables the
built-in password manager and changes the new-tab page to Brave Search.

Source: {provenance['repository']}/tree/{provenance['commit']}
(`{provenance['path']}`, provenance in `SOURCE.json`). The vendored file is
verified against its SHA256 and Git blob before packaging. `SHA256SUMS` covers
the generated files; it is an integrity check, not a publisher signature.

Upstream's integer `BraveVPNDisabled: 1` becomes boolean `true`, as required by
Brave's policy schema. Windows represents booleans as REG_DWORD 0/1. All three
platforms use the same policies from upstream JSON, including
`NewTabPageLocation`; upstream's separate Windows .reg omits that setting.
The upstream Linux installer is not executed or included.

| Policy | Value |
| --- | --- |
{rows}

## Windows

The .reg file targets `{REGISTRY_KEY}` and requires an elevated account.
Close Brave. Before importing, run this in an elevated PowerShell window in
the bundle directory to preserve each affected value, including absent values:

```powershell
$key = 'HKLM:\\Software\\Policies\\BraveSoftware\\Brave'
$names = @({names})
$existing = Get-Item -LiteralPath $key -ErrorAction SilentlyContinue
$backup = foreach ($name in $names) {{
  $present = $null -ne $existing -and $existing.GetValueNames() -contains $name
  [pscustomobject]@{{
    Name = $name
    Present = $present
    Kind = if ($present) {{ $existing.GetValueKind($name).ToString() }} else {{ $null }}
    Value = if ($present) {{ $existing.GetValue($name, $null, 'DoNotExpandEnvironmentNames') }} else {{ $null }}
  }}
}}
$backup | ConvertTo-Json -Depth 4 | Set-Content -Encoding UTF8 -LiteralPath '.\\brave-policy-backup.json'
reg.exe import '.\\windows\\brave-debloatinator.reg'
if ($LASTEXITCODE -ne 0) {{ throw 'Policy import failed' }}
```

Keep that backup; take it only before the first application so it retains your
original values. To roll back, restore only the values changed by this bundle:

```powershell
$key = 'HKLM:\\Software\\Policies\\BraveSoftware\\Brave'
$backup = Get-Content -Raw -LiteralPath '.\\brave-policy-backup.json' | ConvertFrom-Json
foreach ($item in $backup) {{
  if ($item.Present) {{
    if (-not (Test-Path -LiteralPath $key)) {{ New-Item -Path $key -Force | Out-Null }}
    New-ItemProperty -LiteralPath $key -Name $item.Name -PropertyType $item.Kind -Value $item.Value -Force | Out-Null
  }} elseif (Test-Path -LiteralPath $key) {{
    Remove-ItemProperty -LiteralPath $key -Name $item.Name -ErrorAction SilentlyContinue
  }}
}}
```

## Linux

Brave reads managed JSON from `/etc/brave/policies/managed/`. Close Brave.
If `/etc/brave/policies/managed/brave-debloatinator.json` already exists, copy
it to a safe backup before proceeding, for example:

```sh
sudo cp -p /etc/brave/policies/managed/brave-debloatinator.json ./brave-policy-backup.json
```

Then install just this bundle's file:

```sh
sudo install -d -m 0755 /etc/brave/policies/managed
sudo install -m 0644 linux/brave-debloatinator.json /etc/brave/policies/managed/brave-debloatinator.json
```

To roll back, restore your previous named file from that backup. If there was
no previous file, remove only `/etc/brave/policies/managed/brave-debloatinator.json`.
Do not delete the managed directory or other policy files. Existing policies
in other files may conflict; inspect `brave://policy` after installation.

## macOS

This bundle targets **`{bundle_id}`**. Confirm the installed application's
`Contents/Info.plist` `CFBundleIdentifier` matches before using it. Source builds
normally use `com.brave.Browser.development`; stable, beta and nightly have
different identifiers. The build pipeline should pass the actual identifier
with `--macos-bundle-id` when packaging the application.

`macos/brave-debloatinator.mobileconfig` is an unsigned configuration profile
with a forced managed-preferences payload. Open it, then review and install it
in **System Settings > General > Device Management**. Administrators can also
deploy it through their existing MDM. It contains only these Brave preferences;
it does not enroll the Mac in an MDM or add a certificate.

The profile uses a stable identifier for this bundle and application domain,
so a later version replaces the previous one. Preserve the previous profile
file before updating an existing installation. To roll back a first install,
remove **Brave debloatinator policies** in Device Management; to roll back an
update, reinstall your previous profile. Do not remove unrelated profiles.

`macos/{bundle_id}.plist` contains the same preferences for administrators
importing custom settings into an MDM. Prefer the profile for manual setup;
do not overwrite an existing managed-preferences plist on the machine.

## Verification

Restart Brave and open `brave://policy`, reload policies, and inspect each
policy in the table above: its value must match and status must have no error
or conflict. `brave://management` shows the managed state. Exercise the affected
features, including a new tab and password-manager settings. CI validates the
generated file formats; browser behavior requires verification on each target OS.

References:

- [Brave group policy](https://support.brave.app/hc/en-us/articles/360039248271-Group-Policy)
- [Apple managed-preferences payload](https://developer.apple.com/documentation/devicemanagement/managedpreferences)
- [Installing and removing profiles on Mac](https://support.apple.com/guide/mac-help/mh35561/mac)
"""


def package(output, bundle_id):
    if not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id):
        raise ValueError("macOS bundle identifier must be a reverse-DNS identifier")
    policies, provenance = load_policies()
    files = {
        "windows/brave-debloatinator.reg": windows_registry(policies),
        "linux/brave-debloatinator.json": json_bytes(policies),
        f"macos/{bundle_id}.plist": plistlib.dumps(policies, sort_keys=True),
        "macos/brave-debloatinator.mobileconfig": plistlib.dumps(
            macos_profile(policies, bundle_id), sort_keys=True),
        "SOURCE.json": json_bytes(provenance),
        "README.md": readme(policies, provenance, bundle_id).encode("utf-8"),
    }
    checksums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n"
                        for name, data in sorted(files.items()))
    files["SHA256SUMS"] = checksums.encode("ascii")
    if output.is_symlink():
        raise ValueError("Output directory must not be a symbolic link")
    if output.exists():
        if not output.is_dir():
            raise ValueError("Output must be a directory")
        existing = list(output.rglob("*"))
        if any(path.is_symlink() for path in existing):
            raise ValueError("Output directory must not contain symbolic links")
        existing_files = {path.relative_to(output).as_posix()
                          for path in existing if path.is_file()}
        # Regeneration is allowed only in a complete bundle we generated.
        if existing_files and existing_files != set(files):
            raise ValueError("Use an empty output directory or an existing complete policy bundle")
        if existing_files:
            previous = "".join(
                f"{hashlib.sha256((output / name).read_bytes()).hexdigest()}  {name}\n"
                for name in sorted(existing_files - {"SHA256SUMS"}))
            if (output / "SHA256SUMS").read_text("ascii") != previous:
                raise ValueError("Existing bundle was modified; use an empty output directory")
    for name, data in files.items():
        destination = output / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
    print(f"Packaged {len(policies)} policies in {output} (macOS: {bundle_id})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path,
                        help="Directory for generated policy files")
    parser.add_argument("--macos-bundle-id", default=DEFAULT_BUNDLE_ID,
                        help="CFBundleIdentifier of the built macOS app")
    args = parser.parse_args()
    try:
        package(args.output, args.macos_bundle_id)
    except (OSError, ValueError, KeyError) as error:
        print(f"Policy packaging failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
