"""Release-readiness contracts for the native companion apps."""

from __future__ import annotations

import re
import struct
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPANION = REPO_ROOT / "companion"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    return struct.unpack(">II", data[16:24])


def test_companion_version_is_single_authority() -> None:
    version = (COMPANION / "VERSION").read_text(encoding="utf-8").strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)

    for project in (
        COMPANION / "windows/TokdashCompanion/TokdashCompanion.csproj",
        COMPANION / "windows/TokdashCompanion.Tests/TokdashCompanion.Tests.csproj",
    ):
        assert "<Version>" not in project.read_text(encoding="utf-8")

    props = (COMPANION / "windows/Directory.Build.props").read_text(
        encoding="utf-8"
    )
    assert r"..\VERSION" in props
    assert "<Version>$(CompanionVersion)</Version>" in props

    project_yml = (COMPANION / "macos/project.yml").read_text(encoding="utf-8")
    assert f'MARKETING_VERSION: "{version}"' in project_yml


def test_macos_release_metadata_and_icons_are_complete() -> None:
    project_yml = (COMPANION / "macos/project.yml").read_text(encoding="utf-8")
    assert "ENABLE_HARDENED_RUNTIME: YES" in project_yml

    info = (COMPANION / "macos/TokdashCompanion/Info.plist").read_text(
        encoding="utf-8"
    )
    assert "$(MARKETING_VERSION)" in info
    assert "$(CURRENT_PROJECT_VERSION)" in info

    icon_dir = COMPANION / "macos/TokdashCompanion/Assets.xcassets/AppIcon.appiconset"
    expected = {
        "icon_16x16.png": (16, 16),
        "icon_16x16@2x.png": (32, 32),
        "icon_32x32.png": (32, 32),
        "icon_32x32@2x.png": (64, 64),
        "icon_128x128.png": (128, 128),
        "icon_128x128@2x.png": (256, 256),
        "icon_256x256.png": (256, 256),
        "icon_256x256@2x.png": (512, 512),
        "icon_512x512.png": (512, 512),
        "icon_512x512@2x.png": (1024, 1024),
    }
    assert {path.name for path in icon_dir.glob("*.png")} == set(expected)
    for name, size in expected.items():
        assert _png_size(icon_dir / name) == size


def test_windows_release_is_x64_and_has_both_package_formats() -> None:
    project = (
        COMPANION / "windows/TokdashCompanion/TokdashCompanion.csproj"
    ).read_text(encoding="utf-8")
    assert "<PlatformTarget>x64</PlatformTarget>" in project

    script = (COMPANION / "scripts/build_windows_release.ps1").read_text(
        encoding="utf-8"
    )
    assert '"--runtime", "win-x64"' in script
    assert "portable-unsigned.zip" in script
    assert "windows-x64-unsigned.msix" in script
    assert "makeappx.exe" in script

    manifest = (
        COMPANION / "windows/packaging/AppxManifest.xml.in"
    ).read_text(encoding="utf-8")
    assert 'ProcessorArchitecture="x64"' in manifest
    assert 'Category="windows.startupTask"' in manifest
    assert 'TaskId="TokdashCompanionStartup"' in manifest
    assert 'Enabled="false"' in manifest
    assert 'Name="runFullTrust"' in manifest


def test_companion_workflows_avoid_temporary_artifact_storage() -> None:
    companion_ci = (WORKFLOWS / "companion-ci.yml").read_text(encoding="utf-8")
    companion_release = (WORKFLOWS / "companion-release.yml").read_text(
        encoding="utf-8"
    )
    assert "actions/upload-artifact" not in companion_ci
    assert "actions/upload-artifact" not in companion_release
    assert 'tags:\n      - "companion-v*"' in companion_release
    assert "environment: companion-release" in companion_release
    assert "--prerelease" in companion_release


def test_every_actions_artifact_upload_has_one_day_retention() -> None:
    for workflow in WORKFLOWS.glob("*.yml"):
        lines = workflow.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if "actions/upload-artifact@" not in line:
                continue
            block = "\n".join(lines[index : index + 12])
            assert "retention-days: 1" in block, (
                f"{workflow.name} upload-artifact step must retain for one day"
            )


def test_companion_docs_do_not_misidentify_windows_ui() -> None:
    readme = (COMPANION / "README.md").read_text(encoding="utf-8")
    assert "C#/WPF" in readme
    assert "C#/WinUI 3" not in readme

    release = (COMPANION / "docs/RELEASE.md").read_text(encoding="utf-8")
    assert "unsigned build may be published only as a GitHub **prerelease**" in release
    assert "Production/latest releases must be signed." in release
