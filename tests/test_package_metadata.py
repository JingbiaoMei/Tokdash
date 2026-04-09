import re
from pathlib import Path

from tokdash import __version__


def _get_toml_section(text: str, header: str) -> str:
    parts = re.split(rf"^\[{re.escape(header)}\]\s*$", text, flags=re.M)
    if len(parts) < 2:
        return ""
    return re.split(r"^\[", parts[1], flags=re.M)[0]


def test_pyproject_version_configuration_matches_runtime_version():
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    project_body = _get_toml_section(pyproject, "project")
    assert project_body, "Could not find [project] section in pyproject.toml"

    static_match = re.search(r'^\s*version\s*=\s*"([^"]+)"\s*$', project_body, flags=re.M)
    if static_match:
        assert static_match.group(1) == __version__
        return

    dynamic_match = re.search(r"^\s*dynamic\s*=\s*\[(.*?)\]\s*$", project_body, flags=re.M | re.S)
    assert dynamic_match, "Could not find project.version or dynamic version configuration"
    assert '"version"' in dynamic_match.group(1)

    dynamic_body = _get_toml_section(pyproject, "tool.setuptools.dynamic")
    assert dynamic_body, "Could not find [tool.setuptools.dynamic] section in pyproject.toml"

    attr_match = re.search(r'^\s*version\s*=\s*\{\s*attr\s*=\s*"([^"]+)"\s*\}\s*$', dynamic_body, flags=re.M)
    assert attr_match, "Could not find tool.setuptools.dynamic.version attr"
    assert attr_match.group(1) == "tokdash.__version__"
