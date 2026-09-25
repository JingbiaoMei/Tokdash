"""Packaging regression test: the companion/ tree must never enter the wheel or sdist.

The native macOS/Windows companion apps live under ``companion/`` but the Python
package is built from ``src/``. ``MANIFEST.in`` has ``prune companion``; this
test builds both distributions and asserts no companion path leaked in.
"""
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _build_dists(tmp_path: Path) -> tuple[Path, Path]:
    """Build wheel + sdist into tmp_path; return (wheel, sdist) paths."""
    subprocess.run(
        [sys.executable, "-m", "build", "--outdir", str(tmp_path), str(REPO_ROOT)],
        check=True,
        capture_output=True,
    )
    wheels = list(tmp_path.glob("tokdash-*.whl"))
    sdists = list(tmp_path.glob("tokdash-*.tar.gz"))
    assert wheels, "no wheel built"
    assert sdists, "no sdist built"
    return wheels[0], sdists[0]


def _wheel_names(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as zf:
        return set(zf.namelist())


def _sdist_names(sdist: Path) -> set[str]:
    with tarfile_open(sdist) as tf:
        return set(tf.getnames())


def tarfile_open(sdist: Path):
    import tarfile

    return tarfile.open(sdist, "r:gz")


def _assert_no_companion(names: set[str], label: str) -> None:
    leaks = sorted(n for n in names if "/companion/" in n or n.startswith("companion/"))
    assert not leaks, (
        f"{label} contains companion/ paths (MANIFEST.in prune missing?): {leaks}"
    )


def test_wheel_excludes_companion(tmp_path: Path) -> None:
    wheel, _ = _build_dists(tmp_path)
    names = _wheel_names(wheel)
    _assert_no_companion(names, "wheel")


def test_sdist_excludes_companion(tmp_path: Path) -> None:
    _, sdist = _build_dists(tmp_path)
    names = _sdist_names(sdist)
    _assert_no_companion(names, "sdist")


def test_manifest_in_prunes_companion() -> None:
    manifest = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "prune companion" in manifest, "MANIFEST.in missing 'prune companion'"
