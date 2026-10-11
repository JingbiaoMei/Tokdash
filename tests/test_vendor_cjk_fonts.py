"""Exercise optional font installation without contacting a font server."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "vendor_cjk_fonts.sh"
FONT_PATH = Path("src/tokdash/static/vendor/fonts/noto-sans-sc-v41")
UPSTREAM_CSS = """@font-face {font-family: 'Noto Sans SC'; font-weight: 400; src: url(https://fonts.gstatic.com/s/notosanssc/v41/a.woff2);}
@font-face {font-family: 'Noto Sans SC'; font-weight: 700; src: url(https://fonts.gstatic.com/s/notosanssc/v41/b.woff2);}
"""
LOCAL_CSS = UPSTREAM_CSS.replace("https://fonts.gstatic.com/s/notosanssc/v41/", "")


@pytest.fixture
def fake_downloads(tmp_path: Path, monkeypatch):
    if sys.platform == "win32" or shutil.which("bash") is None:
        pytest.skip("the vendoring workflow requires a POSIX shell")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text(f"#!{sys.executable}\n" + """
import os
from pathlib import Path
import sys

args = sys.argv[1:]
target = Path(args[args.index('--output') + 1])
url = args[-1]
failure = os.environ.get('TOKDASH_TEST_FONT_FAILURE')
if 'fonts.googleapis.com' in url:
    target.write_text(os.environ['TOKDASH_TEST_FONT_CSS'], encoding='utf-8')
elif 'OFL.txt' in url:
    if failure == 'license':
        sys.exit(22)
    target.write_text('stub OFL license', encoding='utf-8')
elif url.endswith('b.woff2') and failure == 'slice':
    sys.exit(22)
else:
    target.write_bytes(b'complete font slice')
""", encoding="utf-8")
    curl.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("TOKDASH_TEST_FONT_CSS", UPSTREAM_CSS)
    monkeypatch.delenv("TOKDASH_TEST_FONT_FAILURE", raising=False)
    monkeypatch.delenv("TOKDASH_CJK_WEIGHTS", raising=False)
    return tmp_path


def _installed_copy(root: Path) -> Path:
    directory = root / FONT_PATH
    directory.mkdir(parents=True)
    (directory / "noto-sans-sc.css").write_text(LOCAL_CSS, encoding="utf-8")
    (directory / "OFL.txt").write_text("original license", encoding="utf-8")
    (directory / "a.woff2").write_bytes(b"original first slice")
    (directory / "b.woff2").write_bytes(b"original second slice")
    return directory


def _files(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def _run(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT)], cwd=root, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("installed", (False, True))
@pytest.mark.parametrize("failure", ("slice", "license"))
def test_failed_download_never_publishes_or_changes_an_install(fake_downloads, monkeypatch, installed, failure):
    root = fake_downloads
    directory = _installed_copy(root) if installed else root / FONT_PATH
    before = _files(directory) if installed else None
    monkeypatch.setenv("TOKDASH_TEST_FONT_FAILURE", failure)
    run = _run(root)
    assert run.returncode == 22, run.stderr
    if installed:
        assert _files(directory) == before
    else:
        assert not directory.exists()
    assert not list(directory.parent.glob(".noto-sans-sc-*")), "staging files survived the failure"


@pytest.mark.parametrize("installed", (False, True))
def test_successful_download_publishes_complete_local_fonts_and_license(fake_downloads, installed):
    root = fake_downloads
    directory = _installed_copy(root) if installed else root / FONT_PATH
    run = _run(root)
    assert run.returncode == 0, run.stderr
    assert _files(directory) == {
        "a.woff2": b"complete font slice",
        "b.woff2": b"complete font slice",
        "OFL.txt": b"stub OFL license",
        "noto-sans-sc.css": LOCAL_CSS.encode("utf-8"),
    }
    assert not list(directory.parent.glob(".noto-sans-sc-*"))


def test_reinstall_does_not_change_an_immutable_stylesheet_url(fake_downloads, monkeypatch):
    root = fake_downloads
    directory = _installed_copy(root)
    before = _files(directory)
    monkeypatch.setenv("TOKDASH_TEST_FONT_CSS", UPSTREAM_CSS.replace("font-weight: 400", "font-weight: 500"))
    run = _run(root)
    assert run.returncode == 1
    assert "different CSS" in run.stderr
    assert _files(directory) == before
    assert not list(directory.parent.glob(".noto-sans-sc-*"))


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
def test_documented_vendoring_command_has_an_executable_git_mode():
    # WSL's mounted checkout can show mode 777 for a file committed as 644.
    run = subprocess.run(
        ["git", "ls-files", "-s", "--", "scripts/vendor_cjk_fonts.sh"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=True, timeout=10,
    )
    assert run.stdout.startswith("100755 "), run.stdout
