"""Path resolution for the Qoder IDE DB and CLI roots (clientpaths)."""
from pathlib import Path

from tokdash import clientpaths, osinfo


def _make_db(base: Path) -> Path:
    db = base / "SharedClientCache" / "cache" / "db" / "local.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    db.write_bytes(b"sqlite")
    return db


def test_qoder_ide_db_path_env_override_wins(monkeypatch, tmp_path):
    db = _make_db(tmp_path / "override")
    appdata = tmp_path / "appdata"
    _make_db(appdata / "QoderCN")
    monkeypatch.setenv("QODER_IDE_DATA_DIR", str(tmp_path / "override"))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(osinfo, "os_kind", lambda: "windows")
    assert clientpaths.qoder_ide_db_path() == db


def test_qoder_ide_db_path_env_override_missing_db(monkeypatch, tmp_path):
    monkeypatch.setenv("QODER_IDE_DATA_DIR", str(tmp_path / "absent"))
    assert clientpaths.qoder_ide_db_path() is None


def test_qoder_ide_db_path_windows_prefers_cn(monkeypatch, tmp_path):
    appdata = tmp_path / "appdata"
    cn = _make_db(appdata / "QoderCN")
    intl = _make_db(appdata / "Qoder")
    monkeypatch.delenv("QODER_IDE_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(osinfo, "os_kind", lambda: "windows")
    assert clientpaths.qoder_ide_db_path() == cn
    cn.unlink()
    assert clientpaths.qoder_ide_db_path() == intl
    intl.unlink()
    assert clientpaths.qoder_ide_db_path() is None


def test_qoder_ide_db_path_macos_prefers_international(monkeypatch, tmp_path):
    monkeypatch.delenv("QODER_IDE_DATA_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    intl = _make_db(tmp_path / "Library" / "Application Support" / "Qoder")
    cn = _make_db(tmp_path / "Library" / "Application Support" / "QoderCN")
    monkeypatch.setattr(osinfo, "os_kind", lambda: "macos")
    assert clientpaths.qoder_ide_db_path() == intl
    intl.unlink()
    assert clientpaths.qoder_ide_db_path() == cn


def test_qoder_ide_db_path_linux_uses_xdg(monkeypatch, tmp_path):
    xdg = tmp_path / "xdg"
    intl = _make_db(xdg / "Qoder")
    cn = _make_db(xdg / "QoderCN")
    monkeypatch.delenv("QODER_IDE_DATA_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setattr(osinfo, "os_kind", lambda: "linux")
    assert clientpaths.qoder_ide_db_path() == intl
    intl.unlink()
    assert clientpaths.qoder_ide_db_path() == cn


def test_qoder_ide_db_path_linux_default_share(monkeypatch, tmp_path):
    monkeypatch.delenv("QODER_IDE_DATA_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db = _make_db(tmp_path / ".local" / "share" / "QoderCN")
    monkeypatch.setattr(osinfo, "os_kind", lambda: "linux")
    assert clientpaths.qoder_ide_db_path() == db


def test_qoder_ide_db_path_wsl_glob_brand_priority_across_users(monkeypatch, tmp_path):
    mnt = tmp_path / "mnt"
    # A Qoder (international) DB for one user, a QoderCN DB for another:
    # brand priority must beat user/path order.
    cn_bob = _make_db(mnt / "Users" / "bob" / "AppData" / "Roaming" / "QoderCN")
    intl_alice = _make_db(mnt / "Users" / "alice" / "AppData" / "Roaming" / "Qoder")
    monkeypatch.delenv("QODER_IDE_DATA_DIR", raising=False)
    monkeypatch.setattr(clientpaths, "_wsl_windows_root", lambda: mnt)
    monkeypatch.setattr(osinfo, "os_kind", lambda: "wsl")
    assert clientpaths.qoder_ide_db_path() == cn_bob
    cn_bob.unlink()
    # Within one brand the sorted user order wins.
    cn_alice = _make_db(mnt / "Users" / "alice" / "AppData" / "Roaming" / "QoderCN")
    assert clientpaths.qoder_ide_db_path() == cn_alice
    cn_alice.unlink()
    assert clientpaths.qoder_ide_db_path() == intl_alice
    intl_alice.unlink()
    assert clientpaths.qoder_ide_db_path() is None


def test_qoder_cli_roots_union_in_priority_order(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".qoder").mkdir(parents=True)
    (home / ".qoder-cn").mkdir(parents=True)
    h1, h2 = tmp_path / "h1", tmp_path / "h2"
    h1.mkdir()
    h2.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("QODER_CLI_HOME", f"{h1},{h2}")
    monkeypatch.setenv("QODER_CONFIG_DIR", str(cfg))
    assert clientpaths.qoder_cli_roots() == [h1, h2, cfg, home / ".qoder", home / ".qoder-cn"]


def test_qoder_cli_roots_dedupe_and_skip_missing(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setenv("QODER_CLI_HOME", f"{cfg}, {tmp_path / 'missing'}")
    monkeypatch.setenv("QODER_CONFIG_DIR", str(cfg))
    assert clientpaths.qoder_cli_roots() == [cfg]


def test_qoder_cli_roots_defaults(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".qoder").mkdir(parents=True)
    (home / ".qoder-cn").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.delenv("QODER_CLI_HOME", raising=False)
    monkeypatch.delenv("QODER_CONFIG_DIR", raising=False)
    assert clientpaths.qoder_cli_roots() == [home / ".qoder", home / ".qoder-cn"]
