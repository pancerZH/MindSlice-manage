"""Temporary git repos, a fake PyPI and a config that ties them together."""

import json
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from mindslice_manage import sources
from mindslice_manage.check import Context
from mindslice_manage.config import load_config, load_projects

GIT = ["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def git(repo: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def make_repo(path: Path, files: dict[str, str], tag: str | None = None) -> Path:
    path.mkdir(parents=True)
    for name, text in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    if tag:
        git(path, "tag", tag)
    return path


def pypi(releases: dict[str, dict[str, list[str]]]):
    """A fetch_json that serves {package: {version: [wheel sha256]}}."""
    def fetch(url: str):
        parts = url.removeprefix(sources.PYPI + "/").split("/")
        pkg = parts[0]
        if pkg not in releases:
            return None
        def files(version):
            return [{"filename": f"{pkg}-{version}-py3-none-any.whl", "packagetype": "bdist_wheel", "yanked": False,
                     "url": f"https://files.example/{pkg}-{version}-py3-none-any.whl", "digests": {"sha256": sha}}
                    for sha in releases[pkg][version]]
        if len(parts) == 3:  # /pkg/version/json
            return {"urls": files(parts[1])} if parts[1] in releases[pkg] else None
        return {"info": {"version": "0"}, "releases": {v: files(v) for v in releases[pkg]}}
    return mock.patch.object(sources, "fetch_json", fetch)


class Workspace(unittest.TestCase):
    """A temp dir with repos, managed/ specs and config.toml."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.repos: list[str] = []
        self.hosts = self.root / "hosts"
        (self.hosts / "pipx").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def repo(self, name: str, files: dict[str, str], tag: str | None = None) -> Path:
        self.repos.append(name)
        return make_repo(self.root / name, files, tag)

    def spec(self, name: str, text: str) -> None:
        (self.root / "managed").mkdir(exist_ok=True)
        (self.root / "managed" / f"{name}.toml").write_text(textwrap.dedent(text).lstrip("\n"), encoding="utf-8")

    def config(self, harness: str = "") -> Path:
        lines = [f'[[repos]]\npath = "{r}"\n' for r in self.repos]
        lines.append(f'[hosts]\npipx_venvs = "{self.hosts / "pipx"}"\nclaude = "{self.hosts / "claude.json"}"\n'
                     f'agy = "{self.hosts / "agy"}"\ncodex = "{self.hosts / "codex"}"\n')
        lines.append(harness)
        f = self.root / "config.toml"
        f.write_text("\n".join(lines), encoding="utf-8")
        return f

    def ctx(self, online: bool = True, harness: str = "") -> Context:
        cfg = load_config(self.config(harness))
        ctx = Context(cfg, load_projects(cfg), online=online)
        ctx.prefetch()
        return ctx


ENGINE = {"pyproject.toml": '[project]\nname = "engine"\ndynamic = ["version"]\n',
          "engine/__init__.py": '__version__ = "1.2.0"\n'}
ENGINE_SPEC = """
schema = 1
name = "engine"

[version]
locations = [ { file = "engine/__init__.py", regex = '__version__ = "([^"]+)"' } ]

[release]
pypi = "engine"
tag = "v{version}"
"""


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
