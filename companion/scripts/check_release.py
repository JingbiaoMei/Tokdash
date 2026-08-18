#!/usr/bin/env python3
"""Validate companion release metadata without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from xml.etree import ElementTree


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPANION_ROOT = REPO_ROOT / "companion"


def fail(message: str) -> None:
    raise SystemExit(message)


def check_msix_packaging(version: str) -> None:
    """Validate the Microsoft Store (MSIX) track.

    Store rejections happen at upload or in certification, both of which are slow and
    manual, so the cheap invariants are asserted here instead.
    """
    manifest_template_path = (
        COMPANION_ROOT / "windows" / "packaging" / "AppxManifest.xml.in"
    )
    template = manifest_template_path.read_text(encoding="utf-8")

    placeholders = (
        "@@IDENTITY_NAME@@",
        "@@PUBLISHER@@",
        "@@PUBLISHER_DISPLAY_NAME@@",
        "@@VERSION@@",
    )
    for placeholder in placeholders:
        if placeholder not in template:
            fail(f"AppxManifest template is missing {placeholder}")

    # Identity must never be hard-coded: Partner Center issues it, and a stale literal
    # would be rejected at upload rather than caught here.
    if re.search(r'Name="Tokdash\.Companion"', template):
        fail("AppxManifest Identity/Name must come from Partner Center, not a literal")

    # Substituting and parsing catches malformed XML in the template itself. An XML
    # comment cannot contain a double hyphen, which is easy to introduce when documenting
    # command-line flags.
    substituted = (
        template.replace("@@IDENTITY_NAME@@", "12345Example.TokdashCompanion")
        .replace("@@PUBLISHER@@", "CN=00000000-0000-0000-0000-000000000000")
        .replace("@@PUBLISHER_DISPLAY_NAME@@", "Example")
        .replace("@@VERSION@@", f"{version}.0")
    )
    try:
        root = ElementTree.fromstring(substituted)
    except ElementTree.ParseError as exc:
        fail(f"AppxManifest template is not well-formed XML once substituted: {exc}")

    ns = {
        "m": "http://schemas.microsoft.com/appx/manifest/foundation/windows10",
        "uap10": "http://schemas.microsoft.com/appx/manifest/uap/windows10/10",
        "rescap": (
            "http://schemas.microsoft.com/appx/manifest/foundation/windows10"
            "/restrictedcapabilities"
        ),
    }

    identity = root.find("m:Identity", ns)
    if identity is None:
        fail("AppxManifest has no Identity element")
    package_version = identity.get("Version", "")
    parts = package_version.split(".")
    if len(parts) != 4:
        fail(f"MSIX package version must have four parts, got {package_version!r}")
    if parts[3] != "0":
        fail(
            "the Store reserves the MSIX revision field and rejects a non-zero value: "
            f"{package_version!r}"
        )
    # "The other sections must be set to an integer between 0 and 65535 (except for the
    # first section, which cannot be 0)." A 0.x.y companion version is therefore not
    # submittable at all, which is why companion/VERSION starts at 1.0.0.
    # https://learn.microsoft.com/en-us/windows/apps/publish/publish-your-app/msix/app-package-requirements
    if not parts[0].isdigit() or int(parts[0]) == 0:
        fail(
            "the Store rejects a zero major version; companion/VERSION must start at "
            f"1.0.0 or later, got {package_version!r}"
        )
    for index, part in enumerate(parts[:3]):
        if not part.isdigit() or not 0 <= int(part) <= 65535:
            fail(
                f"MSIX version component {index} must be an integer 0-65535, "
                f"got {package_version!r}"
            )
    if ".".join(parts[:3]) != version:
        fail(
            f"MSIX package version {package_version!r} does not derive from "
            f"companion/VERSION {version!r}"
        )

    application = root.find("m:Applications/m:Application", ns)
    if application is None:
        fail("AppxManifest declares no Application")
    runtime_behavior = application.get(f"{{{ns['uap10']}}}RuntimeBehavior")
    trust_level = application.get(f"{{{ns['uap10']}}}TrustLevel")
    # A full-trust packaged desktop app runs outside AppContainer, which is what lets the
    # companion reach a loopback Tokdash service. appContainer would silently break it.
    if runtime_behavior != "packagedClassicApp":
        fail(f"Application RuntimeBehavior must be packagedClassicApp, got {runtime_behavior!r}")
    if trust_level != "mediumIL":
        fail(f"Application TrustLevel must be mediumIL, got {trust_level!r}")

    capabilities = [
        element.get("Name")
        for element in root.findall("m:Capabilities/*", ns)
    ]
    if capabilities != ["runFullTrust"]:
        fail(
            "the companion declares exactly one capability, runFullTrust; "
            f"found {capabilities}"
        )

    # Every asset the manifest references must exist, or packing fails late.
    packaging_root = manifest_template_path.parent
    referenced = set(re.findall(r'"(Assets\\[^"]+)"', substituted))
    referenced.update(re.findall(r"<Logo>(Assets\\[^<]+)</Logo>", substituted))
    missing = sorted(
        asset
        for asset in referenced
        if not (packaging_root / asset.replace("\\", "/")).is_file()
    )
    if missing:
        fail(f"AppxManifest references missing packaging assets: {missing}")

    builder = (
        COMPANION_ROOT / "scripts" / "build_windows_msix.ps1"
    ).read_text(encoding="utf-8")
    for required, reason in (
        (
            "-p:PublishSingleFile=false",
            "MSIX is already a container; single-file adds a decompress-to-temp startup cost",
        ),
        (
            '$packageVersion = "$version.0"',
            "the MSIX revision field must stay pinned to 0 for the Store",
        ),
        (
            "the Store rejects a zero major version",
            "the builder must refuse a 0.x.y version before packing, not at upload",
        ),
        (
            "SelfSignForTesting cannot be combined with a Store identity",
            "a Store package must be uploaded unsigned; Microsoft re-signs it",
        ),
    ):
        if required not in builder:
            fail(f"MSIX builder is missing {required!r} - {reason}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", help="Validate an expected companion-vX.Y.Z tag")
    args = parser.parse_args()

    version = (COMPANION_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        fail(f"companion/VERSION must be MAJOR.MINOR.PATCH, got {version!r}")

    if args.tag and args.tag != f"companion-v{version}":
        fail(f"tag {args.tag!r} does not match companion-v{version}")

    project_yml = (
        COMPANION_ROOT / "macos" / "project.yml"
    ).read_text(encoding="utf-8")
    if f'MARKETING_VERSION: "{version}"' not in project_yml:
        fail("macOS project.yml MARKETING_VERSION does not match companion/VERSION")
    if "ENABLE_HARDENED_RUNTIME: YES" not in project_yml:
        fail("macOS project.yml must enable Hardened Runtime")
    if 'CURRENT_PROJECT_VERSION: "2"' not in project_yml:
        fail("macOS CURRENT_PROJECT_VERSION must be the committed build number 2")
    if 'xcodeVersion: "15.4"' not in project_yml:
        fail("macOS project.yml must remain compatible with the macOS 14 CI toolchain")
    if 'SWIFT_VERSION: "5.0"' not in project_yml:
        fail("macOS project.yml must compile with the Swift 5 language mode")

    pbxproj = (
        COMPANION_ROOT
        / "macos"
        / "TokdashCompanion.xcodeproj"
        / "project.pbxproj"
    ).read_text(encoding="utf-8")
    marketing_versions = set(
        re.findall(r"MARKETING_VERSION = ([^;]+);", pbxproj)
    )
    if marketing_versions != {version}:
        fail(
            "tracked Xcode project MARKETING_VERSION values do not match "
            f"companion/VERSION: {sorted(marketing_versions)}"
        )
    if "ENABLE_HARDENED_RUNTIME = YES;" not in pbxproj:
        fail("tracked Xcode project must enable Hardened Runtime")
    object_versions = re.findall(r"^\s*objectVersion = (\d+);", pbxproj, re.MULTILINE)
    if object_versions != ["60"]:
        fail(
            "tracked Xcode project must use objectVersion 60 for Xcode 15.4: "
            f"{object_versions}"
        )
    swift_versions = set(re.findall(r"SWIFT_VERSION = ([^;]+);", pbxproj))
    if swift_versions != {"5.0"}:
        fail(
            "tracked Xcode project must use Swift 5 language mode: "
            f"{sorted(swift_versions)}"
        )
    current_versions = set(
        re.findall(r"CURRENT_PROJECT_VERSION = ([^;]+);", pbxproj)
    )
    if current_versions != {"2"}:
        fail(
            "tracked Xcode project CURRENT_PROJECT_VERSION values must be 2: "
            f"{sorted(current_versions)}"
        )

    info_plist = (
        COMPANION_ROOT / "macos" / "TokdashCompanion" / "Info.plist"
    ).read_text(encoding="utf-8")
    for build_setting in ("$(MARKETING_VERSION)", "$(CURRENT_PROJECT_VERSION)"):
        if build_setting not in info_plist:
            fail(f"macOS Info.plist must use {build_setting}")

    for project in (
        COMPANION_ROOT
        / "windows"
        / "TokdashCompanion"
        / "TokdashCompanion.csproj",
        COMPANION_ROOT
        / "windows"
        / "TokdashCompanion.Tests"
        / "TokdashCompanion.Tests.csproj",
    ):
        if re.search(r"<Version>.*?</Version>", project.read_text(encoding="utf-8")):
            fail(f"{project.relative_to(REPO_ROOT)} must inherit companion/VERSION")

    props = (
        COMPANION_ROOT / "windows" / "Directory.Build.props"
    ).read_text(encoding="utf-8")
    if r"..\VERSION" not in props or "<Version>$(CompanionVersion)</Version>" not in props:
        fail("Windows Directory.Build.props must derive Version from companion/VERSION")
    if "<CompanionBuildNumber>2</CompanionBuildNumber>" not in props:
        fail("Windows build number must match macOS CURRENT_PROJECT_VERSION 2")
    for lockfile in (
        COMPANION_ROOT / "windows" / "TokdashCompanion" / "packages.lock.json",
        COMPANION_ROOT / "windows" / "TokdashCompanion.Tests" / "packages.lock.json",
    ):
        if not lockfile.is_file():
            fail(f"missing locked .NET dependency graph: {lockfile.relative_to(REPO_ROOT)}")

    windows_builder = (
        COMPANION_ROOT / "scripts" / "build_windows_release.ps1"
    ).read_text(encoding="utf-8")
    # The portable ZIP and the Store MSIX are built by separate scripts on purpose: they
    # differ in payload layout, signing, and who publishes them. Keep the portable builder
    # from growing packaging concerns.
    if "makeappx.exe" in windows_builder or ".msix" in windows_builder:
        fail("portable builder must not produce MSIX assets; that is build_windows_msix.ps1")
    for required in (
        "-p:PublishSingleFile=true",
        "-p:PublishTrimmed=false",
        "-p:ContinuousIntegrationBuild=true",
        "THIRD-PARTY-NOTICES.txt",
    ):
        if required not in windows_builder:
            fail(f"Windows portable builder is missing {required}")

    check_msix_packaging(version)

    workflows = REPO_ROOT / ".github" / "workflows"
    action_pattern = re.compile(r"^\s*uses:\s+([^@\s]+)@([^\s#]+)", re.MULTILINE)
    for workflow in workflows.glob("*.yml"):
        text = workflow.read_text(encoding="utf-8")
        for action, ref in action_pattern.findall(text):
            if not action.startswith("./") and not re.fullmatch(r"[0-9a-f]{40}", ref):
                fail(f"{workflow.name}: {action}@{ref} is not pinned to a full commit SHA")

    release_workflow = (
        workflows / "companion-release.yml"
    ).read_text(encoding="utf-8")
    for required in (
        "name: Companion unsigned release",
        "gh release create",
        "gh release upload",
        "gh release download",
        "gh release edit",
        "GH_REPO: ${{ github.repository }}",
        "--draft",
        # Companion releases are ordinary releases, not prereleases, but they must never
        # take the repository's "Latest" pointer: this repo also publishes the Python
        # package, and a companion tag newer than the newest vX.Y.Z would otherwise become
        # what /releases/latest resolves to for everyone installing Tokdash itself.
        "--latest=false",
        "environment: companion-release-publish",
        "macos-universal-unsigned.dmg",
        "windows-x64-unsigned.zip",
        "SHA256SUMS",
    ):
        if required not in release_workflow:
            fail(f"unsigned companion release workflow is missing {required!r}")
    for forbidden in (
        "actions/upload-artifact",
        "actions/download-artifact",
        "--clobber",
        "id-token: write",
        # A bare --latest (or --prerelease) would undo the rule above.
        "--prerelease",
        "--latest ",
    ):
        if forbidden in release_workflow:
            fail(f"unsigned companion release workflow must not contain {forbidden!r}")

    release_notes = (
        COMPANION_ROOT / "RELEASE_NOTES.md"
    ).read_text(encoding="utf-8")
    for required in ("unsigned", "Gatekeeper", "SmartScreen", "SHA256SUMS"):
        if required not in release_notes:
            fail(f"companion release notes are missing {required!r}")

    native_source = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for root in (
            COMPANION_ROOT / "windows" / "TokdashCompanion",
            COMPANION_ROOT / "macos" / "TokdashCompanion",
        )
        for path in root.rglob("*")
        if path.suffix in {".cs", ".swift"}
    ).lower()
    for forbidden in (
        ".credentials.json",
        "browsercookies",
        "getextendedtcptable",
        "ipglobalproperties",
        "tcplistener",
        "checknetisolation",
        "telemetryclient",
    ):
        if forbidden in native_source:
            fail(f"native companion privacy contract forbids {forbidden!r}")

    icon_dir = (
        COMPANION_ROOT
        / "macos"
        / "TokdashCompanion"
        / "Assets.xcassets"
        / "AppIcon.appiconset"
    )
    icon_manifest = json.loads(
        (icon_dir / "Contents.json").read_text(encoding="utf-8")
    )
    missing_icons = [
        image.get("filename", "<missing filename>")
        for image in icon_manifest["images"]
        if not image.get("filename") or not (icon_dir / image["filename"]).is_file()
    ]
    if missing_icons:
        fail(f"macOS AppIcon asset is incomplete: {missing_icons}")

    print(f"Companion release metadata OK: {version}")


if __name__ == "__main__":
    main()
