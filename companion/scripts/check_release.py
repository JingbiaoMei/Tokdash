#!/usr/bin/env python3
"""Validate companion release metadata without third-party dependencies."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMPANION_ROOT = REPO_ROOT / "companion"


def fail(message: str) -> None:
    raise SystemExit(message)


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
    if "makeappx.exe" in windows_builder or "unsigned.msix" in windows_builder:
        fail("Windows builder must not produce deferred MSIX assets")
    for required in (
        "-p:PublishSingleFile=true",
        "-p:PublishTrimmed=false",
        "-p:ContinuousIntegrationBuild=true",
        "THIRD-PARTY-NOTICES.txt",
    ):
        if required not in windows_builder:
            fail(f"Windows portable builder is missing {required}")

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
        "name: Companion unsigned prerelease",
        "gh release create",
        "gh release upload",
        "gh release download",
        "gh release edit",
        "GH_REPO: ${{ github.repository }}",
        "--draft",
        "--prerelease",
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
